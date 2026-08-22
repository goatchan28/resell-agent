"""Automated retrieval: fetch the page, read it, quote it.

The manual adapter makes the operator the witness to what a page said. This path
has no witness, so the properties worth testing are the ones that replace one:
authority comes from the URL and not from the model, and every extracted fact
quotes text that was actually fetched.
"""

from __future__ import annotations

import pytest

from resell import db
from resell.domain import FeeModel
from resell.gateway import Gateway, candidate_evidence
from resell.reasoning.adapters.fetch import FetchError, PageFetcher, html_to_text
from resell.reasoning.adapters.research import ResearchQuery, RetrievalMethod
from resell.reasoning.authority import authority_for_url
from resell.reasoning.research import FactDomain, SourceAuthority
from resell.reasoning.tools import parse_extract_tool_input


# --- authority is a fact about the URL -----------------------------------------


def test_a_listed_domain_carries_its_authority():
    authority, _ = authority_for_url("https://beatsbydre.com/pill")
    assert authority is SourceAuthority.MANUFACTURER


def test_a_subdomain_inherits_from_its_parent():
    authority, why = authority_for_url("https://www.shop.beatsbydre.com/pill")
    assert authority is SourceAuthority.MANUFACTURER
    assert "via beatsbydre.com" in why


def test_an_unlisted_domain_donates_nothing():
    """Fails closed. The alternative is a heuristic that guesses upward."""
    authority, why = authority_for_url("https://some-blog.example/beats-review")
    assert authority is SourceAuthority.UNKNOWN
    assert "not in the authority table" in why


def test_a_lookalike_domain_does_not_inherit():
    """Matching walks up the labels, never down: apple.com.evil.example is a
    different registrable domain and is exactly the shape a spoof takes."""
    authority, _ = authority_for_url("https://apple.com.evil.example/p")
    assert authority is SourceAuthority.UNKNOWN


def test_a_reseller_is_recorded_as_one():
    authority, _ = authority_for_url("https://www.amazon.com/dp/B0000")
    assert authority is SourceAuthority.RESELLER


def test_ebay_carries_no_authority():
    """No adapter fetches it and none asserts authority for it, so it resolves to
    UNKNOWN like any unlisted host."""
    authority, _ = authority_for_url("https://www.ebay.com/itm/110590229841")
    assert authority is SourceAuthority.UNKNOWN


def test_a_url_with_no_host_is_unknown():
    authority, why = authority_for_url("not-a-url")
    assert authority is SourceAuthority.UNKNOWN
    assert "no host" in why


# --- html to text ---------------------------------------------------------------


def test_script_and_style_contents_are_dropped():
    """A product page embeds its whole catalogue as JSON in a script tag; feeding
    that to the extractor buries the visible specification in machine noise."""
    text, _ = html_to_text(
        "<html><head><style>.a{color:red}</style>"
        '<script>var catalog={"sku":"zzz"}</script></head>'
        "<body><p>Colourway: Navy</p></body></html>"
    )
    assert "Colourway: Navy" in text
    assert "catalog" not in text
    assert "color:red" not in text


def test_tag_boundaries_keep_fields_apart():
    """Two table cells must not become one word, or an excerpt check on either
    would fail against text the page really does contain."""
    text, _ = html_to_text("<table><tr><td>Navy</td><td>Wool</td></tr></table>")
    assert "NavyWool" not in text
    assert "Navy" in text and "Wool" in text


def test_the_title_is_returned_separately():
    _, title = html_to_text("<html><head><title>Beats Pill</title></head><body>x</body></html>")
    assert title == "Beats Pill"


def test_broken_markup_yields_what_it_can():
    text, _ = html_to_text("<p>Navy <b>wool</p></b><div>unclosed")
    assert "Navy" in text and "unclosed" in text


# --- the fetcher ----------------------------------------------------------------


class FakeResponse:
    def __init__(self, body=b"<p>hi</p>", status=200, content_type="text/html", url=None):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.content = body
        self.encoding = "utf-8"
        self.url = url or "https://beatsbydre.com/p"


class FakeClient:
    def __init__(self, response=None, error=None):
        self._response = response or FakeResponse()
        self._error = error
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        if self._error:
            raise self._error
        return self._response

    def close(self):
        pass


