"""How each kind of evidence counted, and the case where none of it did.

The failure that prompted this: two eBay comps were recorded and the band did not
move, because `comp-add` writes an observation and `price claim` is what attaches
it to an item. Pricing reads the join, so an unclaimed observation is invisible --
which is indistinguishable, in a band, from an observation nobody recorded.

The wider point is the same one: evidence that is present and contributes nothing
looks exactly like evidence that is absent. Retail context, retained sold comps in
the wrong condition, asks that have sat too long, and observations attached to no
item all read as silence. Each of them now has a line.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from resell import db, store_pricing as sp
from resell.domain import FeeModel
from resell.gateway import Gateway
from resell.pricing.comps import (
    CompBasis,
    CompClaim,
    CompObservation,
    Comparability,
    ConditionBand,
    PriceKind,
    RetailKind,
)
from resell.pricing.estimate import (
    EvidenceRole,
    PricingInput,
    RetailReference,
    measure_demand,
    recommend,
)

NOW = datetime(2026, 8, 22, tzinfo=UTC)


def comp(price_cents, *, kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
         band=ConditionBand.USED_GOOD, days=None, comp_id=None, shipping=0,
         comparability=Comparability.SAME_FAMILY_VARIANT, excluded_reason=None):
    comp_id = comp_id or f"c{price_cents}{band}{days}"
    return sp.ScoredComp(
        claim=CompClaim(
            claim_id=f"cl{comp_id}", sku="MP-000005", comp_id=comp_id,
            comparability=comparability, item_citations=("1",),
            comp_citations=("title",), excluded_reason=excluded_reason,
        ),
        observation=CompObservation(
            comp_id=comp_id, marketplace="EBAY_US", external_id=comp_id,
            price_kind=kind, basis=basis, price_cents=price_cents,
            observed_at=NOW, condition_band=band, shipping_cents=shipping,
            days_on_market=days,
        ),
    )


def band_for(comps, *, item_band=ConditionBand.USED_GOOD, retail=(), window=90):
    return recommend(PricingInput(
        sku="MP-000005", item_condition_band=item_band,
        identity_resolution="searched_not_found", comps=tuple(comps),
        retail=tuple(retail), window_days=window, now=NOW,
    ))


def lines(rec):
    return {line.source: line for line in rec.contributions}


# --- the ledger ------------------------------------------------------------------


def test_the_pool_that_set_the_band_is_marked_as_such():
    rec = band_for([comp(8000), comp(9000)])
    entry = lines(rec)["asks, condition matched"]
    assert entry.role is EvidenceRole.SET_THE_BAND
    assert entry.contributed
    assert entry.n == 2


def test_evidence_retained_but_unapplied_gets_its_own_line():
    """A sold comp in a materially different condition estimates something else.
    It is kept, reported, and applied to nothing -- which without a line reads as
    if no sold evidence existed at all."""
    rec = band_for(
        [comp(8000, band=ConditionBand.USED_GOOD),
         comp(4000, kind=PriceKind.REALIZED, basis=CompBasis.SOLD_SIMILAR,
              band=ConditionBand.FOR_PARTS)],
        item_band=ConditionBand.USED_GOOD,
    )
    sold = lines(rec)["sold, other condition"]
    assert sold.role is EvidenceRole.RETAINED_NOT_APPLIED
    assert not sold.contributed
    assert sold.n == 1
    assert lines(rec)["asks, condition matched"].contributed


def test_marketplace_comps_come_before_everything_else():
    """Order is the argument: what set the number should be read first."""
    rec = band_for([comp(8000)], retail=[
        RetailReference(price_cents=19900, kind=RetailKind.ORIGINAL)
    ])
    sources = [line.source for line in rec.contributions]
    assert sources.index("asks, condition matched") < sources.index("retail context")


# --- retail is context, never a comp -----------------------------------------------


def test_retail_context_is_a_ceiling_check_not_a_sample():
    rec = band_for([comp(8000)], retail=[
        RetailReference(price_cents=19900, kind=RetailKind.ORIGINAL)
    ])
    entry = lines(rec)["retail context"]
    assert entry.role is EvidenceRole.CEILING_CHECK
    assert not entry.contributed
    assert entry.low_cents == 19900


def test_a_retail_price_recorded_as_a_comp_is_reclassified_not_dropped():
    """It used to be filtered out silently, so an operator who supplied one saw no
    trace of it anywhere and could not tell whether it had been counted."""
    rec = band_for([
        comp(8000),
        comp(19900, kind=PriceKind.REFERENCE, basis=CompBasis.RETAIL_REFERENCE,
             comp_id="retail1"),
    ])
    entry = lines(rec)["retail recorded as a comp"]
    assert entry.role is EvidenceRole.RECLASSIFIED
    assert entry.n == 1


def test_the_reclassified_retail_comp_stays_out_of_every_distribution():
    rec = band_for([
        comp(8000),
        comp(19900, kind=PriceKind.REFERENCE, basis=CompBasis.RETAIL_REFERENCE,
             comp_id="retail1"),
    ])
    assert rec.band_high_cents == 8000
    assert rec.asking_comparable.n == 1


def test_retail_alone_cannot_produce_a_price():
    rec = band_for([], retail=[
        RetailReference(price_cents=19900, kind=RetailKind.ORIGINAL)
    ])
    assert rec.unpriceable
    assert "cannot become a price on its own" in rec.reason


# --- demand, modelled apart from price ----------------------------------------------


def test_days_on_market_is_measured_across_the_asking_pool():
    signal = measure_demand([comp(8000, days=10), comp(9000, days=30)], 90)
    assert signal.measured
    assert signal.median_days == 20
    assert signal.n_asks == 2


def test_unmeasured_liquidity_is_not_reported_as_fast():
    signal = measure_demand([comp(8000), comp(9000)], 90)
    assert not signal.measured
    assert "unmeasured, which is not the same as fast" in signal.describe()


def test_demand_counts_the_asks_that_sat_beyond_the_window():
    signal = measure_demand([comp(8000, days=10), comp(9000, days=200)], 90)
    assert signal.n_beyond_window == 1
    assert "evidence the asking price is wrong" in signal.describe()


def test_demand_is_measured_before_stale_asks_are_dropped_from_the_band():
    """An ask that has sat 200 days is the most informative thing in the sample
    about liquidity and the least informative about value. It leaves the band and
    stays in the demand signal."""
    rec = band_for([comp(8000, days=5), comp(9000, days=5), comp(50000, days=400)])
    assert rec.demand.n_asks == 3
    assert rec.demand.n_beyond_window == 1
    # and the stale one is out of the band
    assert rec.band_high_cents == 9000


def test_demand_moves_no_number():
    """The instruction it exists under: modelled separately, informing which
    strategy to pick and nothing else."""
    slow = band_for([comp(8000, days=80), comp(9000, days=85)])
    fast = band_for([comp(8000, days=1), comp(9000, days=2)])
    assert slow.band_central_cents == fast.band_central_cents
    assert slow.demand.median_days != fast.demand.median_days


def test_demand_appears_in_the_ledger_as_its_own_role():
    rec = band_for([comp(8000, days=10)])
    entry = lines(rec)["days on market"]
    assert entry.role is EvidenceRole.DEMAND_ONLY
    assert not entry.contributed


# --- exclusions -----------------------------------------------------------------------


def test_operator_exclusions_are_accounted_for():
    rec = band_for([
        comp(8000),
        comp(2000, comparability=Comparability.EXCLUDED, comp_id="x",
             excluded_reason="a lot of five"),
    ])
    entry = lines(rec)["ruled out as different"]
    assert entry.role is EvidenceRole.EXCLUDED
    assert entry.n == 1


def test_stale_asks_dropped_from_the_sample_are_accounted_for():
    rec = band_for([comp(8000, days=5), comp(9000, days=5), comp(50000, days=400)])
    entry = lines(rec)["dropped from the sample"]
    assert entry.role is EvidenceRole.EXCLUDED
    assert "beyond the 90-day window" in entry.detail


# --- the bug that started it -------------------------------------------------------


def fixture(tmp_path):
    conn = db.connect(tmp_path / "evidence.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    return conn, gateway, sku


def observation_row(comp_id, price_cents):
    return CompObservation(
        comp_id=comp_id, marketplace="EBAY_US", external_id=comp_id,
        price_kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
        price_cents=price_cents, observed_at=NOW,
        condition_band=ConditionBand.USED_GOOD, shipping_cents=None,
        title=f"listing {comp_id}",
    )


def test_an_observation_with_no_claim_is_invisible_to_pricing(tmp_path):
    """The data model is right -- one listing can be a comp for several items at
    different rungs -- but it makes 'recorded' and 'counted' two states, and
    nothing said so."""
    conn, _, sku = fixture(tmp_path)
    sp.record_comp_observation(conn, observation_row("comp_a", 7995))
    assert sp.load_scored_comps(conn, sku) == []


def test_unclaimed_observations_are_findable(tmp_path):
    conn, _, sku = fixture(tmp_path)
    sp.record_comp_observation(conn, observation_row("comp_a", 7995))
    sp.record_comp_observation(conn, observation_row("comp_b", 4997))
    unclaimed = sp.unclaimed_observations(conn)
    assert {row["comp_id"] for row in unclaimed} == {"comp_a", "comp_b"}


def test_a_claimed_observation_leaves_the_unclaimed_list(tmp_path):
    conn, _, sku = fixture(tmp_path)
    sp.record_comp_observation(conn, observation_row("comp_a", 7995))
    sp.record_comp_claim(
        conn,
        CompClaim(claim_id="cl1", sku=sku, comp_id="comp_a",
                  comparability=Comparability.SAME_FAMILY_VARIANT,
                  item_citations=("1",), comp_citations=("title",)),
        identity_resolution="searched_not_found",
    )
    assert sp.unclaimed_observations(conn) == []
    assert len(sp.load_scored_comps(conn, sku)) == 1


def test_claiming_a_comp_moves_the_band(tmp_path):
    """The end of the reported problem: two comps added, band unchanged; claim
    them and the band reflects them."""
    conn, _, sku = fixture(tmp_path)
    for comp_id, price in (("comp_a", 7995), ("comp_b", 4997)):
        sp.record_comp_observation(conn, observation_row(comp_id, price))

    before = recommend(PricingInput(
        sku=sku, item_condition_band=ConditionBand.USED_GOOD,
        identity_resolution="searched_not_found",
        comps=tuple(sp.load_scored_comps(conn, sku)), now=NOW,
    ))
    assert before.unpriceable

    for index, comp_id in enumerate(("comp_a", "comp_b")):
        sp.record_comp_claim(
            conn,
            CompClaim(claim_id=f"cl{index}", sku=sku, comp_id=comp_id,
                      comparability=Comparability.SAME_FAMILY_VARIANT,
                      item_citations=("1",), comp_citations=("title",)),
            identity_resolution="searched_not_found",
        )
    after = recommend(PricingInput(
        sku=sku, item_condition_band=ConditionBand.USED_GOOD,
        identity_resolution="searched_not_found",
        comps=tuple(sp.load_scored_comps(conn, sku)), now=NOW,
    ))
    assert not after.unpriceable
    assert after.band_low_cents == 4997
    assert after.band_high_cents == 7995


# --- an approximate asking market, labelled as one ---------------------------------
#
# The policy this encodes: a search index returns prices with no condition attached,
# and those are worth having. What they are not is a condition-matched sample, and a
# band built on them must say so. The numbers below are the real Brave result for
# MP-000005.


BRAVE_SAMPLE = [5099, 5899, 7500, 7800, 7995, 9495]


def unstated(cents, comp_id):
    """An ask whose condition nobody stated. Not 'a different condition'."""
    return comp(cents, kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
                band=ConditionBand.UNKNOWN, comp_id=comp_id)


def test_asks_of_unknown_condition_still_produce_a_band():
    """They used to be the pool of last resort and they still are -- the point is
    that last resort is not the same as discarded."""
    rec = band_for([unstated(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)])
    assert not rec.unpriceable
    assert rec.band_low_cents < rec.band_central_cents < rec.band_high_cents
    assert rec.n_in_basis == 6


def test_the_whole_sample_reaches_the_distribution():
    rec = band_for([unstated(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)])
    assert rec.basis_distribution.min_cents == 5099
    assert rec.basis_distribution.max_cents == 9495


def test_the_band_is_labelled_asking_and_unstated_not_mismatched():
    """`condition_mismatch` asserts a difference was observed between this item's
    condition and the comps'. Nothing was observed: the comps have no condition at
    all. Saying 'mismatch' invents the comparison."""
    rec = band_for([unstated(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)])
    quals = {str(q) for q in rec.qualifiers}
    assert "asking_unknown_condition" in quals
    assert "asking_only" in quals
    assert "condition_mismatch" not in quals
    assert rec.band_relation == "asking_condition_unstated"


def test_it_is_never_reported_as_realized():
    rec = band_for([unstated(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)])
    assert rec.price_kind is PriceKind.ASKING
    assert rec.realized is None


def test_the_ledger_names_the_pool_for_what_it_is():
    rec = band_for([unstated(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)])
    entry = lines(rec)["asks, condition unstated"]
    assert entry.role is EvidenceRole.SET_THE_BAND
    assert entry.n == 6
    assert "no condition attached" in entry.detail


def test_a_mixed_pool_is_still_a_mismatch():
    """One ask with a stated, materially different condition means a comparison
    genuinely was made and genuinely failed."""
    rec = band_for(
        [unstated(5099, "b1"), unstated(7800, "b2"),
         comp(4000, band=ConditionBand.FOR_PARTS, comp_id="known")],
        item_band=ConditionBand.USED_EXCELLENT,
    )
    quals = {str(q) for q in rec.qualifiers}
    assert "condition_mismatch" in quals
    assert "asking_unknown_condition" not in quals


def test_a_thin_matched_pool_is_widened_rather_than_left_alone():
    """A deliberate revision of the earlier rule, not a regression of it.

    Two condition-matched asks used to speak over six marketplace observations,
    and the band came from the two. Two observations are not a distribution, and
    discarding six for lacking a condition label is what "unknown condition should
    reduce weight, not remove the data" rules out. They are pooled, and the
    qualifier says the pool is mixed.

    No price kind is crossed: every one of the eight is an asking price from a
    marketplace. What differs is whether anyone stated the condition.
    """
    rec = band_for(
        [unstated(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)]
        + [comp(6000, band=ConditionBand.USED_EXCELLENT, comp_id="m1"),
           comp(6200, band=ConditionBand.USED_EXCELLENT, comp_id="m2")],
        item_band=ConditionBand.USED_EXCELLENT,
    )
    quals = {str(q) for q in rec.qualifiers}
    assert "pooled_unknown_condition" in quals
    assert "condition_unknown_in_sample" in quals
    assert rec.price_kind is PriceKind.ASKING
    assert rec.n_in_basis == 8
    assert rec.band_relation == "asks_widened_by_unstated"


def test_a_real_matched_distribution_stands_on_its_own():
    """The boundary. Widening exists because one or two observations cannot
    describe a market; three or more can, and are not diluted."""
    rec = band_for(
        [unstated(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)]
        + [comp(6000, band=ConditionBand.USED_EXCELLENT, comp_id="m1"),
           comp(6200, band=ConditionBand.USED_EXCELLENT, comp_id="m2"),
           comp(6400, band=ConditionBand.USED_EXCELLENT, comp_id="m3")],
        item_band=ConditionBand.USED_EXCELLENT,
    )
    assert lines(rec)["asks, condition matched"].role is EvidenceRole.SET_THE_BAND
    assert lines(rec)["asks, condition unstated"].role is EvidenceRole.RETAINED_NOT_APPLIED
    assert rec.band_central_cents == 6200
    assert "pooled_unknown_condition" not in {str(q) for q in rec.qualifiers}


def test_realized_evidence_still_wins():
    rec = band_for(
        [unstated(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)]
        + [comp(6000, kind=PriceKind.REALIZED, basis=CompBasis.SOLD_SIMILAR,
                band=ConditionBand.USED_EXCELLENT, comp_id="s1")],
        item_band=ConditionBand.USED_EXCELLENT,
    )
    assert rec.price_kind is PriceKind.REALIZED
    assert lines(rec)["sold, condition matched"].role is EvidenceRole.SET_THE_BAND


def test_retail_stays_context_beside_an_unstated_asking_band():
    """The other half of the instruction: manufacturer prices remain a ceiling
    check and never join the sample, however thin the sample is."""
    rec = band_for(
        [unstated(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)],
        retail=[RetailReference(price_cents=19900, kind=RetailKind.ORIGINAL)],
    )
    assert lines(rec)["retail context"].role is EvidenceRole.CEILING_CHECK
    assert not lines(rec)["retail context"].contributed
    assert rec.basis_distribution.max_cents == 9495


# --- the output has to say how good the evidence is --------------------------------


def index_comp(cents, comp_id):
    """A marketplace ask a search engine described, rather than a page we read."""
    from resell.pricing.comps import RetrievalMethod

    scored = unstated(cents, comp_id)
    return sp.ScoredComp(
        claim=scored.claim,
        observation=dataclasses.replace(
            scored.observation, retrieval_method=RetrievalMethod.SEARCH_INDEX,
        ),
    )


def test_a_pool_reports_its_median():
    """"n=12, $50-$95" says less than it knows, and the median is the number a
    seller actually reasons from."""
    rec = band_for([index_comp(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)])
    entry = lines(rec)["asks, condition unstated"]
    assert entry.median_cents == sorted(BRAVE_SAMPLE)[len(BRAVE_SAMPLE) // 2 - 1] or \
        entry.median_cents > 0
    assert entry.low_cents == min(BRAVE_SAMPLE)
    assert entry.high_cents == max(BRAVE_SAMPLE)


def test_a_pool_reports_where_it_came_from():
    """Twelve observations summarised by a search engine are not twelve pages we
    loaded, and a line that reports only the count overstates them."""
    rec = band_for([index_comp(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE)])
    assert lines(rec)["asks, condition unstated"].origins == ("search index",)


def test_a_mixed_pool_names_both_origins():
    rec = band_for(
        [index_comp(c, f"i{i}") for i, c in enumerate(BRAVE_SAMPLE[:3])]
        + [unstated(c, f"f{i}") for i, c in enumerate(BRAVE_SAMPLE[3:])],
    )
    origins = lines(rec)["asks, condition unstated"].origins
    assert any("search index" in o for o in origins)
    assert any("typed by you" in o or "fetched page" in o for o in origins)
    assert all("(" in o for o in origins), "a mixed pool has to give the counts"


def test_retail_never_enters_the_marketplace_distribution():
    """The rule that does not move: manufacturer and retailer prices bound the
    answer and are never a sample of it, however thin the marketplace evidence."""
    rec = band_for(
        [index_comp(c, f"b{i}") for i, c in enumerate(BRAVE_SAMPLE[:2])],
        retail=[RetailReference(price_cents=14999, kind=RetailKind.ORIGINAL)],
    )
    assert rec.basis_distribution.max_cents <= max(BRAVE_SAMPLE[:2])
    assert lines(rec)["retail context"].role is EvidenceRole.CEILING_CHECK
    assert not lines(rec)["retail context"].contributed


def test_marketplace_evidence_outranks_retail_however_thin():
    """Two marketplace observations beat an MSRP. A resale agent with prices people
    are actually asking is better informed than one with a list price."""
    rec = band_for(
        [index_comp(5000, "b1"), index_comp(9500, "b2")],
        retail=[RetailReference(price_cents=14999, kind=RetailKind.ORIGINAL)],
    )
    assert not rec.unpriceable
    assert rec.band_central_cents < 14999
    assert lines(rec)["asks, condition unstated"].contributed
