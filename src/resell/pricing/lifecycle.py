"""The price lifecycle, separate from the listing-content lifecycle.

Pure: no database, no model, no HTTP.

Price is expected to change after publication; listing content is not. Binding
them to one approval hash would mean every markdown voids the approval of a
title, a description and an identification that nobody touched -- and it would
make repricing indistinguishable from re-listing.

So there are two approvals over one item:

    listing approval   identification + aspects + title + description + photos
    price approval     one PriceProposal, hash-bound the same way

`publishable()` requires both to be live. Repricing replaces only the second.

A reprice is a new proposal with `reason=reprice_*` and a `supersedes` pointer,
approved through the same gate and applied through `updateOffer` -- the offer and
therefore the listing survive, which is what "update price without recreating the
listing" means at the eBay API level. Automated triggers are deliberately absent;
the transition they would fire already exists, so adding them later is a caller,
not a schema change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from ..domain import ItemState
from .comps import CompBasis, ConditionAdjustment, PriceKind
from .estimate import PriceQualifier
from .proceeds import FeeBasis
from .strategy import SellerObjective


class PriceState(StrEnum):
    UNPRICED = "unpriced"
    PROPOSED = "proposed"
    APPROVED = "approved"
    LIVE = "live"  # this price is the one on the marketplace right now


class PriceReason(StrEnum):
    INITIAL = "initial"
    REPRICE_OPERATOR = "reprice_operator"
    REPRICE_POLICY = "reprice_policy"  # reserved for automated triggers
    CORRECTION = "correction"

    @property
    def is_reprice(self) -> bool:
        return self in (PriceReason.REPRICE_OPERATOR, PriceReason.REPRICE_POLICY)


class PriceEventType(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    VOIDED = "voided"
    APPLIED = "applied"
    APPLY_FAILED = "apply_failed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class PriceProposal:
    """One proposed price with everything needed to reproduce it.

    The comp set is cited by id **and hash**: a proposal points at a frozen set,
    and if that set were ever rebuilt the mismatch is visible rather than silent.
    """

    proposal_id: str
    sku: str
    reason: PriceReason
    price_cents: int
    created_at: datetime
    basis: CompBasis | None = None
    price_kind: PriceKind | None = None
    comp_set_id: str | None = None
    comp_set_hash: str | None = None
    band_low_cents: int | None = None
    band_central_cents: int | None = None
    band_high_cents: int | None = None
    adjustments: tuple[ConditionAdjustment, ...] = ()
    qualifiers: tuple[PriceQualifier, ...] = ()
    fee_schedule_version: str = ""
    fee_basis: FeeBasis = FeeBasis.PROVISIONAL_ESTIMATE
    net_proceeds_cents: int | None = None
    floor_ok: bool = False
    rationale: str = ""
    supersedes: str | None = None
    previous_price_cents: int | None = None
    # Which seller objective produced this price, and the observed statistic it
    # was taken from. Both are decision-bearing: the same comp set under a
    # different objective is a different decision and must be approved again.
    objective: SellerObjective | None = None
    anchor_statistic: str | None = None
    anchor_value_cents: int | None = None

    def content_hash(self) -> str:
        """SHA-256 over the decision-bearing content, same pattern as elsewhere.

        `created_at`, ids and the rationale prose are excluded: re-narrating a
        decision is not changing it, but any change to the number, the evidence
        it rests on, or the fee model that made it acceptable voids the approval.
        """
        payload = {
            "sku": self.sku,
            "price_cents": self.price_cents,
            "basis": str(self.basis) if self.basis else None,
            "price_kind": str(self.price_kind) if self.price_kind else None,
            "comp_set_hash": self.comp_set_hash,
            "band": [self.band_low_cents, self.band_central_cents, self.band_high_cents],
            "adjustments": [
                [a.magnitude_pct, str(a.source), str(a.from_band), str(a.to_band), a.reason]
                for a in self.adjustments
            ],
            "qualifiers": sorted(str(q) for q in self.qualifiers),
            "fee_schedule_version": self.fee_schedule_version,
            "fee_basis": str(self.fee_basis),
            "net_proceeds_cents": self.net_proceeds_cents,
            "floor_ok": self.floor_ok,
            "objective": str(self.objective) if self.objective else None,
            "anchor": [self.anchor_statistic, self.anchor_value_cents],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PriceApproval:
    approval_id: str
    proposal_id: str
    content_hash: str
    approved_at: datetime
    approved_by: str = "operator"
    voided_at: datetime | None = None
    void_reason: str | None = None

    @property
    def is_live(self) -> bool:
        return self.voided_at is None

    def covers(self, proposal: PriceProposal) -> bool:
        return (
            self.is_live
            and self.proposal_id == proposal.proposal_id
            and self.content_hash == proposal.content_hash()
        )


@dataclass(frozen=True)
class RepricePolicy:
    """Present in V1, permissive in V1. The point is that the shape exists.

    When automated triggers arrive they tighten these numbers; they do not
    introduce a new concept, and the guard they need is already tested.
    """

    max_increase_pct: float = 1.00
    max_decrease_pct: float = 0.50
    min_hours_between_changes: float = 0.0
    require_floor_recheck: bool = True


# --- gates -------------------------------------------------------------------

# Pricing is legal once identification has settled, and stays legal on a live
# listing. Deliberately **not** derived from `domain.TERMINAL_STATES`, which
# contains LISTED: that set describes states from which no further *item*
# transition is legal, and repricing is precisely the operation that changes a
# live listing without moving the item at all. Reusing it here would make the
# reprice path unreachable. The two lifecycles are separate on purpose, and this
# is where that separation has to be stated rather than assumed.
PRICEABLE_ITEM_STATES = frozenset({
    ItemState.PRICING,
    ItemState.PROPOSED,
    ItemState.APPROVED,
    ItemState.PUBLISH_FAILED,  # a failed publish is often a price problem
    ItemState.LISTED,
})

# An initial price reaches eBay as part of publishing; a reprice needs something
# already live to revise.
INITIAL_APPLY_STATES = frozenset({
    ItemState.APPROVED, ItemState.PUBLISHING, ItemState.LISTED,
})
REPRICE_APPLY_STATES = frozenset({ItemState.LISTED})


def coerce_item_state(value: ItemState | str) -> ItemState:
    """Unknown states raise rather than failing closed.

    Failing closed on an unrecognised string is how `pricing` -- a real state the
    item was actually in -- silently read as "not priceable" for as long as this
    module kept its own copy of the vocabulary. A typo should be loud.
    """
    if isinstance(value, ItemState):
        return value
    try:
        return ItemState(value)
    except ValueError:
        known = ", ".join(sorted(s.value for s in ItemState))
        raise ValueError(
            f"{value!r} is not a known ItemState; expected one of: {known}"
        ) from None


def can_propose_price(item_state: ItemState | str) -> tuple[bool, str]:
    state = coerce_item_state(item_state)
    if state is ItemState.ABANDONED:
        return False, "item is abandoned; pricing it is meaningless"
    if state not in PRICEABLE_ITEM_STATES:
        return False, (
            f"item is {state}; pricing needs identification settled "
            f"(one of: {', '.join(sorted(s.value for s in PRICEABLE_ITEM_STATES))})"
        )
    return True, f"priceable in {state}"


def can_approve_price(
    proposal: PriceProposal, *, item_state: ItemState | str, production: bool
) -> tuple[bool, str]:
    ok, why = can_propose_price(item_state)
    if not ok:
        return False, why
    if not proposal.floor_ok:
        return False, "proposal does not clear the minimum net proceeds floor"
    if production and proposal.fee_basis is FeeBasis.PROVISIONAL_ESTIMATE:
        return False, (
            "production approval requires a verified fee basis; "
            f"schedule {proposal.fee_schedule_version!r} is provisional"
        )
    return True, "approvable"


def check_reprice(
    proposal: PriceProposal,
    *,
    policy: RepricePolicy,
    current_price_cents: int,
    last_change_at: datetime | None,
    now: datetime,
) -> tuple[bool, str]:
    """Deterministic guard on a price change. Not about taste; about blast radius."""
    if not proposal.reason.is_reprice:
        return False, f"{proposal.reason} is not a reprice"
    if current_price_cents <= 0:
        return False, "no current price to reprice from"
    if policy.require_floor_recheck and not proposal.floor_ok:
        return False, "reprice must be re-checked against the floor at the new price"

    delta = (proposal.price_cents - current_price_cents) / current_price_cents
    if delta > policy.max_increase_pct:
        return False, (
            f"increase of {delta:+.0%} exceeds the {policy.max_increase_pct:.0%} cap"
        )
    if delta < -policy.max_decrease_pct:
        return False, (
            f"decrease of {delta:+.0%} exceeds the {policy.max_decrease_pct:.0%} cap"
        )

    if last_change_at and policy.min_hours_between_changes:
        elapsed = now - last_change_at
        if elapsed < timedelta(hours=policy.min_hours_between_changes):
            return False, (
                f"last price change was {elapsed} ago; cooldown is "
                f"{policy.min_hours_between_changes}h"
            )
    return True, f"reprice of {delta:+.0%} within policy"


def can_apply_price(
    proposal: PriceProposal,
    approval: PriceApproval | None,
    *,
    item_state: ItemState | str,
) -> tuple[bool, str]:
    """Applying means calling the marketplace. Both the approval and the item gate it."""
    if approval is None or not approval.covers(proposal):
        return False, (
            "no live approval binds this proposal; the price or its evidence "
            "changed after approval"
        )
    state = coerce_item_state(item_state)
    if proposal.reason is PriceReason.INITIAL:
        if state not in INITIAL_APPLY_STATES:
            return False, f"initial price applies at publish; item is {state}"
    elif state not in REPRICE_APPLY_STATES:
        return False, f"a reprice requires a live listing; item is {state}"
    return True, f"applicable in {state}"


def publishable(
    *,
    listing_approval_live: bool,
    price_proposal: PriceProposal | None,
    price_approval: PriceApproval | None,
    production: bool,
) -> tuple[bool, str]:
    """Two approvals, one publish. Neither substitutes for the other."""
    if not listing_approval_live:
        return False, "listing content has no live approval"
    if price_proposal is None:
        return False, "item has no price proposal"
    if price_approval is None or not price_approval.covers(price_proposal):
        return False, "price has no live approval"
    if production and price_proposal.fee_basis is FeeBasis.PROVISIONAL_ESTIMATE:
        return False, "production publish refuses a provisional fee basis"
    return True, "listing and price both approved"


def apply_idempotency_key(sku: str, proposal_id: str) -> str:
    """Scopes one apply to one proposal.

    Not the content hash: two proposals can legitimately share content -- reprice
    to $59, then back to $112 on the same evidence -- and treating them as the
    same action skips a write the listing needs.
    """
    return f"price:{sku}:{proposal_id}"


def next_state(current: PriceState, event: PriceEventType) -> PriceState:
    """The whole price state machine, small enough to read in one go."""
    table: dict[tuple[PriceState, PriceEventType], PriceState] = {
        (PriceState.UNPRICED, PriceEventType.PROPOSED): PriceState.PROPOSED,
        (PriceState.PROPOSED, PriceEventType.APPROVED): PriceState.APPROVED,
        (PriceState.PROPOSED, PriceEventType.VOIDED): PriceState.UNPRICED,
        (PriceState.PROPOSED, PriceEventType.SUPERSEDED): PriceState.PROPOSED,
        (PriceState.APPROVED, PriceEventType.APPLIED): PriceState.LIVE,
        (PriceState.APPROVED, PriceEventType.APPLY_FAILED): PriceState.APPROVED,
        (PriceState.APPROVED, PriceEventType.VOIDED): PriceState.PROPOSED,
        (PriceState.APPROVED, PriceEventType.PROPOSED): PriceState.PROPOSED,
        (PriceState.LIVE, PriceEventType.PROPOSED): PriceState.LIVE,
        (PriceState.LIVE, PriceEventType.APPROVED): PriceState.LIVE,
        (PriceState.LIVE, PriceEventType.APPLIED): PriceState.LIVE,
        (PriceState.LIVE, PriceEventType.APPLY_FAILED): PriceState.LIVE,
    }
    try:
        return table[(current, event)]
    except KeyError:
        raise ValueError(f"{event} is not legal from {current}") from None
