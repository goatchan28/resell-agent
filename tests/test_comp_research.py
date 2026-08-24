"""Comp research: gather evidence about other listings, and price nothing.

The identity agent that goes wrong attaches the wrong attributes to an object.
This one produces numbers, and a number carries an authority prose does not, so
what is tested here is mostly what the model is *not* allowed to decide: whether a
price was paid, what condition a listing is in, and how strong a comparison may be
claimed.
"""

from __future__ import annotations

import json

import pytest

from resell import db, store_pricing as sp
from resell.domain import FeeModel
from resell.gateway import Gateway
from resell.pricing.comps import Comparability, ConditionBand, PriceKind
from resell.reasoning.adapters.fetch import PageFetcher
from resell.reasoning.adapters.marketplace import (
    OperatorUrlMarketplaceAdapter,
    marketplace_for_url,
)
from resell.reasoning.adapters.research import ResearchQuery
from resell.reasoning.authority import fetch_permitted
from resell.reasoning.comp_reading import (
    band_for_declared_condition,
    price_supported_by,
    read_price_kind,
)
from resell.reasoning.tools import (
    parse_comp_extract_tool_input,
    parse_comp_judge_tool_input,
    parse_comp_plan_tool_input,
)


# --- sold versus asking, which is the whole ballgame -------------------------------


def test_a_sold_claim_needs_the_page_to_say_so():
    kind, why = read_price_kind("sold", "Sold for $149.99 on 3 Aug", sale_date_given=False)
    assert kind is PriceKind.REALIZED
    assert "sold for" in why


def test_a_sold_claim_with_no_support_becomes_asking():
    """A sample of asking prices recorded as sales reads as a firm market that does
    not exist. Unsupported claims fail toward the weaker reading."""
    kind, why = read_price_kind("sold", "$149.99 Buy It Now", sale_date_given=False)
    assert kind is PriceKind.ASKING
    assert "no sale marker" in why


def test_a_sale_date_also_supports_a_sold_reading():
    kind, _ = read_price_kind("sold", "$149.99", sale_date_given=True)
    assert kind is PriceKind.REALIZED


def test_an_asking_price_is_never_promoted():
    kind, _ = read_price_kind("asking", "Sold for $149.99", sale_date_given=True)
    assert kind is PriceKind.ASKING


def test_the_marker_must_be_in_the_quoted_text_not_the_page_furniture():
    """'Sold' in a navigation bar is not evidence that this listing sold, so the
    check runs against the excerpt the price came from."""
    kind, _ = read_price_kind("sold", "$149.99 Free postage", sale_date_given=False)
    assert kind is PriceKind.ASKING


# --- condition -------------------------------------------------------------------


@pytest.mark.parametrize("wording,expected", [
    ("Brand New", ConditionBand.NEW_WITH_TAGS),
    ("New without tags", ConditionBand.NEW_WITHOUT_TAGS),
    ("Open box", ConditionBand.NEW_OTHER),
    ("Seller refurbished", ConditionBand.REFURBISHED),
    ("Used - Like New", ConditionBand.USED_EXCELLENT),
    ("Pre-owned", ConditionBand.USED_GOOD),
    ("Acceptable", ConditionBand.USED_FAIR),
    ("For parts or not working", ConditionBand.FOR_PARTS),
])
def test_seller_wording_maps_onto_the_ladder(wording, expected):
    band, _ = band_for_declared_condition(wording)
    assert band is expected


def test_unrecognised_wording_is_unknown_rather_than_guessed():
    """Unknown stratifies as off-band. Guessing it onto the middle of the ladder
    would put a listing in a sample it does not belong to."""
    band, why = band_for_declared_condition("gently loved, see pics")
    assert band is ConditionBand.UNKNOWN
    assert "rather than guessed" in why


def test_no_condition_stated_is_unknown():
    band, _ = band_for_declared_condition("")
    assert band is ConditionBand.UNKNOWN


# --- the price must be in the quotation --------------------------------------------


def test_a_price_present_in_the_excerpt_is_supported():
    assert price_supported_by(14999, "Sold for $149.99 on 3 Aug")


def test_dollars_alone_are_enough():
    assert price_supported_by(14900, "Price: $149")


def test_a_thousands_separator_does_not_defeat_the_check():
    assert price_supported_by(129900, "Sold for $1,299.00")


def test_an_invented_price_is_not_supported():
    """A real quotation beside a fabricated figure would pass a text-only check and
    poison a distribution."""
    assert not price_supported_by(9999, "Sold for $149.99")


# --- extraction -------------------------------------------------------------------

PAGE = (
    "Beats Pill Speaker Red Sold for $149.99 on 3 Aug. Condition: Pre-owned. "
    "Postage $5.00. Beats Pill Black $189.00 Buy It Now. Condition: Brand New."
)


def extract(listings, page=PAGE):
    return parse_comp_extract_tool_input({"listings": listings}, page_text=page)


def test_a_quoted_listing_is_kept():
    out = extract([{"title": "Beats Pill Red", "price_cents": 14999,
                    "price_state": "sold", "excerpt": "Sold for $149.99 on 3 Aug",
                    "condition_text": "Pre-owned", "shipping_cents": 500}])
    assert len(out.listings) == 1
    assert out.listings[0].shipping_cents == 500


def test_a_listing_whose_price_is_not_in_its_quotation_is_dropped():
    out = extract([{"title": "Beats Pill Red", "price_cents": 8999,
                    "price_state": "sold", "excerpt": "Sold for $149.99 on 3 Aug"}])
    assert out.listings == ()
    assert "does not contain 89.99" in out.malformed[0]


def test_a_fabricated_quotation_is_dropped():
    out = extract([{"title": "x", "price_cents": 14999, "price_state": "sold",
                    "excerpt": "Sold for $149.99 in Manchester"}])
    assert out.listings == ()
    assert "not in the fetched page" in out.malformed[0]


def test_omitted_postage_is_not_recorded_as_zero():
    """`shipping_cents is None` means not reported, and the estimator flags it.
    Zero would silently make a comp look cheaper to the buyer than it is."""
    out = extract([{"title": "Beats Pill Black", "price_cents": 18900,
                    "price_state": "asking", "excerpt": "Beats Pill Black $189.00"}])
    assert out.listings[0].shipping_cents is None


