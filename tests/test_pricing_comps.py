"""Comp vocabulary and claim legality."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from resell.pricing.comps import (
    AdjustmentSource,
    CompBasis,
    CompClaim,
    CompObservation,
    Comparability,
    ComparisonBasis,
    ConditionAdjustment,
    ConditionBand,
    CrossKindAdjustment,
    PriceKind,
    band_for_condition_id,
    ceiling_for_identity,
    ladder_steps,
    refuse_cross_kind,
    validate_adjustment,
    validate_claim,
)

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _obs(**kw) -> CompObservation:
    base = dict(
        comp_id="c1", marketplace="EBAY_US", external_id="1", price_kind=PriceKind.REALIZED,
        basis=CompBasis.SOLD_SIMILAR, price_cents=4000, observed_at=NOW,
    )
    base.update(kw)
    return CompObservation(**base)


def _claim(**kw) -> CompClaim:
    base = dict(
        claim_id="cl1", sku="MP-000003", comp_id="c1",
        comparability=Comparability.SAME_FAMILY_VARIANT,
        item_citations=("ev_1",), comp_citations=("comp_title",),
    )
    base.update(kw)
    return CompClaim(**base)


# --- comparison basis: the operator's decision, made mechanical ---------------


def test_known_shipping_compares_on_total_to_buyer():
    o = _obs(price_cents=4000, shipping_cents=800)
    assert o.comparison_basis is ComparisonBasis.TOTAL_TO_BUYER
    assert o.comparison_price_cents == 4800


def test_unknown_shipping_is_retained_not_excluded():
    o = _obs(price_cents=4500, shipping_cents=None)
    assert o.shipping_known is False
    assert o.comparison_basis is ComparisonBasis.ITEM_PRICE
    assert o.comparison_price_cents == 4500  # kept, and flagged downstream


def test_zero_shipping_is_not_unknown_shipping():
    assert _obs(shipping_cents=0).shipping_known is True


# --- identity ceiling ---------------------------------------------------------


def test_same_product_requires_resolved_identity():
    assert ceiling_for_identity("resolved") is Comparability.SAME_PRODUCT
    assert ceiling_for_identity("searched_not_found") is Comparability.SAME_FAMILY_VARIANT


def test_same_product_claim_refused_when_identity_unresolved():
    ok, why = validate_claim(
        _claim(comparability=Comparability.SAME_PRODUCT),
        identity_resolution="searched_not_found",
    )
    assert not ok
    assert "same_family_variant" in why


def test_same_product_claim_allowed_when_resolved():
    ok, _ = validate_claim(
        _claim(comparability=Comparability.SAME_PRODUCT), identity_resolution="resolved"
    )
    assert ok


# --- citation floor -----------------------------------------------------------


def test_claim_needs_item_citations():
    ok, why = validate_claim(_claim(item_citations=()), identity_resolution="resolved")
    assert not ok and "item evidence" in why


def test_claim_needs_comp_citations():
    ok, why = validate_claim(_claim(comp_citations=()), identity_resolution="resolved")
    assert not ok and "comp fields" in why


def test_exclusion_requires_a_reason():
    ok, why = validate_claim(
        _claim(comparability=Comparability.EXCLUDED, excluded_reason=None),
        identity_resolution="resolved",
    )
    assert not ok and "why it was excluded" in why


def test_exclusion_with_reason_is_legal_and_contributes_nothing():
    c = _claim(comparability=Comparability.EXCLUDED, excluded_reason="wrong size")
    ok, _ = validate_claim(c, identity_resolution="resolved")
    assert ok and c.contributes is False


def test_superficial_is_retained_but_contributes_nothing():
    assert Comparability.SUPERFICIAL.contributes is False
    assert Comparability.CATEGORY_ATTRIBUTE.contributes is True


# --- condition ladder ---------------------------------------------------------


def test_condition_ids_map_to_bands():
    assert band_for_condition_id(1000) is ConditionBand.NEW_WITH_TAGS
    assert band_for_condition_id(7000) is ConditionBand.FOR_PARTS
    assert band_for_condition_id(None) is ConditionBand.UNKNOWN
    assert band_for_condition_id(99999) is ConditionBand.UNKNOWN


def test_unknown_band_has_no_ladder_position():
    assert ConditionBand.UNKNOWN.ordinal is None
    assert ladder_steps(ConditionBand.UNKNOWN, ConditionBand.USED_GOOD) is None


# --- adjustment caps are asymmetric on purpose --------------------------------


def _adj(pct: float, **kw) -> ConditionAdjustment:
    base = dict(
        magnitude_pct=pct, reason="tags attached", source=AdjustmentSource.MODEL_PROPOSED,
        from_band=ConditionBand.NEW_WITHOUT_TAGS, to_band=ConditionBand.NEW_WITH_TAGS,
        citations=("ev_7",),
    )
    base.update(kw)
    return ConditionAdjustment(**base)


def test_small_upward_adjustment_allowed():
    ok, _ = validate_adjustment(_adj(0.10))
    assert ok


def test_large_upward_adjustment_refused_more_tightly_than_downward():
    ok_up, _ = validate_adjustment(_adj(0.25))
    ok_down, _ = validate_adjustment(_adj(-0.25))
    assert not ok_up  # optimism is the failure mode
    assert ok_down


def test_downward_adjustment_still_capped():
    ok, why = validate_adjustment(_adj(-0.40))
    assert not ok and "exceeds" in why


def test_adjustment_across_too_many_ladder_steps_refused():
    ok, why = validate_adjustment(
        _adj(0.10, from_band=ConditionBand.FOR_PARTS, to_band=ConditionBand.NEW_WITH_TAGS)
    )
    assert not ok and "ladder steps" in why


def test_model_proposed_adjustment_must_cite():
    ok, why = validate_adjustment(_adj(0.05, citations=()))
    assert not ok and "cite" in why


def test_derived_adjustment_must_record_its_sample():
    ok, why = validate_adjustment(
        _adj(0.05, source=AdjustmentSource.DERIVED_FROM_SET, derived_n=None)
    )
    assert not ok and "sample" in why


def test_adjustment_needs_a_reason():
    ok, why = validate_adjustment(_adj(0.05, reason="   "))
    assert not ok and "reason" in why


def test_cannot_adjust_across_an_unknown_band():
    ok, why = validate_adjustment(_adj(0.05, from_band=ConditionBand.UNKNOWN))
    assert not ok and "unknown condition band" in why


# --- V1 refuses ask-to-sold conversion outright -------------------------------


def test_asking_cannot_be_converted_into_realized():
    with pytest.raises(CrossKindAdjustment, match="does not permit"):
        refuse_cross_kind(PriceKind.ASKING, PriceKind.REALIZED)


def test_same_kind_passes_through():
    refuse_cross_kind(PriceKind.REALIZED, PriceKind.REALIZED)


def test_basis_knows_its_kind():
    assert CompBasis.SOLD_EXACT.price_kind is PriceKind.REALIZED
    assert CompBasis.ACTIVE_SIMILAR.price_kind is PriceKind.ASKING
    assert CompBasis.RETAIL_REFERENCE.price_kind is PriceKind.REFERENCE
