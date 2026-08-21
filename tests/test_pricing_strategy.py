"""Seller strategy, and the condition-mismatch rule it depends on."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from resell.pricing.comps import (
    CompBasis,
    CompClaim,
    CompObservation,
    Comparability,
    ConditionBand,
    PriceKind,
)
from resell.pricing.estimate import (
    PriceQualifier,
    PricingInput,
    ScoredComp,
    condition_comparable,
    recommend,
)
from resell.pricing.proceeds import CostLines, FeeBasis, FeeSchedule
from resell.pricing.strategy import (
    BrandSignal,
    BrandStrength,
    SellerObjective,
    Statistic,
    build_strategies,
)

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)
VERIFIED = FeeSchedule(
    version="ebay-us-clothing-2026-08", category_id="57988", rate=0.1335,
    fixed_cents=40, basis=FeeBasis.CATEGORY_VERIFIED,
)


def comp(price, kind=PriceKind.REALIZED, band=ConditionBand.NEW_WITH_TAGS, *,
         shipping=0, days_on_market=None, cid=None) -> ScoredComp:
    cid = cid or f"c{price}{kind}{band}"
    obs = CompObservation(
        comp_id=cid, marketplace="EBAY_US", external_id=cid, price_kind=kind,
        basis=CompBasis.SOLD_SIMILAR if kind is PriceKind.REALIZED else CompBasis.ACTIVE_SIMILAR,
        price_cents=price, observed_at=NOW - timedelta(days=1), condition_band=band,
        shipping_cents=shipping, days_on_market=days_on_market,
    )
    claim = CompClaim(
        claim_id=f"cl_{cid}", sku="MP-000003", comp_id=cid,
        comparability=Comparability.SAME_FAMILY_VARIANT,
        item_citations=("ev_1",), comp_citations=("title",),
    )
    return ScoredComp(claim=claim, observation=obs)


def inp(**kw) -> PricingInput:
    base = dict(
        sku="MP-000003", item_condition_band=ConditionBand.NEW_WITH_TAGS,
        identity_resolution="searched_not_found", now=NOW,
    )
    base.update(kw)
    return PricingInput(**base)


# --- the condition-mismatch fix -------------------------------------------------

# MP-000003's actual shape: one used sold comp, two new-with-tags asks.
MISMATCH = (
    comp(6800, band=ConditionBand.USED_EXCELLENT),
    comp(9900, PriceKind.ASKING, cid="a1"),
    comp(12500, PriceKind.ASKING, cid="a2"),
)


def test_out_of_band_sold_comp_no_longer_anchors_alone():
    rec = recommend(inp(comps=MISMATCH))
    assert rec.price_kind is PriceKind.ASKING
    assert rec.band_relation == "condition_matched_asks"
    assert rec.band_central_cents == 11200  # the matched asks, not the used sale
    assert rec.band_central_cents > 6800


def test_the_sold_evidence_is_retained_and_reported_not_discarded():
    rec = recommend(inp(comps=MISMATCH))
    assert rec.realized_off_band is not None
    assert rec.realized_off_band.median_cents == 6800
    assert rec.has(PriceQualifier.SOLD_EVIDENCE_OUT_OF_BAND)
    assert rec.has(PriceQualifier.POSITIONED_ON_ASKS)
    assert rec.has(PriceQualifier.CONDITION_MISMATCH)


def test_no_numerical_adjustment_is_invented_to_bridge_the_gap():
    rec = recommend(inp(comps=MISMATCH))
    assert rec.adjustments == ()
    assert rec.adjustment_factor == 1.0
    # every reported number is an observed statistic of an observed sample
    assert rec.band_central_cents == rec.asking_comparable.median_cents


def test_divergent_sold_evidence_is_flagged_not_applied():
    rec = recommend(inp(comps=MISMATCH))
    assert rec.has(PriceQualifier.SOLD_EVIDENCE_DIVERGES)
    ss = build_strategies(rec, schedule=VERIFIED)
    assert "no adjustment applied" in ss.sold_evidence_note


def test_matched_sold_comps_still_win_over_asks():
    rec = recommend(inp(comps=(
        comp(4000), comp(4200), comp(9900, PriceKind.ASKING, cid="a1"),
    )))
    assert rec.price_kind is PriceKind.REALIZED
    assert rec.band_relation == "condition_matched"
    assert not rec.has(PriceQualifier.POSITIONED_ON_ASKS)


def test_adjacent_bands_are_treated_as_one_market():
    assert condition_comparable(ConditionBand.NEW_WITH_TAGS, ConditionBand.NEW_WITHOUT_TAGS)
    assert not condition_comparable(ConditionBand.NEW_WITH_TAGS, ConditionBand.USED_GOOD)
    assert not condition_comparable(ConditionBand.NEW_WITH_TAGS, ConditionBand.UNKNOWN)
    rec = recommend(inp(comps=(
        comp(4000, band=ConditionBand.NEW_WITHOUT_TAGS),
        comp(4200, band=ConditionBand.NEW_WITH_TAGS),
    )))
    assert rec.n_in_basis == 2
    assert rec.has(PriceQualifier.ADJACENT_CONDITION_POOLED)


def test_out_of_band_sold_still_used_when_nothing_matches():
    rec = recommend(inp(comps=(comp(6800, band=ConditionBand.USED_EXCELLENT),)))
    assert rec.price_kind is PriceKind.REALIZED
    assert rec.band_relation == "condition_mismatched"
    assert rec.has(PriceQualifier.CONDITION_MISMATCH)
    assert not rec.has(PriceQualifier.POSITIONED_ON_ASKS)


def test_stale_asks_leave_the_sample_with_a_recorded_reason():
    rec = recommend(inp(comps=(
        comp(9900, PriceKind.ASKING, cid="fresh1", days_on_market=10),
        comp(10500, PriceKind.ASKING, cid="fresh2", days_on_market=20),
        comp(12500, PriceKind.ASKING, cid="stale", days_on_market=400),
    ), window_days=90))
    assert rec.has(PriceQualifier.STALE_ASKS_EXCLUDED)
    assert rec.n_in_basis == 2
    assert len(rec.sample_exclusions) == 1
    assert "400 days" in rec.sample_exclusions[0]


def test_stale_asks_are_kept_when_removing_them_would_gut_the_sample():
    rec = recommend(inp(comps=(
        comp(12500, PriceKind.ASKING, cid="stale", days_on_market=400),
    ), window_days=90))
    assert not rec.has(PriceQualifier.STALE_ASKS_EXCLUDED)
    assert rec.n_in_basis == 1


# --- strategies -----------------------------------------------------------------


def test_one_comp_set_yields_three_distinct_prices():
    rec = recommend(inp(comps=(
        comp(4000), comp(4400), comp(5000), comp(6000), comp(9000),
    )))
    ss = build_strategies(rec, schedule=VERIFIED)
    fast = ss.get(SellerObjective.FAST_SALE).price_cents
    bal = ss.get(SellerObjective.BALANCED).price_cents
    mx = ss.get(SellerObjective.MAX_PROCEEDS).price_cents
    assert fast < bal < mx


def test_every_strategy_price_is_an_observed_statistic():
    rec = recommend(inp(comps=(comp(4000), comp(4400), comp(5000), comp(6000))))
    d = rec.basis_distribution
    ss = build_strategies(rec, schedule=VERIFIED)
    observed = {d.min_cents, d.p25_cents, d.median_cents, d.p75_cents, d.max_cents}
    for objective in SellerObjective:
        assert ss.get(objective).price_cents in observed


def test_strategy_records_which_anchor_it_took():
    rec = recommend(inp(comps=(comp(4000), comp(4400), comp(5000), comp(6000))))
    ss = build_strategies(rec, schedule=VERIFIED)
    s = ss.get(SellerObjective.MAX_PROCEEDS)
    assert s.anchor.statistic is Statistic.P75
    assert s.anchor.price_kind is PriceKind.REALIZED
    assert "p75 of 4 realized comps" in s.anchor.describe()


def test_thin_samples_collapse_quartiles_to_the_extremes():
    rec = recommend(inp(comps=(comp(4000), comp(6000))))
    ss = build_strategies(rec, schedule=VERIFIED)
    assert ss.get(SellerObjective.FAST_SALE).price_cents == 4000
    assert ss.get(SellerObjective.MAX_PROCEEDS).price_cents == 6000


def test_cited_premium_brand_reaches_for_the_top_of_the_sample():
    rec = recommend(inp(comps=(comp(4000), comp(4400), comp(5000), comp(9000))))
    plain = build_strategies(rec, schedule=VERIFIED)
    premium = build_strategies(rec, schedule=VERIFIED, brand=BrandSignal(
        strength=BrandStrength.PREMIUM, citations=("ev_retail_398",),
        rationale="$398 swing tag",
    ))
    assert premium.get(SellerObjective.MAX_PROCEEDS).price_cents == 9000
    assert plain.get(SellerObjective.MAX_PROCEEDS).price_cents < 9000


def test_uncited_brand_strength_is_treated_as_unknown_and_says_so():
    rec = recommend(inp(comps=(comp(4000), comp(4400), comp(5000), comp(9000))))
    ss = build_strategies(rec, schedule=VERIFIED, brand=BrandSignal(
        strength=BrandStrength.PREMIUM, citations=(),
    ))
    assert ss.brand.effective is BrandStrength.UNKNOWN
    assert any("without a citation" in n for n in ss.notes)
    assert ss.get(SellerObjective.MAX_PROCEEDS).price_cents < 9000


def test_brand_strength_never_multiplies_anything():
    """It selects a statistic; it cannot produce a price the sample does not contain."""
    rec = recommend(inp(comps=(comp(4000), comp(4400), comp(5000), comp(9000))))
    d = rec.basis_distribution
    for strength in BrandStrength:
        ss = build_strategies(rec, schedule=VERIFIED, brand=BrandSignal(
            strength=strength, citations=("ev_1",)))
        for objective in SellerObjective:
            assert ss.get(objective).price_cents <= d.max_cents


def test_strategies_respect_the_net_proceeds_floor():
    rec = recommend(inp(comps=(comp(300), comp(400), comp(500), comp(900))))
    ss = build_strategies(
        rec, schedule=VERIFIED, minimum_net_proceeds_cents=500,
    )
    fast = ss.get(SellerObjective.FAST_SALE)
    assert fast.floor_bound
    assert fast.net_proceeds_cents >= 500
    assert "floor" in fast.selection_reason


def test_each_strategy_reports_its_net_and_its_tradeoff():
    rec = recommend(inp(comps=(comp(4000), comp(4400), comp(5000), comp(6000))))
    ss = build_strategies(
        rec, schedule=VERIFIED, costs=CostLines(seller_paid_shipping_cents=900),
    )
    for objective in SellerObjective:
        s = ss.get(objective)
        assert s.net_proceeds_cents < s.price_cents
        assert s.tradeoff


def test_max_proceeds_names_repricing_as_the_reason_it_is_safe():
    rec = recommend(inp(comps=(comp(4000), comp(6000))))
    ss = build_strategies(rec, schedule=VERIFIED)
    assert "reprice" in ss.get(SellerObjective.MAX_PROCEEDS).tradeoff


def test_default_objective_is_balanced_and_explicit():
    rec = recommend(inp(comps=(comp(4000), comp(6000))))
    assert build_strategies(rec, schedule=VERIFIED).default_objective is SellerObjective.BALANCED
    ss = build_strategies(
        rec, schedule=VERIFIED, default_objective=SellerObjective.MAX_PROCEEDS
    )
    assert ss.default_objective is SellerObjective.MAX_PROCEEDS


def test_uncertainty_is_stated_rather_than_smoothed_away():
    rec = recommend(inp(comps=MISMATCH))
    ss = build_strategies(rec, schedule=VERIFIED)
    note = ss.uncertainty_note
    assert "asking prices, not realized sales" in note
    assert "never resolved" in note


def test_unpriceable_evidence_yields_no_strategies():
    assert build_strategies(recommend(inp()), schedule=VERIFIED) is None


def test_strategies_on_the_mismatch_case_all_sit_above_the_used_sale():
    """The liquidation failure, closed: no objective anchors on the used comp."""
    rec = recommend(inp(comps=MISMATCH))
    ss = build_strategies(rec, schedule=VERIFIED)
    for objective in SellerObjective:
        assert ss.get(objective).price_cents >= 9900


def test_a_stale_top_of_range_is_called_out_against_max_proceeds():
    """The walkthrough case: the highest ask had sat 140 days."""
    rec = recommend(inp(comps=(
        comp(9900, PriceKind.ASKING, cid="a1", days_on_market=10),
        comp(12500, PriceKind.ASKING, cid="a2", days_on_market=140),
    ), window_days=90))
    ss = build_strategies(rec, schedule=VERIFIED)
    assert ss.get(SellerObjective.MAX_PROCEEDS).price_cents == 12500
    assert any("already declined" in n for n in ss.notes)


def test_no_stale_note_when_the_asks_are_fresh():
    rec = recommend(inp(comps=(
        comp(9900, PriceKind.ASKING, cid="a1", days_on_market=10),
        comp(12500, PriceKind.ASKING, cid="a2", days_on_market=20),
    ), window_days=90))
    ss = build_strategies(rec, schedule=VERIFIED)
    assert not any("already declined" in n for n in ss.notes)