def test_a_page_with_no_listings_is_a_valid_answer():
    out = extract([])
    assert out.listings == ()
    assert out.malformed == []


# --- planning ----------------------------------------------------------------------


def test_a_search_citing_no_observation_is_browsing():
    plan = parse_comp_plan_tool_input(
        {"assessment": {"sufficient": False, "rationale": "need sales"},
         "lookups": [{"query": "beats pill", "seeking": "sold",
                      "motivation": "find sales", "evidence_ids": []}]},
        valid_evidence_ids={1},
    )
    assert plan.lookups == []
    assert "browsing" in plan.malformed[0]


def test_a_repeat_search_is_dropped():
    plan = parse_comp_plan_tool_input(
        {"assessment": {"sufficient": False, "rationale": "more"},
         "lookups": [{"query": "beats pill sold", "seeking": "sold",
                      "motivation": "m", "evidence_ids": [1]}]},
        valid_evidence_ids={1}, already_searched={"beats pill sold"},
    )
    assert plan.lookups == []
    assert "already performed" in plan.malformed[0]


def test_the_planner_can_say_enough():
    plan = parse_comp_plan_tool_input(
        {"assessment": {"sufficient": True, "rationale": "eight sales already"},
         "lookups": []},
        valid_evidence_ids={1},
    )
    assert plan.sufficient
    assert plan.usable


def test_the_split_argument_encoding_is_recovered_here_too():
    payload = {"assessment": {"sufficient": False, "rationale": "need sales"},
               "lookups": [{"query": "q", "seeking": "sold", "motivation": "m",
                            "evidence_ids": [1]}]}
    document = json.dumps(payload)
    broken = {"assessment": document[len('{"assessment": '):]}
    plan = parse_comp_plan_tool_input(broken, valid_evidence_ids={1})
    assert plan.usable
    assert len(plan.lookups) == 1


# --- judging ------------------------------------------------------------------------


def judge(judgements, comp_ids={"comp_1"}, item_ids={88}):
    return parse_comp_judge_tool_input(
        {"judgements": judgements},
        valid_item_evidence=item_ids, valid_comp_ids=comp_ids,
    )


def test_a_judgement_citing_only_the_listing_is_refused():
    """That is a description of a web page, not a comparison."""
    out = judge([{"comp_id": "comp_1", "comparability": "same_family_variant",
                  "item_evidence_ids": [], "comp_fields": ["title"], "rationale": "r"}])
    assert out.judgements == ()
    assert "cites no observation of the item" in out.malformed[0]


def test_a_judgement_naming_no_listing_field_is_refused():
    out = judge([{"comp_id": "comp_1", "comparability": "same_family_variant",
                  "item_evidence_ids": [88], "comp_fields": [], "rationale": "r"}])
    assert out.judgements == ()
    assert "names no field" in out.malformed[0]


def test_an_exclusion_needs_a_reason():
    out = judge([{"comp_id": "comp_1", "comparability": "excluded",
                  "item_evidence_ids": [], "comp_fields": [], "rationale": "r"}])
    assert out.judgements == ()
    assert "must record why" in out.malformed[0]


def test_an_exclusion_with_a_reason_is_kept_without_citations():
    """An excluded comp is a decision about the listing itself -- a bundle, a
    parts unit -- and does not need to be matched to the item to be recorded."""
    out = judge([{"comp_id": "comp_1", "comparability": "excluded",
                  "item_evidence_ids": [], "comp_fields": [], "rationale": "r",
                  "excluded_reason": "a lot of five units"}])
    assert len(out.judgements) == 1


def test_a_judgement_about_an_unretrieved_comp_is_refused():
    out = judge([{"comp_id": "invented", "comparability": "same_product",
                  "item_evidence_ids": [88], "comp_fields": ["title"], "rationale": "r"}])
    assert out.judgements == ()
    assert "not a comp retrieved for this item" in out.malformed[0]


# --- the ladder ceiling is enforced at storage ---------------------------------------


def fixture(tmp_path):
    conn = db.connect(tmp_path / "comps.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    return conn, gateway, sku


def observation(conn, sku, gateway):
    from resell.reasoning.schema import Basis, Observation

    return gateway.record_observation(
        sku, Observation(claim="panel reads A3211", basis=Basis.TEXT_READ,
                         photo_positions=(1,)),
    ).data["evidence_id"]


def test_same_product_is_refused_when_identity_was_never_resolved(tmp_path):
    """Pricing inherits identification's limits, and the refusal lives in storage
    so no caller can route around it."""
    from resell.pricing.comps import CompClaim

    conn, gateway, sku = fixture(tmp_path)
    claim = CompClaim(
        claim_id="claim_1", sku=sku, comp_id="comp_1",
        comparability=Comparability.SAME_PRODUCT,
        item_citations=("88",), comp_citations=("title",), rationale="looks identical",
    )
    with pytest.raises(ValueError, match="requires identity_resolution=resolved"):
        sp.record_comp_claim(conn, claim, identity_resolution="searched_not_found")


# --- eBay is not fetched --------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://www.ebay.com/sch/i.html?_nkw=beats+pill",
    "https://ebay.co.uk/itm/12345",
    "https://www.ebay.de/sch/beats",
])
def test_ebay_is_refused_at_the_fetcher(url):
    """Not a quality judgement but a licensing one, enforced below every adapter so
    a new adapter cannot forget it."""
    permitted, why = fetch_permitted(url)
    assert not permitted
    assert "must not be fetched" in why


def test_the_refusal_survives_robots_being_switched_off():
    """`respect_robots=False` lets an operator name a page they chose to read. It
    is not a way out of an agreement."""
    guard = PageFetcher(respect_robots=False)
    allowed, why = guard.allowed("https://www.ebay.com/itm/1")
    assert not allowed
    assert "must not be fetched" in why


def test_a_permitted_marketplace_is_not_blocked():
    permitted, _ = fetch_permitted("https://poshmark.com/listing/1")
    assert permitted


def test_the_adapter_names_the_alternative_when_it_refuses():
    said: list[str] = []
    adapter = OperatorUrlMarketplaceAdapter(
        prompt=lambda _: next(iter(["https://www.ebay.com/sch/beats"]), ""),
        echo=said.append,
    )
    assert adapter.search(ResearchQuery("beats", "marketplace", "m")) == []
    assert any("comp-add" in line for line in said)


