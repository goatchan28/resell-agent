"""The price lifecycle, and its separation from listing-content approval."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from resell.pricing.comps import CompBasis, PriceKind
from resell.pricing.lifecycle import (
    PriceApproval,
    PriceEventType,
    PriceProposal,
    PriceReason,
    PriceState,
    RepricePolicy,
    apply_idempotency_key,
    can_apply_price,
    can_approve_price,
    can_propose_price,
    check_reprice,
    next_state,
    publishable,
)
from resell.pricing.proceeds import FeeBasis

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def proposal(**kw) -> PriceProposal:
    base = dict(
        proposal_id="pp1", sku="MP-000003", reason=PriceReason.INITIAL,
        price_cents=4500, created_at=NOW, basis=CompBasis.SOLD_SIMILAR,
        price_kind=PriceKind.REALIZED, comp_set_hash="abc123",
        fee_schedule_version="ebay-us-clothing-2026-08",
        fee_basis=FeeBasis.CATEGORY_VERIFIED, net_proceeds_cents=3400, floor_ok=True,
    )
    base.update(kw)
    return PriceProposal(**base)


def approval_for(p: PriceProposal, **kw) -> PriceApproval:
    base = dict(
        approval_id="pa1", proposal_id=p.proposal_id, content_hash=p.content_hash(),
        approved_at=NOW,
    )
    base.update(kw)
    return PriceApproval(**base)


# --- hash binding --------------------------------------------------------------


def test_hash_is_stable_across_narration():
    a = proposal(rationale="three sold comps")
    b = proposal(rationale="rewritten explanation of the same three comps")
    assert a.content_hash() == b.content_hash()


def test_changing_the_price_voids_the_approval():
    p = proposal()
    app = approval_for(p)
    assert app.covers(p)
    assert not app.covers(replace(p, price_cents=5500))


def test_changing_the_evidence_voids_the_approval():
    p = proposal()
    app = approval_for(p)
    assert not app.covers(replace(p, comp_set_hash="def456"))


def test_changing_the_fee_model_voids_the_approval():
    p = proposal()
    app = approval_for(p)
    assert not app.covers(replace(p, fee_schedule_version="ebay-us-2027-01"))


def test_voided_approval_covers_nothing():
    p = proposal()
    app = approval_for(p, voided_at=NOW, void_reason="operator changed their mind")
    assert not app.covers(p)


# --- proposal and approval gates ------------------------------------------------


def test_abandoned_items_cannot_be_priced():
    ok, why = can_propose_price("abandoned")
    assert not ok and "abandoned" in why


def test_listed_items_can_be_repriced():
    ok, _ = can_propose_price("listed")
    assert ok


def test_approval_requires_clearing_the_floor():
    ok, why = can_approve_price(
        proposal(floor_ok=False), item_state="pricing", production=False
    )
    assert not ok and "floor" in why


def test_production_approval_refuses_a_provisional_fee_basis():
    ok, why = can_approve_price(
        proposal(fee_basis=FeeBasis.PROVISIONAL_ESTIMATE),
        item_state="pricing", production=True,
    )
    assert not ok and "provisional" in why


def test_sandbox_approval_tolerates_a_provisional_fee_basis():
    ok, _ = can_approve_price(
        proposal(fee_basis=FeeBasis.PROVISIONAL_ESTIMATE),
        item_state="pricing", production=False,
    )
    assert ok


# --- price is not coupled to listing-content approval --------------------------


def test_publish_needs_both_approvals():
    p = proposal()
    ok, why = publishable(
        listing_approval_live=True, price_proposal=p, price_approval=None,
        production=False,
    )
    assert not ok and "price has no live approval" in why

    ok2, why2 = publishable(
        listing_approval_live=False, price_proposal=p, price_approval=approval_for(p),
        production=False,
    )
    assert not ok2 and "listing content" in why2

    ok3, _ = publishable(
        listing_approval_live=True, price_proposal=p, price_approval=approval_for(p),
        production=False,
    )
    assert ok3


def test_repricing_does_not_touch_listing_content_approval():
    """The whole point of the split: a markdown must not void a title's approval."""
    first = proposal()
    second = proposal(
        proposal_id="pp2", reason=PriceReason.REPRICE_OPERATOR, price_cents=3900,
        previous_price_cents=4500, supersedes="pp1",
    )
    # the listing approval is an input here and is unchanged by the new price
    ok, _ = publishable(
        listing_approval_live=True, price_proposal=second,
        price_approval=approval_for(second, approval_id="pa2"), production=False,
    )
    assert ok
    assert first.content_hash() != second.content_hash()


# --- reprice policy -------------------------------------------------------------


def test_reprice_within_policy_allowed():
    p = proposal(reason=PriceReason.REPRICE_OPERATOR, price_cents=3900)
    ok, why = check_reprice(
        p, policy=RepricePolicy(), current_price_cents=4500, last_change_at=None, now=NOW
    )
    assert ok and "-13%" in why


def test_reprice_beyond_the_decrease_cap_refused():
    p = proposal(reason=PriceReason.REPRICE_OPERATOR, price_cents=1000)
    ok, why = check_reprice(
        p, policy=RepricePolicy(max_decrease_pct=0.25), current_price_cents=4500,
        last_change_at=None, now=NOW,
    )
    assert not ok and "exceeds" in why


def test_reprice_cooldown_enforced_when_configured():
    p = proposal(reason=PriceReason.REPRICE_OPERATOR, price_cents=4200)
    ok, why = check_reprice(
        p, policy=RepricePolicy(min_hours_between_changes=24),
        current_price_cents=4500, last_change_at=NOW - timedelta(hours=2), now=NOW,
    )
    assert not ok and "cooldown" in why


