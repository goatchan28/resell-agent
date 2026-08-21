"""Fees, net proceeds, and the inverse."""

from __future__ import annotations

import pytest

from resell.pricing.proceeds import (
    PROVISIONAL_DEFAULT,
    CostLines,
    FeeBasis,
    FeeSchedule,
    gross_from_net,
    meets_publication_floor,
    net_from_gross,
    proceeds_range,
    production_fee_basis_ok,
)

VERIFIED = FeeSchedule(
    version="ebay-us-clothing-2026-08", category_id="57988", rate=0.1335,
    fixed_cents=40, basis=FeeBasis.CATEGORY_VERIFIED,
    source_url="https://www.ebay.com/help/selling/fees-credits-invoices/selling-fees",
)


def test_default_reproduces_the_existing_placeholder_numbers():
    assert PROVISIONAL_DEFAULT.rate == 0.1335
    assert PROVISIONAL_DEFAULT.fixed_cents == 40
    assert PROVISIONAL_DEFAULT.fees_for(10000) == round(10000 * 0.1335) + 40


def test_fee_base_includes_buyer_paid_shipping():
    with_ship = VERIFIED.fees_for(10000, buyer_paid_shipping_cents=1000)
    without = VERIFIED.fees_for(10000)
    assert with_ship > without
    assert with_ship == round(11000 * 0.1335) + 40


def test_tax_inclusion_is_declared_not_guessed():
    off = VERIFIED.fees_for(10000, tax_cents=800)
    on = FeeSchedule(
        version="v", rate=0.1335, fixed_cents=40, includes_tax_in_base=True,
        basis=FeeBasis.CATEGORY_VERIFIED,
    ).fees_for(10000, tax_cents=800)
    assert off < on


def test_percentage_cap_applies_to_the_percentage_only():
    capped = FeeSchedule(
        version="capped", rate=0.1335, fixed_cents=40, cap_cents=1000,
        basis=FeeBasis.CATEGORY_VERIFIED,
    )
    assert capped.fees_for(100000) == 1000 + 40


def test_ad_rate_is_a_named_line_not_a_hidden_haircut():
    p = net_from_gross(10000, schedule=VERIFIED, costs=CostLines(ad_rate=0.02))
    assert p.ad_fee_cents == 200
    assert ("ad fee", -200) in p.breakdown()


def test_net_subtracts_every_line():
    p = net_from_gross(
        10000, schedule=VERIFIED,
        costs=CostLines(seller_paid_shipping_cents=900, packaging_cents=75, ad_rate=0.02),
    )
    expected = 10000 - (round(10000 * 0.1335) + 40) - 200 - 900 - 75
    assert p.net_cents == expected


def test_gross_from_net_round_trips():
    for target in (500, 1000, 2500, 7777, 50000):
        price = gross_from_net(target, schedule=VERIFIED)
        assert net_from_gross(price, schedule=VERIFIED).net_cents >= target
        # and it is the *smallest* such price
        assert net_from_gross(price - 1, schedule=VERIFIED).net_cents < target


def test_gross_from_net_round_trips_with_costs_and_shipping():
    costs = CostLines(seller_paid_shipping_cents=900, packaging_cents=75, ad_rate=0.02)
    price = gross_from_net(
        2000, schedule=VERIFIED, costs=costs, buyer_paid_shipping_cents=500
    )
    net = net_from_gross(
        price, schedule=VERIFIED, costs=costs, buyer_paid_shipping_cents=500
    ).net_cents
    assert net >= 2000
    assert net_from_gross(
        price - 1, schedule=VERIFIED, costs=costs, buyer_paid_shipping_cents=500
    ).net_cents < 2000


def test_impossible_rate_refuses_rather_than_dividing_by_zero():
    with pytest.raises(ValueError, match="no margin"):
        gross_from_net(
            1000,
            schedule=FeeSchedule(version="x", rate=0.9, basis=FeeBasis.CATEGORY_VERIFIED),
            costs=CostLines(ad_rate=0.2),
        )


def test_floor_uses_the_pessimistic_end_of_a_shipping_range():
    ok_optimistic, _ = meets_publication_floor(
        2000, schedule=VERIFIED, shipping_range_cents=(400, 400),
        minimum_net_proceeds_cents=1000,
    )
    ok_range, why = meets_publication_floor(
        2000, schedule=VERIFIED, shipping_range_cents=(400, 1200),
        minimum_net_proceeds_cents=1000,
    )
    assert ok_optimistic
    assert not ok_range
    assert "worst-case" in why


def test_proceeds_range_collapses_to_a_point_without_a_range():
    r = proceeds_range(5000, schedule=VERIFIED)
    assert r.is_point


def test_floor_ignores_purchase_cost_entirely():
    # zero-cost declutter items must remain publishable
    ok, _ = meets_publication_floor(2000, schedule=VERIFIED, minimum_net_proceeds_cents=500)
    assert ok


def test_production_refuses_a_provisional_fee_basis():
    ok, why = production_fee_basis_ok(PROVISIONAL_DEFAULT)
    assert not ok and "provisional" in why
    ok2, _ = production_fee_basis_ok(VERIFIED)
    assert ok2


def test_every_computation_records_the_schedule_version():
    p = net_from_gross(1000, schedule=VERIFIED)
    assert p.fee_schedule_version == "ebay-us-clothing-2026-08"
    assert p.fee_basis is FeeBasis.CATEGORY_VERIFIED