def test_the_marketplace_is_the_host():
    assert marketplace_for_url("https://www.poshmark.com/listing/1") == "poshmark.com"


# --- the round, end to end --------------------------------------------------------


class FakeResponse:
    def __init__(self, body, url="https://poshmark.com/search?q=beats"):
        self.status_code = 200
        self.headers = {"content-type": "text/html"}
        self.content = body
        self.encoding = "utf-8"
        self.url = url

    def iter_bytes(self):
        yield self.content

    def close(self):
        pass


class _Stream:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        return False


class FakeClient:
    """Streams, because the fetcher streams.

    It used to fake `get()` alone, so when the fetcher moved to chunked reads --
    the change that put a real deadline on a stalled transfer -- this double kept
    reporting success while every page silently failed to fetch.
    """

    def __init__(self, response):
        self._response = response

    def stream(self, method, url):
        return _Stream(self._response)

    def close(self):
        pass


BODY = (
    b"<html><body>"
    b"<div>Beats Pill Red. Sold for $149.99 on 3 Aug. Condition: Pre-owned.</div>"
    b"<div>Beats Pill Black. $189.00 Buy It Now. Condition: Brand New.</div>"
    b"</body></html>"
)


class CompModel:
    """Answers each comp stage by tool name."""

    provider, model = "fake", "m"

    def __init__(self, plan, listings, judgements):
        self._plan, self._listings = plan, listings
        # Callable, because comp ids are minted inside the round and a judgement
        # has to name one that exists.
        self._judgements = judgements
        self.calls: list[str] = []

    def estimate_input_tokens(self, request):
        return 1000

    def rates(self):
        from resell.reasoning.budget import ModelRates

        return ModelRates()

    def run(self, request):
        from resell.reasoning.stages import StageResult, Usage

        self.calls.append(request.tool.name)
        payload = {
            "plan_comp_research": self._plan,
            "extract_comps": {"listings": self._listings},
            "judge_comps": {"judgements": (
                self._judgements() if callable(self._judgements) else self._judgements
            )},
        }[request.tool.name]
        return StageResult(
            tool_input=payload, usage=Usage(1000, 300, {}), latency_ms=8,
            provider="fake", model="m", stop_reason="tool_use", raw_response={},
        )


_ACTIVE_CONN = {}


def _prompts(urls):
    """A prompt function that answers with each URL once, then blank."""
    remaining = list(urls)
    return lambda _: remaining.pop(0) if remaining else ""


def _recorded_comp_ids():
    """Comp ids as they stand when the judge stage runs."""
    conn = _ACTIVE_CONN["conn"]
    return [r["comp_id"] for r in conn.execute("SELECT comp_id FROM comp_observation")]


def comp_round(tmp_path, listings, judgements=None, plan=None, visibility="full"):
    from resell.reasoning.budget import LookupBudget, StageBudget
    from resell.reasoning.comp_loop import run_comp_round

    conn, gateway, sku = fixture(tmp_path)
    # Without a recorded policy a source resolves to derived_only, so its rows
    # never reach the judging prompt. Registering one is what these tests are
    # asserting around, so it is explicit rather than assumed.
    if visibility:
        sp.set_source_policy(
            conn, source="poshmark.com", model_visibility=visibility,
            policy_version="test", note="fixture",
        )
    evidence_id = observation(conn, sku, gateway)
    plan = plan or {
        "assessment": {"sufficient": False, "rationale": "no sales on file"},
        "lookups": [{"query": "beats pill sold", "seeking": "sold",
                     "motivation": "find realised sales",
                     "evidence_ids": [evidence_id]}],
    }
    if judgements is None:
        judgements = []
    _ACTIVE_CONN["conn"] = conn
    model = CompModel(plan, listings, judgements)
    adapter = OperatorUrlMarketplaceAdapter(
        fetcher=PageFetcher(client=FakeClient(FakeResponse(BODY)), respect_robots=False),
        prompt=_prompts(["https://poshmark.com/search?q=beats"]),
        echo=lambda *a: None,
    )
    outcome = run_comp_round(
        conn, gateway, sku, model_adapter=model, research_adapter=adapter,
        stage_budget=StageBudget(max_calls=9, max_cost_micros=9_000_000),
        lookup_budget=LookupBudget(scope="pricing", max_lookups=4),
    )
    return conn, sku, outcome, model, evidence_id


SOLD = {"title": "Beats Pill Red", "price_cents": 14999, "price_state": "sold",
        "excerpt": "Sold for $149.99 on 3 Aug", "condition_text": "Pre-owned"}
ASKING = {"title": "Beats Pill Black", "price_cents": 18900, "price_state": "asking",
          "excerpt": "$189.00 Buy It Now", "condition_text": "Brand New"}


def test_a_round_records_comps_and_preserves_the_kind_distinction(tmp_path):
    conn, sku, outcome, model, _ = comp_round(tmp_path, [SOLD, ASKING])
    assert model.calls == ["plan_comp_research", "extract_comps", "judge_comps"]
    assert outcome.comps_recorded == 2
    assert outcome.kinds == {"realized": 1, "asking": 1}

    rows = conn.execute(
        "SELECT price_kind, basis, condition_band, shipping_cents, source_excerpt "
        "FROM comp_observation ORDER BY price_cents"
    ).fetchall()
    assert [r["price_kind"] for r in rows] == ["realized", "asking"]
    # Identity was never resolved, so nothing may be recorded as an exact match.
    assert {r["basis"] for r in rows} == {"sold_similar", "active_similar"}
    assert [r["condition_band"] for r in rows] == ["used_good", "new_with_tags"]
    # Postage was stated for neither listing in the quoted text.
    assert {r["shipping_cents"] for r in rows} == {None}
    assert all(r["source_excerpt"] for r in rows)


def test_an_unsupported_sold_claim_is_downgraded_and_reported(tmp_path):
    """The single most consequential correction the loop makes, so it is visible
    in the outcome rather than buried in a note."""
    liar = dict(ASKING, price_state="sold")
    conn, sku, outcome, _, _ = comp_round(tmp_path, [liar])
    assert outcome.kinds == {"asking": 1}
    assert len(outcome.downgraded) == 1
    assert "no sale marker" in outcome.downgraded[0]


