"""The V2 comp round, through the real recording path.

Everything measured in `v2bench` came from `plan_round`, which is pure and
touches nothing. This exercises the half that writes: observations, claims,
lookups, events, and the invariant that decides whether an item is allowed to
conclude it has no market.

The invariant is the reason this file exists. *"Set a price yourself" is a
statement about the market: we looked, and there is not enough to price from.*
MP-000039 retrieved 38 listings, lost every verdict to a budget check, and asked
its owner to name a price as though the market had been searched and found
wanting. V1 guards that with `judging_complete`; V2 has no judging stage to fail
partway, so its equivalent failure is a search that raised, and it must reach
the orchestrator as incompleteness rather than as an empty market.
"""

from __future__ import annotations

from datetime import UTC, datetime

from resell import db
from resell.reasoning.adapters.search import SearchHit
from resell.reasoning.comp_loop_v2 import run_comp_round_v2


class FakeBackend:
    """A search backend that returns what a test tells it to, or raises."""

    provider = "fake"

    def __init__(self, hits_by_query=None, raises=None, always=None):
        self.hits_by_query = hits_by_query or {}
        self.raises = raises or set()
        self.always = always
        self.queries: list[str] = []

    def cost_micros_per_search(self) -> int:
        return 1000

    def find(self, query, *, limit=10):
        self.queries.append(query.query)
        if query.query in self.raises:
            raise RuntimeError("brave is down")
        if self.always is not None:
            return list(self.always)
        return list(self.hits_by_query.get(query.query, ()))


def hit(price_cents, title, host="ebay.com", ident="x1"):
    return SearchHit(
        url=f"https://www.{host}/itm/{ident}", title=title, snippet=title,
        price_cents=price_cents, currency="USD",
        page_age=datetime.now(UTC), query="q",
    )


def priced_item(tmp_path):
    """An identified item, ready for pricing, with observations to cite."""
    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
        "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
        (sku, "vision_observation", "fake/m", '{"claim": "a massage gun"}',
         db.now_iso(), "visual_observation"),
    )
    gateway.propose_identification(
        sku, brand="Achedaway", title="Achedaway Percussion Massage Gun",
        category_id="36449", condition_id="USED_EXCELLENT",
        aspects={"Type": ["Massage Gun"]},
    )
    gateway.begin_pricing(sku)
    return conn, gateway, sku


# --- the recording half -------------------------------------------------------

def test_it_records_observations_claims_and_lookups(tmp_path):
    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(always=[
        hit(4500, "Achedaway Therapy Handheld Massage Gun With Charger", ident="a"),
        hit(9999, "Achedaway Pro Massage Gun Kit Extra Attachments", ident="b"),
    ])
    outcome = run_comp_round_v2(conn, gateway, sku, backend=backend)

    assert outcome.judging_complete
    assert outcome.comps_recorded == 2, "deduped across the four queries"
    assert outcome.claims_recorded == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM comp_observation").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM comp_claim WHERE sku = ?", (sku,)).fetchone()[0] == 2


def test_every_search_is_accounted_for_as_a_lookup(tmp_path):
    """Brave costs money whether or not the round liked what it found."""
    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(always=[hit(4500, "Achedaway Massage Gun", ident="a")])
    outcome = run_comp_round_v2(conn, gateway, sku, backend=backend)

    rows = conn.execute(
        "SELECT query, cost_micros, result_count FROM research_lookup "
        "WHERE sku = ? AND scope = 'pricing'", (sku,)).fetchall()
    assert len(rows) == len(outcome.performed) == 4, "one per query, all four"
    assert all(r["cost_micros"] == 1000 for r in rows)


def test_the_lookup_budget_is_respected(tmp_path):
    from resell.reasoning.budget import LookupBudget

    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(always=[hit(4500, "Achedaway Massage Gun", ident="a")])
    outcome = run_comp_round_v2(
        conn, gateway, sku, backend=backend,
        lookup_budget=LookupBudget(scope="pricing", max_lookups=2,
                                   max_cost_micros=10_000_000),
    )
    assert len(backend.queries) == 2, "searched only what it could afford"
    assert len(outcome.deferred) == 2
    assert outcome.judging_complete, "a trimmed plan is not a failed one"


def test_a_deterministic_round_never_leaves_a_comp_unjudged(tmp_path):
    """V1's judging stage could run out of budget mid-sample. This one cannot."""
    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(always=[
        hit(1000 * n, f"Achedaway Massage Gun model {n}", ident=str(n))
        for n in range(1, 13)
    ])
    outcome = run_comp_round_v2(conn, gateway, sku, backend=backend)

    assert outcome.unjudged == []
    assert outcome.claims_recorded == outcome.comps_recorded
    assert outcome.judging_complete


# --- the invariant ------------------------------------------------------------

def test_a_failed_search_is_never_an_empty_market(tmp_path):
    """The whole point. Brave raising must not read as "we looked and found
    nothing" -- that is the sentence that sends a seller to type their own price
    believing the market was searched."""
    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(raises={"Achedaway Percussion Massage Gun",
                                  "Achedaway Percussion Massage Gun sold",
                                  "Achedaway Percussion Massage Gun used",
                                  "Achedaway Percussion Massage Gun price"})
    outcome = run_comp_round_v2(conn, gateway, sku, backend=backend)

    assert not outcome.judging_complete, "the orchestrator must raise on this"
    assert "search(es) failed" in outcome.incomplete_reason
    assert outcome.stopped != "searched_not_found"