def fetcher(response=None, error=None, **kw):
    return PageFetcher(
        client=FakeClient(response, error), respect_robots=False, **kw
    )


def test_a_page_is_fetched_and_reduced_to_text():
    page = fetcher(FakeResponse(b"<html><body><p>Colourway: Navy</p></body></html>")).fetch(
        "https://beatsbydre.com/p"
    )
    assert page.status_code == 200
    assert "Colourway: Navy" in page.text


def test_a_non_html_content_type_is_refused_before_reading():
    with pytest.raises(FetchError, match="not a page"):
        fetcher(FakeResponse(b"%PDF-1.4", content_type="application/pdf")).fetch(
            "https://beatsbydre.com/manual.pdf"
        )


def test_an_error_status_is_a_fetch_error():
    with pytest.raises(FetchError, match="HTTP 404"):
        fetcher(FakeResponse(status=404)).fetch("https://beatsbydre.com/gone")


def test_a_transport_failure_does_not_leak_the_vendor_exception():
    with pytest.raises(FetchError, match="RuntimeError"):
        fetcher(error=RuntimeError("connection reset")).fetch("https://beatsbydre.com/p")


def test_a_non_http_scheme_is_refused():
    with pytest.raises(FetchError, match="not a fetchable scheme"):
        fetcher().fetch("file:///etc/passwd")


def test_an_oversized_body_is_truncated_rather_than_read_whole():
    body = b"<p>" + b"x" * 5000 + b"</p>"
    page = fetcher(FakeResponse(body), max_bytes=500).fetch("https://beatsbydre.com/p")
    assert page.truncated
    assert page.bytes_read == 500


def test_a_redirect_is_reported_so_authority_follows_the_destination():
    page = fetcher(
        FakeResponse(url="https://elsewhere.example/p")
    ).fetch("https://beatsbydre.com/p")
    assert page.redirected
    assert page.final_url == "https://elsewhere.example/p"
    # And the authority of where we landed, not of what was typed.
    assert authority_for_url(page.final_url)[0] is SourceAuthority.UNKNOWN


def test_an_unreadable_robots_file_leaves_the_page_alone():
    """Unreadable is not permission. Proceeding anyway would make the setting
    decorative."""
    guard = PageFetcher(client=FakeClient(), respect_robots=True)
    guard._robots["https://beatsbydre.com"] = None
    allowed, why = guard.allowed("https://beatsbydre.com/p")
    assert allowed is False
    assert "could not be read" in why


def test_robots_disallow_is_honoured():
    from urllib.robotparser import RobotFileParser

    parser = RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /private"])
    guard = PageFetcher(client=FakeClient(), respect_robots=True)
    guard._robots["https://beatsbydre.com"] = parser
    assert guard.allowed("https://beatsbydre.com/private/x")[0] is False
    assert guard.allowed("https://beatsbydre.com/public/x")[0] is True


# --- the extraction parser -------------------------------------------------------

PAGE = (
    "Beats Pill Portable Speaker. Colourway: Statement Red. "
    "Model number A3211. Price $149.99. Weight 680 g."
)


def extract(facts, page=PAGE, title="Beats Pill"):
    return parse_extract_tool_input(
        {"product_title": title, "facts": facts}, page_text=page
    )


def test_a_quoted_fact_is_kept():
    out = extract([{"claim": "Colourway: Statement Red", "domain": "identity",
                    "excerpt": "Colourway: Statement Red"}])
    assert out.facts == (("Colourway: Statement Red", "identity",
                          "Colourway: Statement Red"),)
    assert out.malformed == []


def test_a_fact_with_no_excerpt_is_dropped():
    out = extract([{"claim": "Made in China", "domain": "identity", "excerpt": ""}])
    assert out.facts == ()
    assert "has no support" in out.malformed[0]


def test_a_fabricated_quotation_is_dropped():
    """The whole reason the excerpt is worth storing. Without this check the field
    is just more model output, and an invented quotation looks like a real one."""
    out = extract([{"claim": "Bluetooth 5.3", "domain": "identity",
                    "excerpt": "Supports Bluetooth 5.3"}])
    assert out.facts == ()
    assert "does not appear in the fetched page" in out.malformed[0]