def test_claims_are_recorded_with_citations_on_both_sides(tmp_path):
    conn, sku, outcome, _, evidence_id = comp_round(
        tmp_path, [SOLD], judgements=lambda: [
            {"comp_id": cid, "comparability": "same_family_variant",
             "item_evidence_ids": [1], "comp_fields": ["title", "condition_text"],
             "rationale": "same product line, different colourway"}
            for cid in _recorded_comp_ids()
        ],
    )
    assert outcome.claims_recorded == 1
    assert outcome.ladder == {"same_family_variant": 1}
    row = conn.execute(
        "SELECT item_citations_json, comp_citations_json FROM comp_claim"
    ).fetchone()
    assert json.loads(row["item_citations_json"]) == ["1"]
    assert json.loads(row["comp_citations_json"]) == ["title", "condition_text"]


def test_a_rung_above_the_ceiling_is_refused_at_storage_not_demoted(tmp_path):
    """Identity was never resolved here. Silently dropping the claim a rung would
    make the judgement look considered when it was salvaged."""
    conn, sku, outcome, _, _ = comp_round(
        tmp_path, [SOLD], judgements=lambda: [
            {"comp_id": cid, "comparability": "same_product",
             "item_evidence_ids": [1], "comp_fields": ["title"],
             "rationale": "identical"}
            for cid in _recorded_comp_ids()
        ],
    )
    assert outcome.claims_recorded == 0
    assert len(outcome.refused) == 1
    assert "requires identity_resolution=resolved" in outcome.refused[0]
    assert conn.execute("SELECT COUNT(*) FROM comp_claim").fetchone()[0] == 0


def test_the_round_records_no_price_and_no_proposal(tmp_path):
    """The line this agent must not cross."""
    conn, sku, outcome, _, _ = comp_round(tmp_path, [SOLD, ASKING])
    assert conn.execute("SELECT COUNT(*) FROM price_proposal").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM comp_set").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM item_price_state").fetchone()[0] == 0


def test_the_lookup_is_recorded_under_the_pricing_scope(tmp_path):
    """Identity and pricing budgets are separate, so a hard-to-identify item cannot
    spend the comp allowance."""
    conn, sku, outcome, _, _ = comp_round(tmp_path, [SOLD])
    scopes = [
        r["scope"] for r in conn.execute("SELECT scope FROM research_lookup")
    ]
    assert scopes == ["pricing"]


def test_an_unusable_plan_stops_without_recording(tmp_path):
    conn, sku, outcome, _, _ = comp_round(
        tmp_path, [SOLD], plan={"assessment": "}}not json{{"}
    )
    assert outcome.stopped == "plan_unusable"
    assert conn.execute("SELECT COUNT(*) FROM comp_observation").fetchone()[0] == 0


def test_a_sufficient_plan_stops_cleanly(tmp_path):
    conn, sku, outcome, _, _ = comp_round(
        tmp_path, [SOLD],
        plan={"assessment": {"sufficient": True, "rationale": "enough on file"},
              "lookups": []},
    )
    assert outcome.stopped == "sufficient"
    assert conn.execute("SELECT COUNT(*) FROM comp_observation").fetchone()[0] == 0


def test_the_extractor_is_told_the_search_intent_does_not_settle_the_page(tmp_path):
    """Telling the extractor we wanted sales is how a page of offers becomes a page
    of recorded sales, so the prompt says the opposite explicitly."""
    from resell.reasoning.stages import comp_extraction_stage

    request = comp_extraction_stage("text", "https://x.example", "q", "sold")
    assert "tells you nothing about what this page actually shows" in request.instruction
    assert "`sold` only where the page says so" in request.system_prompt


# --- what a source's licence permits into a prompt -----------------------------------


def test_an_unregistered_source_is_withheld_from_the_prompt(tmp_path):
    """The conservative default. A source nobody has ruled on may still price the
    item -- the estimator is arithmetic -- but its rows do not reach a model."""
    conn, sku, outcome, model, _ = comp_round(tmp_path, [SOLD], visibility=None)
    assert outcome.comps_recorded == 1
    assert outcome.stopped == "nothing_promptable"
    assert len(outcome.withheld_from_model) == 1
    # The judge was never called.
    assert model.calls == ["plan_comp_research", "extract_comps"]


def test_a_withheld_comp_is_still_recorded_and_priceable(tmp_path):
    """Withholding is about prompts, not about storage. `estimate.py` reads these
    rows and never sees a prompt."""
    conn, sku, outcome, _, _ = comp_round(tmp_path, [SOLD, ASKING], visibility=None)
    rows = conn.execute(
        "SELECT price_cents, model_visibility FROM comp_observation"
    ).fetchall()
    assert len(rows) == 2
    assert {r["model_visibility"] for r in rows} == {"derived_only"}


def test_a_derived_only_source_is_withheld_even_when_registered(tmp_path):
    conn, sku, outcome, model, _ = comp_round(
        tmp_path, [SOLD], visibility="derived_only"
    )
    assert outcome.stopped == "nothing_promptable"
    assert "yours to record with `price claim`" in outcome.stop_reason


def test_promptable_comps_split_by_visibility(tmp_path):
    conn, sku, _, _, _ = comp_round(tmp_path, [SOLD], visibility="full")
    promptable, withheld = sp.promptable_comps(conn, sku)
    assert len(promptable) + len(withheld) == conn.execute(
        "SELECT COUNT(*) FROM comp_claim"
    ).fetchone()[0]


def test_the_policy_defaults_to_the_strictest_answer(tmp_path):
    conn, _, _ = fixture(tmp_path)
    from resell.pricing.comps import ModelVisibility

    assert sp.visibility_for_source(conn, "never-heard-of-it.example") is (
        ModelVisibility.DERIVED_ONLY
    )


def test_a_recorded_policy_is_readable_back(tmp_path):
    conn, _, _ = fixture(tmp_path)
    sp.set_source_policy(
        conn, source="ebay_marketplace_insights", model_visibility="derived_only",
        policy_version="2025-06-24", licence_ref="eBay API License Agreement",
        note="Restricted API; rows must not enter a prompt",
    )
    policy = sp.source_policy(conn, "ebay_marketplace_insights")
    assert policy["model_visibility"] == "derived_only"
    assert policy["licence_ref"] == "eBay API License Agreement"