def test_one_failure_among_several_still_reports_incomplete(tmp_path):
    """A partial answer is worth keeping and never worth presenting as a
    complete one."""
    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(
        hits_by_query={"Achedaway Percussion Massage Gun":
                       [hit(4500, "Achedaway Massage Gun", ident="a")]},
        raises={"Achedaway Percussion Massage Gun sold"},
    )
    outcome = run_comp_round_v2(conn, gateway, sku, backend=backend)

    assert outcome.comps_recorded == 1, "what it found is kept"
    assert not outcome.judging_complete, "and still not offered as the whole truth"


def test_an_exhausted_budget_is_incomplete_not_a_thin_market(tmp_path):
    """Nothing was searched, so nothing was learned. This is the exact shape of
    the MP-000039 failure."""
    from resell.reasoning.budget import LookupBudget

    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(always=[hit(4500, "Achedaway Massage Gun", ident="a")])
    outcome = run_comp_round_v2(
        conn, gateway, sku, backend=backend,
        lookup_budget=LookupBudget(scope="pricing", max_lookups=0,
                                   max_cost_micros=10_000_000),
    )
    assert backend.queries == [], "nothing was searched"
    assert not outcome.judging_complete
    assert outcome.stopped == "exhausted"


def test_a_genuinely_empty_market_is_allowed_to_say_so(tmp_path):
    """The other half of the invariant. When every search succeeded and returned
    nothing usable, that IS a statement about the market."""
    conn, gateway, sku = priced_item(tmp_path)
    outcome = run_comp_round_v2(conn, gateway, sku, backend=FakeBackend(always=[]))

    assert outcome.judging_complete, "nothing failed"
    assert outcome.stopped == "searched_not_found"
    assert outcome.comps_recorded == 0


def test_an_item_with_no_observations_cannot_conclude_anything(tmp_path):
    """`validate_claim` refuses a claim citing nothing, so a round with no
    observations would record comps it could never claim."""
    from tests.test_orchestrator import fixture, with_photo

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, brand="Achedaway", title="A massage gun",
                                  category_id="36449",
                                  condition_id="USED_EXCELLENT")
    gateway.begin_pricing(sku)

    outcome = run_comp_round_v2(conn, gateway, sku, backend=FakeBackend(always=[]))
    assert not outcome.judging_complete
    assert outcome.stopped == "no_observations"


# --- the shape the rest of the system already reads ---------------------------

def test_it_reports_in_the_shape_ops_already_reads(tmp_path):
    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(always=[
        hit(4500, "Achedaway Therapy Handheld Massage Gun With Charger", ident="a"),
        hit(1589, "Genuine OEM Achedaway Massage Gun Attachment Fork Head", ident="b"),
    ])
    outcome = run_comp_round_v2(conn, gateway, sku, backend=backend)

    assert outcome.ladder.get("excluded") == 1, "the attachment head"
    assert outcome.ladder.get("same_family_variant") == 1
    kinds = {k: v for k, v in outcome.kinds.items()}
    assert kinds.get("asking") == 2, "never realized; the index has no sales"

    events = {r["kind"] for r in conn.execute(
        "SELECT kind FROM events WHERE item_id = ?", (sku,))}
    assert "comp_research.round_complete" in events
    assert "comp_research.round_detail" in events


def test_retailers_are_not_comps(tmp_path):
    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(always=[
        hit(4500, "Achedaway Massage Gun", host="ebay.com", ident="a"),
        hit(9999, "Achedaway Massage Gun", host="walmart.com", ident="b"),
    ])
    outcome = run_comp_round_v2(conn, gateway, sku, backend=backend)
    assert outcome.comps_recorded == 1
    assert any("not resale marketplaces" in n for n in outcome.notes)


def test_re_running_a_round_does_not_crash_on_comps_already_judged(tmp_path):
    """Found by running the real path against a copy of production. MP-000047
    already carried ten claims from its V1 round, and `comp_claim` is UNIQUE on
    (sku, comp_id) -- so the second round raised IntegrityError partway through
    recording and lost the rest of its work.

    A listing already judged for this item is judged. The verdict on record
    stands, and the round is still complete."""
    conn, gateway, sku = priced_item(tmp_path)
    backend = FakeBackend(always=[
        hit(4500, "Achedaway Therapy Handheld Massage Gun With Charger", ident="a"),
        hit(9999, "Achedaway Pro Massage Gun Kit Extra Attachments", ident="b"),
    ])
    first = run_comp_round_v2(conn, gateway, sku, backend=backend)
    second = run_comp_round_v2(conn, gateway, sku, backend=backend)

    assert first.claims_recorded == 2
    assert second.claims_recorded == 2, "counted, not lost"
    assert second.judging_complete, "and the round may still conclude"
    assert any("already judged" in n for n in second.notes)
    assert conn.execute(
        "SELECT COUNT(*) FROM comp_claim WHERE sku = ?", (sku,)
    ).fetchone()[0] == 2, "still one claim per listing"