def test_whitespace_differences_do_not_reject_a_real_quotation():
    """HTML-to-text collapses line breaks unpredictably; a quotation differing only
    in spacing is still the page's own words."""
    out = extract([{"claim": "Model A3211", "domain": "identity",
                    "excerpt": "Model   number\n  A3211"}])
    assert len(out.facts) == 1


def test_an_unknown_domain_becomes_retail_rather_than_identity():
    """Failing toward retail means the fact goes unused. Failing toward identity
    would let it be cited by an aspect."""
    out = extract([{"claim": "Price $149.99", "domain": "invented",
                    "excerpt": "Price $149.99"}])
    assert out.facts[0][1] == "retail"
    assert "unknown domain" in out.malformed[0]


def test_a_page_describing_no_product_is_a_valid_answer():
    out = extract([])
    assert out.facts == ()
    assert out.malformed == []


def test_a_string_payload_is_recovered_rather_than_crashing():
    import json

    out = parse_extract_tool_input(
        json.dumps({"product_title": "x", "facts": [
            {"claim": "Model A3211", "domain": "identity", "excerpt": "Model number A3211"}
        ]}),
        page_text=PAGE,
    )
    assert len(out.facts) == 1


def test_an_unusable_payload_reports_rather_than_raises():
    out = parse_extract_tool_input(["not", "an", "object"], page_text=PAGE)
    assert out.facts == ()
    assert "expected an object" in out.malformed[0]


# --- the excerpt reaches storage --------------------------------------------------