def test_an_unknown_visibility_is_refused(tmp_path):
    conn, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="unknown model_visibility"):
        sp.set_source_policy(
            conn, source="x", model_visibility="whatever", policy_version="1",
        )


# --- supplied pages are read once, not once per planner query --------------------


def test_supplied_pages_are_read_once_across_the_whole_round():
    """The bug: the planner proposed four queries, the adapter returned the same
    pasted page for each, and four extraction calls on identical text exhausted
    the stage's three-call budget on duplicated work."""
    from resell.reasoning.adapters.marketplace import SuppliedUrlsMarketplaceAdapter

    fetched: list[str] = []

    class CountingFetcher:
        def fetch(self, url):
            fetched.append(url)
            raise __import__(
                "resell.reasoning.adapters.fetch", fromlist=["FetchError"]
            ).FetchError("not reachable in a test")

    adapter = SuppliedUrlsMarketplaceAdapter(
        ["https://poshmark.com/a", "https://poshmark.com/b"],
        fetcher=CountingFetcher(), echo=lambda *a: None,
    )
    for query in ("first", "second", "third", "fourth"):
        adapter.search(ResearchQuery(query, "marketplace", "m"))

    assert fetched == ["https://poshmark.com/a", "https://poshmark.com/b"]


def test_a_later_query_gets_nothing_rather_than_a_repeat():
    from resell.reasoning.adapters.marketplace import SuppliedUrlsMarketplaceAdapter

    adapter = SuppliedUrlsMarketplaceAdapter(
        ["https://poshmark.com/a"],
        fetcher=PageFetcher(client=FakeClient(FakeResponse(BODY)), respect_robots=False),
        echo=lambda *a: None,
    )
    first = adapter.search(ResearchQuery("q1", "marketplace", "m"))
    second = adapter.search(ResearchQuery("q2", "marketplace", "m"))
    assert len(first) == 1
    assert second == []


def test_the_round_extracts_each_supplied_page_once(tmp_path):
    """End to end: several planned queries, one page, one extraction call."""
    from resell.reasoning.adapters.marketplace import SuppliedUrlsMarketplaceAdapter
    from resell.reasoning.budget import LookupBudget, StageBudget
    from resell.reasoning.comp_loop import run_comp_round

    conn, gateway, sku = fixture(tmp_path)
    sp.set_source_policy(
        conn, source="poshmark.com", model_visibility="full", policy_version="test",
    )
    evidence_id = observation(conn, sku, gateway)
    plan = {
        "assessment": {"sufficient": False, "rationale": "need sales"},
        "lookups": [
            {"query": f"query {n}", "seeking": "sold", "motivation": "m",
             "evidence_ids": [evidence_id]}
            for n in range(4)
        ],
    }
    model = CompModel(plan, [SOLD], [])
    adapter = SuppliedUrlsMarketplaceAdapter(
        ["https://poshmark.com/a"],
        fetcher=PageFetcher(client=FakeClient(FakeResponse(BODY)), respect_robots=False),
        echo=lambda *a: None,
    )
    _ACTIVE_CONN["conn"] = conn
    run_comp_round(
        conn, gateway, sku, model_adapter=model, research_adapter=adapter,
        stage_budget=StageBudget(max_calls=3, max_cost_micros=9_000_000),
        lookup_budget=LookupBudget(scope="pricing", max_lookups=9),
        propose_only=True,
    )
    extractions = model.calls.count("extract_comps")
    assert extractions == 1, model.calls


# --- discovery without an operator ------------------------------------------------


class StubBackend:
    provider = "stub"

    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def cost_micros_per_search(self):
        return 5000

    def find(self, query, *, limit=10):
        self.queries.append(query.query)
        return self.hits


def _hit(url, price=None, title="Beats Pill A3211 Speaker"):
    from resell.reasoning.adapters.search import SearchHit

    return SearchHit(url=url, title=title, price_cents=price, query="beats pill")


def test_an_unfetchable_host_still_yields_an_asking_comp():
    """The whole point of the policy change. eBay is not fetched and its prices
    are still the best asking evidence available."""
    from resell.reasoning.adapters.marketplace import SearchedMarketplaceAdapter
    from resell.reasoning.adapters.research import ResearchQuery

    backend = StubBackend([_hit("https://www.ebay.com/itm/111", 7800)])
    adapter = SearchedMarketplaceAdapter(
        backend, identity_terms=("Beats", "Pill"), echo=lambda *a: None,
    )
    documents = adapter.search(ResearchQuery("beats pill", "marketplace", "why"))
    assert documents == []                      # nothing was fetched
    comps = adapter.take_direct_comps()
    assert [c.price_cents for c in comps] == [7800]
    assert comps[0].marketplace == "ebay.com"


def test_ebay_is_still_never_fetched():
    """The block stays below every adapter. A search that returns eBay results --
    which is most of them -- must not become a way around it."""
    from resell.reasoning.adapters.marketplace import SearchedMarketplaceAdapter
    from resell.reasoning.adapters.research import ResearchQuery

    fetched = []

    class LoudFetcher:
        def fetch(self, url):
            fetched.append(url)
            raise AssertionError(f"fetched a forbidden host: {url}")

    backend = StubBackend([_hit("https://www.ebay.com/itm/111", 7800)])
    adapter = SearchedMarketplaceAdapter(
        backend, identity_terms=("Beats", "Pill"), fetcher=LoudFetcher(),
        echo=lambda *a: None,
    )
    adapter.search(ResearchQuery("beats pill", "marketplace", "why"))
    assert fetched == []


def test_the_comps_are_drained_so_a_second_query_does_not_repeat_them():
    """Each query records its own comps. Accumulating would write every earlier
    query's findings again on the next lookup."""
    from resell.reasoning.adapters.marketplace import SearchedMarketplaceAdapter
    from resell.reasoning.adapters.research import ResearchQuery

    backend = StubBackend([_hit("https://www.ebay.com/itm/111", 7800)])
    adapter = SearchedMarketplaceAdapter(
        backend, identity_terms=("Beats", "Pill"), echo=lambda *a: None,
    )
    adapter.search(ResearchQuery("q1", "marketplace", "why"))
    assert len(adapter.take_direct_comps()) == 1
    assert adapter.take_direct_comps() == []