def test_reprice_must_be_rechecked_against_the_floor():
    p = proposal(reason=PriceReason.REPRICE_OPERATOR, price_cents=900, floor_ok=False)
    ok, why = check_reprice(
        p, policy=RepricePolicy(), current_price_cents=4500, last_change_at=None, now=NOW
    )
    assert not ok and "floor" in why


def test_an_initial_proposal_is_not_a_reprice():
    ok, why = check_reprice(
        proposal(), policy=RepricePolicy(), current_price_cents=4500,
        last_change_at=None, now=NOW,
    )
    assert not ok and "not a reprice" in why


def test_v1_policy_defaults_are_permissive():
    pol = RepricePolicy()
    assert pol.min_hours_between_changes == 0.0
    ok, _ = check_reprice(
        proposal(reason=PriceReason.REPRICE_OPERATOR, price_cents=3000),
        policy=pol, current_price_cents=4500, last_change_at=NOW, now=NOW,
    )
    assert ok


# --- applying -------------------------------------------------------------------


def test_reprice_requires_a_live_listing():
    p = proposal(reason=PriceReason.REPRICE_OPERATOR, price_cents=3900)
    ok, why = can_apply_price(p, approval_for(p), item_state="pricing")
    assert not ok and "live listing" in why


def test_apply_refused_without_a_binding_approval():
    p = proposal()
    stale = approval_for(p, content_hash="stale")
    ok, why = can_apply_price(p, stale, item_state="approved")
    assert not ok and "changed after approval" in why


def test_apply_allowed_for_a_reprice_on_a_listed_item():
    p = proposal(reason=PriceReason.REPRICE_OPERATOR, price_cents=3900)
    ok, _ = can_apply_price(p, approval_for(p), item_state="listed")
    assert ok


def test_idempotency_key_scopes_one_apply_to_one_proposal():
    p = proposal()
    assert apply_idempotency_key(p.sku, p.proposal_id) == \
        apply_idempotency_key(p.sku, replace(p, rationale="reworded").proposal_id)
    assert apply_idempotency_key(p.sku, "pp2") != apply_idempotency_key(p.sku, "pp1")


def test_two_proposals_may_share_content_and_are_still_separate_actions():
    """Reprice to $59 and back to $112: same evidence, same hash, two writes."""
    first = proposal(proposal_id="pp1", price_cents=11200)
    back = proposal(proposal_id="pp3", price_cents=11200,
                    reason=PriceReason.REPRICE_OPERATOR)
    assert first.content_hash() == back.content_hash()
    assert apply_idempotency_key(first.sku, first.proposal_id) != \
        apply_idempotency_key(back.sku, back.proposal_id)


# --- state machine ----------------------------------------------------------------


def test_happy_path_states():
    s = PriceState.UNPRICED
    s = next_state(s, PriceEventType.PROPOSED)
    assert s is PriceState.PROPOSED
    s = next_state(s, PriceEventType.APPROVED)
    assert s is PriceState.APPROVED
    s = next_state(s, PriceEventType.APPLIED)
    assert s is PriceState.LIVE


def test_voiding_an_approval_reverts_to_proposed_not_to_nothing():
    """The missing-state-reversion bug, not repeated."""
    assert next_state(PriceState.APPROVED, PriceEventType.VOIDED) is PriceState.PROPOSED


def test_a_live_price_survives_a_new_proposal_until_it_is_applied():
    assert next_state(PriceState.LIVE, PriceEventType.PROPOSED) is PriceState.LIVE
    assert next_state(PriceState.LIVE, PriceEventType.APPROVED) is PriceState.LIVE


def test_failed_apply_does_not_lose_the_approval():
    assert next_state(PriceState.APPROVED, PriceEventType.APPLY_FAILED) is PriceState.APPROVED


def test_illegal_transition_refused():
    with pytest.raises(ValueError, match="not legal"):
        next_state(PriceState.UNPRICED, PriceEventType.APPLIED)


def test_changing_the_objective_voids_the_approval():
    """Same evidence, different objective, different decision."""
    from resell.pricing.strategy import SellerObjective
    p = proposal(objective=SellerObjective.BALANCED, anchor_statistic="median",
                 anchor_value_cents=4500)
    app = approval_for(p)
    assert app.covers(p)
    assert not app.covers(replace(p, objective=SellerObjective.MAX_PROCEEDS))
    assert not app.covers(replace(p, anchor_statistic="p75", anchor_value_cents=6000))


def test_listed_is_terminal_for_the_item_but_not_for_its_price():
    """domain.TERMINAL_STATES contains LISTED; repricing depends on it not applying.

    Deriving the pricing gates from that set would have made every reprice
    unreachable, which is the whole feature.
    """
    from resell.domain import TERMINAL_STATES, ItemState
    from resell.pricing.lifecycle import PRICEABLE_ITEM_STATES, REPRICE_APPLY_STATES

    assert ItemState.LISTED in TERMINAL_STATES
    assert ItemState.LISTED in PRICEABLE_ITEM_STATES
    assert REPRICE_APPLY_STATES == {ItemState.LISTED}


def test_the_state_that_started_this_is_priceable():
    ok, _ = can_propose_price("pricing")
    assert ok


def test_an_unknown_state_raises_instead_of_failing_closed():
    with pytest.raises(ValueError, match="not a known ItemState"):
        can_propose_price("drafted")