def fixture(tmp_path):
    conn = db.connect(tmp_path / "research.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    return conn, gateway, sku


def test_the_excerpt_is_stored_with_the_fact(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.record_candidate_facts(
        sku, candidate_ref="cand-1", source_url="https://beatsbydre.com/p",
        authority="manufacturer",
        facts=[("Colourway: Statement Red", "identity", "Colourway: Statement Red")],
    )
    [row] = candidate_evidence(conn, sku)
    assert row["source_excerpt"] == "Colourway: Statement Red"


def test_a_transcription_stores_no_excerpt(tmp_path):
    """The operator is the witness; there is no quotation to keep, and the absence
    is the honest record rather than missing data."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.record_candidate_facts(
        sku, candidate_ref="cand-1", source_url="https://beatsbydre.com/p",
        authority="manufacturer", facts=[("Colourway: Navy", "identity")],
        retrieval_method="operator_transcribed",
    )
    [row] = candidate_evidence(conn, sku)
    assert row["source_excerpt"] is None


# --- the adapter end to end -------------------------------------------------------


class FakeModel:
    """Returns one extraction, and records what it was asked."""

    provider = "fake"
    model = "m"

    def __init__(self, tool_input):
        self._tool_input = tool_input
        self.seen = None

    def estimate_input_tokens(self, request):
        return 1000

    def rates(self):
        from resell.reasoning.budget import ModelRates

        return ModelRates()

    def run(self, request):
        from resell.reasoning.stages import StageResult, Usage

        self.seen = request
        return StageResult(
            tool_input=self._tool_input, usage=Usage(1000, 200, {}), latency_ms=10,
            provider="fake", model="m", stop_reason="tool_use", raw_response={},
        )


def adapter_for(tmp_path, tool_input, body, urls=("https://beatsbydre.com/pill",)):
    from resell.reasoning.adapters.web import OperatorUrlResearchAdapter

    conn, gateway, sku = fixture(tmp_path)
    answers = iter([*urls, ""])
    adapter = OperatorUrlResearchAdapter(
        conn, sku,
        model_adapter=FakeModel(tool_input),
        fetcher=fetcher(FakeResponse(body)),
        prompt=lambda _: next(answers, ""),
        echo=lambda *a: None,
    )
    return conn, gateway, sku, adapter


BODY = (
    b"<html><head><title>Beats Pill</title></head><body>"
    b"<p>Colourway: Statement Red</p><p>Model number A3211</p>"
    b"<p>Price $149.99</p></body></html>"
)

GOOD_EXTRACTION = {
    "product_title": "Beats Pill Portable Speaker",
    "facts": [
        {"claim": "Colourway: Statement Red", "domain": "identity",
         "excerpt": "Colourway: Statement Red"},
        {"claim": "Model number A3211", "domain": "identity",
         "excerpt": "Model number A3211"},
        {"claim": "Listed at $149.99", "domain": "retail", "excerpt": "Price $149.99"},
    ],
}


def test_the_adapter_returns_a_document_with_quoted_facts(tmp_path):
    _, _, _, adapter = adapter_for(tmp_path, GOOD_EXTRACTION, BODY)
    [document] = adapter.search(ResearchQuery("beats pill a3211", "manufacturer", "settle the model"))
    assert document.authority is SourceAuthority.MANUFACTURER
    assert document.retrieval_method is RetrievalMethod.AUTOMATED_FETCH
    assert len(document.facts) == 3
    assert all(fact.excerpt for fact in document.facts)


def test_retail_and_identity_stay_separated(tmp_path):
    _, _, _, adapter = adapter_for(tmp_path, GOOD_EXTRACTION, BODY)
    [document] = adapter.search(ResearchQuery("q", "manufacturer", "m"))
    domains = {fact.claim: fact.domain for fact in document.facts}
    assert domains["Listed at $149.99"] is FactDomain.RETAIL
    assert domains["Model number A3211"] is FactDomain.IDENTITY


def test_the_document_is_recorded_as_a_fetch_not_a_transcription(tmp_path):
    conn, gateway, sku, adapter = adapter_for(tmp_path, GOOD_EXTRACTION, BODY)
    [document] = adapter.search(ResearchQuery("q", "manufacturer", "m"))
    gateway.record_candidate_facts(
        sku, candidate_ref=document.candidate_ref, source_url=document.url,
        authority=str(document.authority),
        facts=[(f.claim, str(f.domain), f.excerpt) for f in document.facts],
        title=document.title, retrieval_method=str(document.retrieval_method),
    )
    rows = candidate_evidence(conn, sku)
    assert {row["retrieval_method"] for row in rows} == {"automated_fetch"}
    # The URL recorded is where the fetch landed, not what was typed -- the fake
    # response resolves to /p, and following that is the point.
    assert {row["source"] for row in rows} == {"https://beatsbydre.com/p"}
    assert {row["source_url"] for row in rows} == {"https://beatsbydre.com/p"}
    assert all(row["source_excerpt"] for row in rows)


def test_a_page_yielding_nothing_usable_is_not_stored(tmp_path):
    _, _, _, adapter = adapter_for(
        tmp_path, {"product_title": "x", "facts": [
            {"claim": "Bluetooth 5.3", "domain": "identity", "excerpt": "not on the page"}
        ]}, BODY,
    )
    assert adapter.search(ResearchQuery("q", "manufacturer", "m")) == []
    assert any("does not appear" in note for note in adapter.notes)


def test_an_unfetchable_url_is_noted_and_skipped(tmp_path):
    from resell.reasoning.adapters.web import OperatorUrlResearchAdapter

    conn, _, sku = fixture(tmp_path)
    answers = iter(["https://beatsbydre.com/gone", ""])
    adapter = OperatorUrlResearchAdapter(
        conn, sku, model_adapter=FakeModel(GOOD_EXTRACTION),
        fetcher=fetcher(FakeResponse(status=404)),
        prompt=lambda _: next(answers, ""), echo=lambda *a: None,
    )
    assert adapter.search(ResearchQuery("q", "manufacturer", "m")) == []
    assert any("HTTP 404" in note for note in adapter.notes)


def test_the_extraction_call_is_ledgered_under_its_own_purpose(tmp_path):
    """Separable spend is the argument for moving this stage to a local model, so
    it has to be countable on its own."""
    conn, _, sku, adapter = adapter_for(tmp_path, GOOD_EXTRACTION, BODY)
    adapter.search(ResearchQuery("q", "manufacturer", "m"))
    purposes = [
        row["purpose"] for row in conn.execute(
            "SELECT purpose FROM model_call WHERE sku = ?", (sku,)
        )
    ]
    assert purposes == ["research_extract"]


def test_the_extractor_is_never_shown_the_item(tmp_path):
    """It reads a page about a product. Observations of the object are not its
    business, and a prompt containing them would invite it to describe the item."""
    _, _, _, adapter = adapter_for(tmp_path, GOOD_EXTRACTION, BODY)
    adapter.search(ResearchQuery("beats pill", "manufacturer", "settle the model"))
    request = adapter._model_adapter.seen
    assert request.images == ()
    assert "Colourway: Statement Red" in request.instruction
    assert "observation" not in request.system_prompt.lower()


def test_the_adapter_refuses_without_a_model(tmp_path):
    from resell.reasoning.adapters.research import ResearchError
    from resell.reasoning.adapters.web import OperatorUrlResearchAdapter

    conn, _, sku = fixture(tmp_path)
    adapter = OperatorUrlResearchAdapter(conn, sku, echo=lambda *a: None)
    with pytest.raises(ResearchError, match="no model adapter"):
        adapter.search(ResearchQuery("q", "manufacturer", "m"))


def test_the_adapter_is_registered_and_resolvable():
    from resell.reasoning.adapters.research import ADAPTERS, get_research_adapter

    assert "fetch" in ADAPTERS
    adapter = get_research_adapter("fetch")
    assert adapter.provider == "fetch"
    assert adapter.cost_micros_per_lookup() == 0


def test_ebay_is_still_not_a_retrieval_adapter():
    from resell.reasoning.adapters.research import ADAPTERS

    assert not [name for name in ADAPTERS if "ebay" in name]


# --- the whole round, through the existing gates -----------------------------------


def test_a_fetched_fact_becomes_citable_through_the_donation_gate(tmp_path):
    """The point of the whole exercise: research output reaches identification only
    by the existing route -- candidate fact, match, donation scope, citable -- and
    never by writing an identification field."""
    from resell.reasoning.adapters.web import OperatorUrlResearchAdapter
    from resell.reasoning.budget import LookupBudget, StageBudget
    from resell.reasoning.research_loop import run_round
    from resell.reasoning.schema import Basis, Observation
    from resell.gateway import citable_candidate_evidence

    conn, gateway, sku = fixture(tmp_path)
    gateway.record_observation(
        sku,
        Observation(claim="bottom panel reads A3211", basis=Basis.TEXT_READ,
                    photo_positions=(2,)),
    )

    plan = {"assessment": {"sufficient": False, "proposed_mode": "product_family",
                           "rationale": "A3211 needs confirming against the maker"},
            "lookups": [{"query": "beats a3211", "source_kind": "manufacturer",
                         "motivation": "confirm the model", "evidence_ids": [1]}]}

    def match():
        rows = candidate_evidence(conn, sku)
        return {"assessment": {"any_match": True, "rationale": "the code matches"},
                "claims": [{"candidate_ref": rows[0]["candidate_ref"], "is_match": True,
                            "strength": "identifier_asserted",
                            "rationale": "A3211 appears on both",
                            "item_evidence": [1],
                            "candidate_evidence": [rows[0]["id"]]}]}

    class TwoStageModel:
        provider, model = "fake", "m"

        def estimate_input_tokens(self, request):
            return 1000

        def rates(self):
            from resell.reasoning.budget import ModelRates

            return ModelRates()

        def run(self, request):
            from resell.reasoning.stages import StageResult, Usage

            name = request.tool.name
            payload = {"plan_research": plan, "judge_candidates": None}.get(name)
            if name == "judge_candidates":
                payload = match()
            return StageResult(
                tool_input=payload, usage=Usage(1000, 200, {}), latency_ms=10,
                provider="fake", model="m", stop_reason="tool_use", raw_response={},
            )

    answers = iter(["https://beatsbydre.com/pill", ""])
    research = OperatorUrlResearchAdapter(
        conn, sku, model_adapter=FakeModel(GOOD_EXTRACTION),
        fetcher=fetcher(FakeResponse(BODY)),
        prompt=lambda _: next(answers, ""), echo=lambda *a: None,
    )

    outcome = run_round(
        conn, gateway, sku, model_adapter=TwoStageModel(), research_adapter=research,
        stage_budget=StageBudget(max_calls=9, max_cost_micros=9_000_000),
        lookup_budget=LookupBudget(max_lookups=4),
    )

    assert outcome.performed == ["beats a3211"]
    assert outcome.selection is not None and outcome.selection.selected

    # Identity facts are citable; the retail fact is not, whatever the match said.
    citable = citable_candidate_evidence(conn, sku)
    rows = {row["id"]: row for row in candidate_evidence(conn, sku)}
    by_domain = {rows[i]["fact_domain"] for i in citable}
    assert by_domain == {"identity"}
    assert len(citable) == 2

    # And every citable fact can be checked against the text it came from.
    assert all(rows[i]["source_excerpt"] for i in citable)


def test_the_round_never_writes_an_identification_field(tmp_path):
    """Research may set resolution metadata; identification content arrives only
    through map-aspects citing a donated fact."""
    from resell.reasoning.adapters.web import OperatorUrlResearchAdapter
    from resell.reasoning.budget import LookupBudget, StageBudget
    from resell.reasoning.research_loop import run_round
    from resell.reasoning.schema import Basis, Observation

    conn, gateway, sku = fixture(tmp_path)
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, title="Red speaker", category_id="111694")
    gateway.record_observation(
        sku, Observation(claim="bottom panel reads A3211", basis=Basis.TEXT_READ,
                         photo_positions=(1,)),
    )
    before = conn.execute(
        "SELECT title, brand, model, aspects FROM identification "
        "WHERE sku = ? AND superseded_at IS NULL", (sku,)
    ).fetchone()

    class Planner:
        provider, model = "fake", "m"

        def estimate_input_tokens(self, request):
            return 1000

        def rates(self):
            from resell.reasoning.budget import ModelRates

            return ModelRates()

        def run(self, request):
            from resell.reasoning.stages import StageResult, Usage

            return StageResult(
                tool_input={"assessment": {
                    "sufficient": True, "proposed_mode": "described_object",
                    "rationale": "nothing further to find"}, "lookups": []},
                usage=Usage(1000, 100, {}), latency_ms=5, provider="fake", model="m",
                stop_reason="tool_use", raw_response={},
            )

    run_round(
        conn, gateway, sku, model_adapter=Planner(),
        research_adapter=OperatorUrlResearchAdapter(
            conn, sku, model_adapter=FakeModel(GOOD_EXTRACTION), echo=lambda *a: None
        ),
        stage_budget=StageBudget(max_calls=9, max_cost_micros=9_000_000),
        lookup_budget=LookupBudget(max_lookups=4),
    )

    after = conn.execute(
        "SELECT title, brand, model, aspects FROM identification "
        "WHERE sku = ? AND superseded_at IS NULL", (sku,)
    ).fetchone()
    assert dict(after) == dict(before)


def test_an_excerpt_lifted_from_the_prompt_is_not_accepted(tmp_path):
    """The excerpt is validated against the page body alone. The assembled prompt
    also carries the URL, the query and the motivation, and those are our words --
    accepting a quotation of them would let the check pass on nothing."""
    _, _, _, adapter = adapter_for(
        tmp_path,
        {"product_title": "x", "facts": [
            {"claim": "Confirms the model", "domain": "identity",
             "excerpt": "settle the model"},          # from the motivation
            {"claim": "From beatsbydre.com", "domain": "identity",
             "excerpt": "https://beatsbydre.com/p"},  # from the URL line
        ]},
        BODY,
    )
    assert adapter.search(
        ResearchQuery("beats pill", "manufacturer", "settle the model")
    ) == []
    assert len(adapter.notes) == 2
    assert all("does not appear in the fetched page" in n for n in adapter.notes)


def test_a_quotation_beyond_the_truncation_point_is_rejected(tmp_path):
    """Text the model was never shown cannot be something it read."""
    from resell.reasoning.stages import MAX_PAGE_CHARS

    tail = "Colourway: Statement Red"
    body = ("<p>" + "filler word " * (MAX_PAGE_CHARS // 6) + tail + "</p>").encode()
    _, _, _, adapter = adapter_for(
        tmp_path,
        {"product_title": "x", "facts": [
            {"claim": "Colourway: Statement Red", "domain": "identity", "excerpt": tail}
        ]},
        body,
    )
    assert adapter.search(ResearchQuery("q", "manufacturer", "m")) == []
    assert any("does not appear" in note for note in adapter.notes)