def test_the_lookup_price_is_the_backends():
    from resell.reasoning.adapters.marketplace import SearchedMarketplaceAdapter

    adapter = SearchedMarketplaceAdapter(StubBackend([]), echo=lambda *a: None)
    assert adapter.cost_micros_per_lookup() == 5000


def test_identification_research_skips_hosts_it_may_not_fetch():
    """A general web search returns eBay constantly; a refusal per result would be
    the loudest thing in the log."""
    from resell.reasoning.adapters.research import ResearchQuery
    from resell.reasoning.adapters.web import SearchedResearchAdapter

    fetched = []

    class Fetcher:
        def fetch(self, url):
            fetched.append(url)
            raise FetchErrorStub(url)

    class FetchErrorStub(Exception):
        pass

    backend = StubBackend([
        _hit("https://www.ebay.com/itm/111"),
        _hit("https://support.apple.com/beats-pill"),
    ])
    adapter = SearchedResearchAdapter(
        backend, conn=object(), sku="MP-000001", model_adapter=object(),
        fetcher=Fetcher(), echo=lambda *a: None,
    )
    try:
        adapter.search(ResearchQuery("beats pill", "manufacturer", "why"))
    except Exception:
        pass
    assert not any("ebay.com" in u for u in fetched)


def test_the_same_listing_across_two_queries_is_recorded_once():
    """A live round died on the UNIQUE constraint here. The planner proposes
    several overlapping searches and the same listing is in most of them, so
    deduping per query wrote it once per query -- and the crash came after half
    the round's findings were already committed."""
    from resell.reasoning.adapters.marketplace import SearchedMarketplaceAdapter
    from resell.reasoning.adapters.research import ResearchQuery

    backend = StubBackend([_hit("https://www.ebay.com/itm/111", 7800)])
    adapter = SearchedMarketplaceAdapter(
        backend, identity_terms=("Beats", "Pill"), echo=lambda *a: None,
    )
    adapter.search(ResearchQuery("first query", "marketplace", "why"))
    first = adapter.take_direct_comps()
    adapter.search(ResearchQuery("second query", "marketplace", "why"))
    second = adapter.take_direct_comps()

    assert len(first) == 1
    assert second == []


# --- the maker's own price is not a comp ------------------------------------------


def test_the_brands_own_site_is_retail_not_an_ask():
    """One autonomous round recorded 24 prices from bowflex.com as asking comps
    for a used Bowflex -- the manufacturer's retail list sitting in the sample as
    though somebody were offering second-hand dumbbells at $699. An operator
    choosing URLs never pasted the maker's store; a search backend goes there
    constantly."""
    from resell.pricing.comps import PriceKind, RetailKind
    from resell.reasoning.comp_reading import retail_from_source
    from resell.reasoning.research import SourceAuthority

    kind, retail, why = retail_from_source(
        PriceKind.ASKING, "bowflex.com", "Bowflex", SourceAuthority.UNKNOWN
    )
    assert kind is PriceKind.REFERENCE
    assert retail is RetailKind.CURRENT
    assert "costs new" in why


def test_a_marketplace_ask_is_untouched():
    from resell.pricing.comps import PriceKind
    from resell.reasoning.comp_reading import retail_from_source
    from resell.reasoning.research import SourceAuthority

    kind, retail, _ = retail_from_source(
        PriceKind.ASKING, "craigslist.org", "Bowflex", SourceAuthority.UNKNOWN
    )
    assert kind is PriceKind.ASKING
    assert retail is None


def test_a_recorded_sale_stays_a_sale():
    """Where something sold does not turn a sale into a list price."""
    from resell.pricing.comps import PriceKind
    from resell.reasoning.comp_reading import retail_from_source
    from resell.reasoning.research import SourceAuthority

    kind, retail, _ = retail_from_source(
        PriceKind.REALIZED, "bowflex.com", "Bowflex", SourceAuthority.UNKNOWN
    )
    assert kind is PriceKind.REALIZED
    assert retail is None


def test_a_brand_does_not_claim_a_domain_that_merely_contains_it():
    """Prefix, not substring: "Apple" must not claim pineapple.com."""
    from resell.reasoning.comp_reading import makers_own_site
    from resell.reasoning.research import SourceAuthority

    assert not makers_own_site("pineapple.com", "Apple", SourceAuthority.UNKNOWN)
    assert makers_own_site("beatsbydre.com", "Beats", SourceAuthority.UNKNOWN)


def test_the_authority_table_still_wins_where_it_has_an_opinion():
    """It covers stores that do not share the brand's name."""
    from resell.reasoning.comp_reading import makers_own_site
    from resell.reasoning.research import SourceAuthority

    assert makers_own_site("support.apple.com", "Beats", SourceAuthority.MANUFACTURER)


def test_an_unbranded_item_claims_nothing():
    from resell.reasoning.comp_reading import makers_own_site
    from resell.reasoning.research import SourceAuthority

    assert not makers_own_site("bowflex.com", None, SourceAuthority.UNKNOWN)
    assert not makers_own_site("bowflex.com", "", SourceAuthority.UNKNOWN)


def test_one_unreadable_page_does_not_lose_the_round():
    """A page that stalls must cost that page, not everything already retrieved."""
    from resell.reasoning.adapters.marketplace import SearchedMarketplaceAdapter
    from resell.reasoning.adapters.research import ResearchQuery

    class HalfBrokenFetcher:
        def __init__(self):
            self.seen = []

        def fetch(self, url):
            from resell.reasoning.adapters.fetch import FetchError

            self.seen.append(url)
            raise FetchError(f"gave up after 45.0s with 812 bytes read from {url}")

    backend = StubBackend([
        _hit("https://slow.example/a", 7800),
        _hit("https://www.ebay.com/itm/111", 8100),
    ])
    adapter = SearchedMarketplaceAdapter(
        backend, identity_terms=("Beats", "Pill"), fetcher=HalfBrokenFetcher(),
        echo=lambda *a: None,
    )
    documents = adapter.search(ResearchQuery("beats pill", "marketplace", "why"))

    # the fetchable page failed, but the search-index observation survived
    assert documents == []
    assert [c.price_cents for c in adapter.take_direct_comps()] == [8100]
    assert any("slow.example/a" in n for n in adapter.notes)


