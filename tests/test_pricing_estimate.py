"""Distributions, qualifiers, and the recommendation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from resell.pricing.comps import (
    AdjustmentSource,
    CompBasis,
    CompClaim,
    CompObservation,
    Comparability,
    ConditionAdjustment,
    ConditionBand,
    PriceKind,
    RetailKind,
)
from resell.pricing.estimate import (
    PriceQualifier,
    PricingInput,
    RetailReference,
    ScoredComp,
    check_price_language,
    recommend,
    retail_ceiling_check,
    summarize,
)

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def comp(
    price, kind=PriceKind.REALIZED, band=ConditionBand.NEW_WITH_TAGS, *,
    shipping=0, comparability=Comparability.SAME_FAMILY_VARIANT, age_days=1,
    days_on_market=None, cid=None,
) -> ScoredComp:
    cid = cid or f"c{price}{kind}{band}{shipping}{age_days}"
    obs = CompObservation(
        comp_id=cid, marketplace="EBAY_US", external_id=cid, price_kind=kind,
        basis=CompBasis.SOLD_SIMILAR if kind is PriceKind.REALIZED else CompBasis.ACTIVE_SIMILAR,
        price_cents=price, observed_at=NOW - timedelta(days=age_days),
        condition_band=band, shipping_cents=shipping, days_on_market=days_on_market,
    )
    claim = CompClaim(
        claim_id=f"cl_{cid}", sku="MP-000003", comp_id=cid, comparability=comparability,
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


# --- distribution mechanics ---------------------------------------------------


def test_median_not_mean_so_one_outlier_does_not_move_the_centre():
    d = summarize([4000, 4500, 19000], PriceKind.REALIZED)
    assert d.median_cents == 4500
    assert d.max_cents == 19000


def test_retail_is_rejected_at_the_distribution_boundary():
    with pytest.raises(ValueError, match="context only"):
        summarize([39800], PriceKind.REFERENCE)


def test_empty_sample_refused():
    with pytest.raises(ValueError):
        summarize([], PriceKind.REALIZED)


def test_wide_dispersion_detected_on_small_samples_by_range():
    d = summarize([4000, 4500, 19000], PriceKind.REALIZED)
    assert d.is_widely_dispersed


def test_tight_sample_is_not_widely_dispersed():
    d = summarize([4000, 4100, 4200, 4300], PriceKind.REALIZED)
    assert not d.is_widely_dispersed


# --- realized and asking are never pooled -------------------------------------


def test_realized_and_asking_summarised_separately():
    rec = recommend(inp(comps=(
        comp(4000), comp(4200), comp(9000, PriceKind.ASKING), comp(9500, PriceKind.ASKING),
    )))
    assert rec.realized.n == 2 and rec.asking.n == 2
    assert rec.price_kind is PriceKind.REALIZED
    # the asking prices did not drag the centre upward
    assert rec.band_central_cents == 4100


def test_asking_only_is_labelled_and_still_priced():
    rec = recommend(inp(comps=(
        comp(9000, PriceKind.ASKING), comp(9500, PriceKind.ASKING),
    )))
    assert not rec.unpriceable
    assert rec.price_kind is PriceKind.ASKING
    assert rec.has(PriceQualifier.ASKING_ONLY)
    assert rec.basis is CompBasis.ACTIVE_SIMILAR
    assert "listed at" in rec.describe()


def test_asking_output_never_says_market_price():
    ok, why = check_price_language("the market price is $95", PriceKind.ASKING)
    assert not ok and "listed at" in why
    ok2, _ = check_price_language("comps sold for $95", PriceKind.REALIZED)
    assert ok2


def test_realized_preferred_over_asking_even_when_asking_is_larger():
    rec = recommend(inp(comps=(
        comp(4000), *[comp(9000 + i, PriceKind.ASKING, cid=f"a{i}") for i in range(5)],
    )))
    assert rec.price_kind is PriceKind.REALIZED
    assert rec.has(PriceQualifier.SINGLE_COMP)


# --- condition ----------------------------------------------------------------


def test_stratifies_to_the_items_own_band_when_it_can():
    rec = recommend(inp(comps=(
        comp(4000, band=ConditionBand.NEW_WITH_TAGS),
        comp(4100, band=ConditionBand.NEW_WITH_TAGS),
        comp(1000, band=ConditionBand.USED_GOOD),
    )))
    assert rec.band_central_cents == 4050  # the used comp did not enter the centre
    assert not rec.has(PriceQualifier.CONDITION_MISMATCH)


def test_condition_mismatch_flagged_when_no_in_band_comps():
    rec = recommend(inp(comps=(comp(1000, band=ConditionBand.USED_GOOD),)))
    assert rec.has(PriceQualifier.CONDITION_MISMATCH)


def test_unknown_condition_in_sample_flagged():
    rec = recommend(inp(comps=(
        comp(4000), comp(4200, band=ConditionBand.UNKNOWN),
    )))
    assert rec.has(PriceQualifier.CONDITION_UNKNOWN_IN_SAMPLE)


def test_valid_adjustment_scales_the_whole_band():
    adj = ConditionAdjustment(
        magnitude_pct=0.10, reason="tags attached, comps are used",
        source=AdjustmentSource.MODEL_PROPOSED, from_band=ConditionBand.NEW_WITHOUT_TAGS,
        to_band=ConditionBand.NEW_WITH_TAGS, citations=("ev_7",),
    )
    rec = recommend(inp(
        comps=(comp(4000), comp(5000)), adjustments=(adj,),
    ))
    assert rec.has(PriceQualifier.ADJUSTED)
    assert rec.band_central_cents == 4950


def test_invalid_adjustment_refused_at_estimate_time():
    bad = ConditionAdjustment(
        magnitude_pct=0.50, reason="feels right", source=AdjustmentSource.MODEL_PROPOSED,
        from_band=ConditionBand.NEW_WITHOUT_TAGS, to_band=ConditionBand.NEW_WITH_TAGS,
        citations=("ev_7",),
    )
    with pytest.raises(ValueError, match="invalid condition adjustment"):
        recommend(inp(comps=(comp(4000),), adjustments=(bad,)))


# --- shipping and basis mixing ------------------------------------------------


def test_unknown_shipping_retained_and_flagged():
    c = comp(4500)
    c = ScoredComp(claim=c.claim, observation=type(c.observation)(
        **{**c.observation.__dict__, "shipping_cents": None}))
    rec = recommend(inp(comps=(comp(4000, shipping=800), c)))
    assert rec.n_included == 2  # kept, not dropped
    assert rec.has(PriceQualifier.SHIPPING_UNKNOWN)
    assert rec.has(PriceQualifier.MIXED_COMPARISON_BASIS)


def test_total_to_buyer_used_when_shipping_known():
    rec = recommend(inp(comps=(comp(4000, shipping=800), comp(4000, shipping=1000, cid="x"))))
    assert rec.band_central_cents == 4900


# --- sample-shape qualifiers ---------------------------------------------------


def test_single_comp_says_so():
    rec = recommend(inp(comps=(comp(4000),)))
    assert rec.has(PriceQualifier.SINGLE_COMP)
    assert "1 comp" in rec.describe()


def test_low_count_does_not_refuse_to_price():
    rec = recommend(inp(comps=(comp(4000),)))
    assert not rec.unpriceable
    assert rec.band_central_cents == 4000


def test_wide_dispersion_qualifier_surfaces():
    rec = recommend(inp(comps=(comp(4000), comp(4500), comp(19000))))
    assert rec.has(PriceQualifier.WIDE_DISPERSION)


def test_identity_and_same_product_qualifiers():
    rec = recommend(inp(comps=(comp(4000),)))
    assert rec.has(PriceQualifier.IDENTITY_UNRESOLVED)
    assert rec.has(PriceQualifier.NO_SAME_PRODUCT_COMPS)
    assert rec.identity_ceiling is Comparability.SAME_FAMILY_VARIANT


def test_stale_comps_flagged_against_the_window():
    rec = recommend(inp(comps=(comp(4000, age_days=200),), window_days=90))
    assert rec.has(PriceQualifier.STALE_COMPS)


def test_long_days_on_market_flagged_for_asks():
    rec = recommend(inp(comps=(
        comp(9000, PriceKind.ASKING, days_on_market=180),
    ), window_days=90))
    assert rec.has(PriceQualifier.LONG_DAYS_ON_MARKET)


def test_excluded_and_superficial_comps_counted_but_not_used():
    rec = recommend(inp(comps=(
        comp(4000),
        comp(99999, comparability=Comparability.SUPERFICIAL, cid="sup"),
    )))
    assert rec.n_included == 1 and rec.n_excluded == 1
    assert rec.band_central_cents == 4000
    assert rec.comparability_profile["superficial"] == 1


# --- retail is context only ----------------------------------------------------


def test_retail_only_is_unpriceable_but_keeps_the_context():
    rec = recommend(inp(retail=(
        RetailReference(price_cents=39800, kind=RetailKind.ORIGINAL),
    )))
    assert rec.unpriceable
    assert rec.has(PriceQualifier.RETAIL_ONLY)
    assert rec.retail_context[0].price_cents == 39800
    assert rec.band_central_cents is None


def test_original_retail_is_not_a_ceiling():
    ok, _ = retail_ceiling_check(
        50000, (RetailReference(price_cents=39800, kind=RetailKind.ORIGINAL),)
    )
    assert ok  # a swing tag of unknown date says nothing about today


def test_current_retail_breach_is_flagged_not_refused():
    rec = recommend(inp(
        comps=(comp(50000), comp(51000)),
        retail=(RetailReference(price_cents=39800, kind=RetailKind.CURRENT),),
    ))
    assert not rec.unpriceable
    assert rec.has(PriceQualifier.ABOVE_RETAIL_CEILING)


def test_nothing_at_all_is_unpriceable():
    rec = recommend(inp())
    assert rec.unpriceable
    assert "ask the operator" in rec.reason


def test_confidence_is_diagnostic_and_bounded():
    rec = recommend(inp(comps=(comp(4000), comp(4100), comp(4200))))
    assert 0.0 <= rec.diagnostic_confidence <= 1.0


def test_used_comps_cannot_be_adjusted_up_to_new_with_tags():
    """The MP-000003 shape: a NWT item with only used comps.

    Adjusting used_good up to new_with_tags is five ladder steps and the cap
    refuses it, which is correct -- that gap is not an adjustment, it is a
    different question. The honest output is the used distribution carrying
    `condition_mismatch`, and the operator decides.
    """
    adj = ConditionAdjustment(
        magnitude_pct=0.15, reason="item has tags, comps do not",
        source=AdjustmentSource.MODEL_PROPOSED, from_band=ConditionBand.USED_GOOD,
        to_band=ConditionBand.NEW_WITH_TAGS, citations=("ev_7",),
    )
    with pytest.raises(ValueError, match="ladder steps"):
        recommend(inp(comps=(comp(2000, band=ConditionBand.USED_GOOD),),
                      adjustments=(adj,)))

    unadjusted = recommend(inp(comps=(comp(2000, band=ConditionBand.USED_GOOD),)))
    assert unadjusted.has(PriceQualifier.CONDITION_MISMATCH)
    assert unadjusted.band_central_cents == 2000


def test_summary_counts_the_comps_that_produced_the_band_not_the_whole_set():
    """Five comps included, three of them produced the band."""
    rec = recommend(inp(comps=(
        comp(4000), comp(4200, cid="r2"), comp(4400, cid="r3"),
        comp(12500, PriceKind.ASKING, cid="a1"),
        comp(9900, PriceKind.ASKING, cid="a2"),
    )))
    assert rec.n_included == 5
    assert rec.n_in_basis == 3
    assert "3 of 5 comps" in rec.describe()


def test_mismatch_case_reports_the_ask_count_that_set_the_price():
    """The old rule centred this on one used sale; the count now tracks the asks."""
    rec = recommend(inp(comps=(
        comp(6800, band=ConditionBand.USED_EXCELLENT),
        comp(12500, PriceKind.ASKING, cid="a1"),
        comp(9900, PriceKind.ASKING, cid="a2"),
    )))
    assert rec.n_in_basis == 2
    assert rec.price_kind is PriceKind.ASKING
    assert "2 of 3 comps listed at" in rec.describe()
    assert not rec.has(PriceQualifier.SINGLE_COMP)


def test_summary_omits_the_of_clause_when_every_comp_counted():
    rec = recommend(inp(comps=(comp(4000), comp(4200))))
    assert rec.n_in_basis == rec.n_included == 2
    assert " of " not in rec.describe()