def test_the_failing_url_reaches_the_rounds_notes():
    """The note existed on the adapter and was never read again, so a host that
    reliably stalls was invisible to whoever had to diagnose it."""
    from resell.reasoning.comp_loop import _drain_adapter_notes

    class Outcome:
        notes: list = []

    class Adapter:
        notes = ["https://slow.example/a: gave up after 45.0s"]

    outcome, adapter = Outcome(), Adapter()
    outcome.notes = []
    _drain_adapter_notes(adapter, outcome)

    assert any("slow.example/a" in n for n in outcome.notes)
    assert adapter.notes == []      # drained, so it is not repeated next lookup


# --- a spent extraction budget must not discard the round -------------------------


def test_a_budget_stop_mid_round_keeps_what_was_already_extracted(tmp_path):
    """The Canon T6i failure, exactly. Three rounds planned, searched and
    extracted real listings at $465-$1097, and recorded zero comps: observations
    are written only after every query, so the BudgetExceeded raised by a later
    extraction propagated out of the round and took the earlier ones with it.

    Everything retrieved before the stop was already paid for. It stays, it is
    judged, and the round ends with candidates instead of nothing."""
    from resell.reasoning.adapters.marketplace import SuppliedUrlsMarketplaceAdapter
    from resell.reasoning.budget import LookupBudget, StageBudget
    from resell.reasoning.comp_loop import run_comp_round

    conn, gateway, sku = fixture(tmp_path)
    sp.set_source_policy(
        conn, source="poshmark.com", model_visibility="full", policy_version="test",
    )
    evidence_id = observation(conn, sku, gateway)
    plan = {
        "assessment": {"sufficient": False, "rationale": "need sales"},
        "lookups": [
            {"query": f"query {n}", "seeking": "sold", "motivation": "m",
             "evidence_ids": [evidence_id]}
            for n in range(3)
        ],
    }
    model = CompModel(plan, [SOLD], [])
    adapter = SuppliedUrlsMarketplaceAdapter(
        ["https://poshmark.com/a", "https://poshmark.com/b", "https://poshmark.com/c"],
        fetcher=PageFetcher(client=FakeClient(FakeResponse(BODY)), respect_robots=False),
        echo=lambda *a: None,
    )
    _ACTIVE_CONN["conn"] = conn
    # Two calls: one plan, one extraction. The second extraction is refused, which
    # is what used to destroy the round.
    outcome = run_comp_round(
        conn, gateway, sku, model_adapter=model, research_adapter=adapter,
        stage_budget=StageBudget(max_calls=2, max_cost_micros=9_000_000),
        lookup_budget=LookupBudget(scope="pricing", max_lookups=9),
        propose_only=True,
    )

    assert outcome.stopped_early
    assert outcome.comps_recorded > 0, "the round discarded what it had already read"
    assert any("extraction stopped" in n for n in outcome.notes)
    assert conn.execute("SELECT COUNT(*) FROM comp_observation").fetchone()[0] > 0


def test_it_stops_searching_once_it_cannot_read_what_it_finds(tmp_path):
    """Spending lookups to fetch pages nothing can extract is money for nothing."""
    from resell.reasoning.adapters.marketplace import SuppliedUrlsMarketplaceAdapter
    from resell.reasoning.budget import LookupBudget, StageBudget
    from resell.reasoning.comp_loop import run_comp_round

    conn, gateway, sku = fixture(tmp_path)
    sp.set_source_policy(
        conn, source="poshmark.com", model_visibility="full", policy_version="test",
    )
    evidence_id = observation(conn, sku, gateway)
    plan = {
        "assessment": {"sufficient": False, "rationale": "need sales"},
        "lookups": [
            {"query": f"query {n}", "seeking": "sold", "motivation": "m",
             "evidence_ids": [evidence_id]}
            for n in range(5)
        ],
    }
    from resell.reasoning.adapters.marketplace import MarketplaceDocument
    from resell.reasoning.research import SourceAuthority

    class EndlessPages:
        """A backend with more results than the budget can read."""

        provider = "endless"

        def cost_micros_per_lookup(self):
            return 0

        def search(self, query):
            return [MarketplaceDocument(
                url=f"https://poshmark.com/{query.query.replace(' ', '-')}",
                marketplace="poshmark.com", authority=SourceAuthority.RESELLER,
                page_text=BODY.decode(), title="listings",
            )]

    model = CompModel(plan, [SOLD], [])
    _ACTIVE_CONN["conn"] = conn
    outcome = run_comp_round(
        conn, gateway, sku, model_adapter=model, research_adapter=EndlessPages(),
        stage_budget=StageBudget(max_calls=2, max_cost_micros=9_000_000),
        lookup_budget=LookupBudget(scope="pricing", max_lookups=9),
        propose_only=True,
    )
    assert len(outcome.performed) < 5, "kept searching for pages it could not read"
    assert any("stopped searching" in n for n in outcome.notes)


# --- a licence keeps a comp out of a prompt, not out of the workflow ---------------

_NOW = __import__("datetime").datetime(2026, 8, 23, tzinfo=__import__("datetime").UTC)


def withheld_round(tmp_path, *, also_promptable: bool):
    """A round whose best comp comes from an unregistered source.

    MP-000013 exactly: a $399.99 pair of the right dumbbells from a shop nobody
    had registered, alongside eBay replacement weight plates from a source that
    was registered. Only the plates could be judged, the judge correctly excluded
    them, and the item ended with nothing.
    """
    from resell.pricing.comps import (
        CompBasis, Comparability, CompObservation, ModelVisibility, PriceKind,
    )
    from resell.reasoning.comp_loop import CompRoundOutcome, _offer_withheld

    conn, gateway, sku = fixture(tmp_path)
    good = CompObservation(
        comp_id="c-shop", marketplace="citywideshop.com", external_id="1",
        price_kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
        price_cents=39999, observed_at=_NOW,
        title="Bowflex SelectTech 552 Adjustable Dumbbells (Pair)",
        # What `visibility_for_source` gives an unregistered shop, which is the
        # whole reason this comp never reached the judge.
        model_visibility=ModelVisibility.DERIVED_ONLY,
    )
    sp.record_comp_observation(conn, good)
    outcome = CompRoundOutcome()
    _offer_withheld(conn, sku, [good], Comparability.SAME_FAMILY_VARIANT,
                    outcome, True)
    return conn, gateway, sku, outcome


def test_a_withheld_comp_is_offered_to_the_operator(tmp_path):
    """`derived_only` means the rows must not enter a model prompt. It was never
    meant to mean invisible."""
    conn, gateway, sku, outcome = withheld_round(tmp_path, also_promptable=True)

    pending = sp.pending_comp_candidates(conn, sku)
    assert [c["comp_id"] for c in pending] == ["c-shop"]
    assert outcome.candidates_offered == 1


def test_the_offer_says_the_agent_did_not_judge_it(tmp_path):
    """It has not been allowed to look, and a candidate that implied otherwise
    would be the agent taking credit for a judgement it never made."""
    conn, gateway, sku, outcome = withheld_round(tmp_path, also_promptable=True)

    rationale = sp.pending_comp_candidates(conn, sku)[0]["rationale"]
    assert "not assessed by the agent" in rationale
    assert "citywideshop.com" in rationale


def test_it_is_offered_at_the_identity_ceiling_not_above(tmp_path):
    """An unjudged comp must not arrive claiming to be the same product."""
    conn, gateway, sku, outcome = withheld_round(tmp_path, also_promptable=True)

    proposed = sp.pending_comp_candidates(conn, sku)[0]["proposed_comparability"]
    assert proposed == "same_family_variant"


def test_the_rows_still_never_enter_a_prompt(tmp_path):
    """The licence rule is untouched: offering a listing to a person is not the
    same as putting it in a model's context."""
    from resell.pricing.comps import ModelVisibility

    conn, gateway, sku, outcome = withheld_round(tmp_path, also_promptable=True)
    stored = conn.execute(
        "SELECT model_visibility FROM comp_observation WHERE comp_id = 'c-shop'"
    ).fetchone()[0]
    assert stored != str(ModelVisibility.FULL)


def test_nothing_is_offered_when_the_round_records_claims_itself(tmp_path):
    """`propose_only=False` means the agent is claiming directly; offering the
    same comps for review as well would double them."""
    from resell.pricing.comps import CompBasis, CompObservation, Comparability, PriceKind
    from resell.reasoning.comp_loop import CompRoundOutcome, _offer_withheld

    conn, gateway, sku = fixture(tmp_path)
    obs = CompObservation(
        comp_id="c-x", marketplace="shop.example", external_id="1",
        price_kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
        price_cents=1000, observed_at=_NOW,
    )
    sp.record_comp_observation(conn, obs)
    outcome = CompRoundOutcome()
    offered = _offer_withheld(
        conn, sku, [obs], Comparability.SAME_FAMILY_VARIANT, outcome, False
    )
    assert offered == 0


# --- the agent judges its own comparables ------------------------------------------


def test_the_orchestrator_records_claims_rather_than_offering_them():
    """The judging stage was already doing the work -- it excluded replacement
    weight plates and parts listings correctly every time -- and the operator was
    ratifying a decision made with better information than they had."""
    import inspect

    from resell.orchestrator import StageRunner

    source = inspect.getsource(StageRunner._comp_research)
    assert "propose_only=False" in source


def test_the_price_and_the_listing_still_need_approving():
    """What moved is a matter of fact about objects. What did not move are the
    two decisions about what to charge and what to say."""
    from resell.orchestrator import Actor, Step, StageRunner

    for step in (Step.APPROVE_PRICE, Step.APPROVE_LISTING, Step.PUBLISH):
        assert not hasattr(StageRunner, f"_{step}"), step


def test_an_existing_review_queue_can_still_be_cleared(tmp_path):
    """Items that already have candidates must not be stranded by the change."""
    from resell.orchestrator import Step, next_step
    from resell.pricing.comps import CompBasis, CompObservation, PriceKind

    conn, gateway, sku = fixture(tmp_path)
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="c" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    observation(conn, sku, gateway)
    gateway.propose_identification(
        sku, category_id="137865", condition_id="USED_GOOD",
        title="A thing", description="A thing.", aspects={"Brand": ["Bowflex"]},
    )
    gateway.begin_pricing(sku)
    sp.record_comp_observation(conn, CompObservation(
        comp_id="c-old", marketplace="ebay.com", external_id="1",
        price_kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
        price_cents=25000, observed_at=_NOW,
    ))
    sp.record_comp_candidate(
        conn, sku=sku, comp_id="c-old", proposed_comparability="same_family_variant",
        item_citations=("1",), comp_citations=("title",), rationale="left over",
    )
    assert next_step(conn, sku).step is Step.REVIEW_COMPS


# --- the gate that was throwing eBay away ------------------------------------------


def test_a_generic_type_word_is_not_an_identifier(tmp_path):
    """MP-000022 had two terms, "Bowflex" and "Adjustable", and the gate demanded
    both. Every "Bowflex SelectTech 552 Dumbbells" was discarded for lacking the
    second word: 22 of 31 priced listings on one search."""
    from resell import views

    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(
        sku, aspects={"Brand": ["Bowflex"], "Type": ["Adjustable"]},
    )
    assert views.identity_terms(conn, sku) == ("Bowflex",)


def test_an_item_identified_by_title_still_has_terms(tmp_path):
    """A book has no brand, and without these it had no terms at all -- and so no
    gate whatsoever."""
    from resell import views

    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(
        sku, aspects={"Book Title": ["The House on Mango Street"],
                      "Author": ["Sandra Cisneros"], "Format": ["Paperback"]},
    )
    terms = views.identity_terms(conn, sku)
    assert "The House on Mango Street" in terms
    assert "Paperback" not in terms


def test_one_distinctive_term_is_enough():
    """Deliberately permissive: this removes what is certainly a different
    product, and the judge decides what is comparable."""
    from resell.reasoning.adapters.search import MIN_IDENTITY_TERMS, _matches_identity

    assert MIN_IDENTITY_TERMS == 1
    assert _matches_identity("Bowflex SelectTech 552 Dumbbells", ("Bowflex",))
    assert not _matches_identity("Nike Air Max 90 Trainers", ("Bowflex",))
