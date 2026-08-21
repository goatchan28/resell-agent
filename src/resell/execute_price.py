"""Executor: getting an approved price onto a live eBay listing.

This is the only place in the system that changes the price of something a buyer
can see. Everything it does is gated by decisions made elsewhere -- the proposal,
the approval, the floor, the fee basis -- and it makes none of its own.

Call shape, in order, and it stops at the first refusal:

    0 calls   already applied for this content hash        -> NO_CHANGE
    0 calls   no live approval / wrong state / no budget   -> REFUSED
    1 call    getOffer, validate, detect drift             -> REFUSED on drift
    2 calls   updateOffer with the whole offer resent
    3 calls   getOffer again, confirm the price is live    -> UNCONFIRMED on mismatch

The confirming read is not defensive padding. eBay does not return the offer from
`updateOffer`, so a 200 says the request was accepted and nothing more. Recording
`applied` off the back of a 200 would mean the database asserts a price the
listing may not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

from . import store_pricing as sp
from .pricing.lifecycle import PriceProposal, can_apply_price
from .pricing.offer import (
    DEFAULT_DAILY_REVISION_BUDGET,
    ResponseClass,
    RevisionBudget,
    build_update_payload,
    classify,
    describe_errors,
    listing_id,
    offer_price_cents,
    sold_quantity,
    validate_offer,
    verify_echo,
)
from .pricing.proceeds import FeeBasis


class OfferClient(Protocol):
    """The seam. Anything with these two methods can drive the executor.

    Implemented for real by `EbayOfferClient` over the project's authenticated
    HTTP client, and by a fake in the tests, which is how the read-modify-write
    and the drift refusal get exercised without a network.
    """

    def get_offer(self, offer_id: str) -> tuple[int, dict | None]: ...

    def update_offer(self, offer_id: str, payload: dict) -> tuple[int, dict | None]: ...


class ApplyOutcome(StrEnum):
    APPLIED = "applied"
    NO_CHANGE = "no_change"
    REFUSED = "refused"
    FAILED_PERMANENT = "failed_permanent"
    FAILED_TRANSIENT = "failed_transient"
    UNCONFIRMED = "unconfirmed"

    @property
    def is_retryable(self) -> bool:
        return self is ApplyOutcome.FAILED_TRANSIENT


@dataclass(frozen=True)
class ApplyResult:
    outcome: ApplyOutcome
    detail: str
    calls_made: int = 0
    marketplace_ref: str | None = None
    previous_price_cents: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (ApplyOutcome.APPLIED, ApplyOutcome.NO_CHANGE)

    def describe(self) -> str:
        return f"{self.outcome}: {self.detail} ({self.calls_made} call(s))"


def apply_price(
    conn,
    proposal: PriceProposal,
    *,
    client: OfferClient,
    offer_id: str,
    production: bool = False,
    currency: str = "USD",
    now: datetime | None = None,
    daily_revision_budget: int = DEFAULT_DAILY_REVISION_BUDGET,
    confirm: bool = True,
) -> ApplyResult:
    now = now or datetime.now(timezone.utc)

    # --- 0 calls: is there anything to do, and are we allowed to do it --------

    if sp.already_applied(conn, proposal.proposal_id):
        return ApplyResult(
            ApplyOutcome.NO_CHANGE,
            "this exact proposal is already live; nothing sent",
        )

    approval = sp.live_approval(conn, proposal.proposal_id)
    # Read, never accepted from the caller: the gate is worthless if the thing
    # being gated on is supplied by whoever wants through it.
    state = sp.item_state(conn, proposal.sku)
    ok, why = can_apply_price(proposal, approval, item_state=state)
    if not ok:
        return ApplyResult(ApplyOutcome.REFUSED, why)

    if production and proposal.fee_basis is FeeBasis.PROVISIONAL_ESTIMATE:
        return ApplyResult(
            ApplyOutcome.REFUSED,
            "production apply refuses a provisional fee basis",
        )

    budget = RevisionBudget(
        used_today=sp.revisions_today(conn, proposal.sku, now=now),
        budget=daily_revision_budget,
    )
    if budget.exhausted:
        return ApplyResult(
            ApplyOutcome.REFUSED,
            f"daily revision budget exhausted: {budget.describe()}",
        )

    state = sp.current_price_state(conn, proposal.sku)
    expected = state["live_price_cents"]

    # --- 1 call: read the offer we are about to replace ----------------------

    status, body = client.get_offer(offer_id)
    if classify(status) is not ResponseClass.OK or not isinstance(body, dict):
        return _fail(
            conn, proposal, status, body, calls=1, stage="getOffer",
        )

    valid, problem, reason = validate_offer(
        body, sku=proposal.sku, expected_price_cents=expected, currency=currency,
    )
    if not valid:
        _record_failure(conn, proposal, f"{problem}: {reason}", stage="precondition")
        return ApplyResult(ApplyOutcome.REFUSED, reason, calls_made=1)

    current = offer_price_cents(body)
    if current == proposal.price_cents:
        # The listing already carries this price under a different proposal.
        # Record it as applied so the database stops disagreeing with reality,
        # but send nothing.
        sp.record_applied(conn, proposal, marketplace_ref=listing_id(body))
        return ApplyResult(
            ApplyOutcome.NO_CHANGE,
            "listing already carries this price; recorded without sending",
            calls_made=1,
            marketplace_ref=listing_id(body),
        )

    if proposal.reason.is_reprice and sold_quantity(body) > 0:
        return ApplyResult(
            ApplyOutcome.REFUSED,
            f"listing already has {sold_quantity(body)} sold; repricing a listing "
            "with sales needs a decision, not an automatic write",
            calls_made=1,
        )

    # --- 2 calls: the whole offer back, one leaf changed ---------------------

    payload = build_update_payload(
        body, new_price_cents=proposal.price_cents, currency=currency
    )
    status, resp = client.update_offer(offer_id, payload)
    kind = classify(status)
    if kind is not ResponseClass.OK:
        return _fail(conn, proposal, status, resp, calls=2, stage="updateOffer")

    ref = listing_id(body)

    if not confirm:
        sp.record_applied(conn, proposal, marketplace_ref=ref)
        return ApplyResult(
            ApplyOutcome.APPLIED,
            f"accepted (unconfirmed by request); {describe_errors(resp) or 'no warnings'}",
            calls_made=2, marketplace_ref=ref, previous_price_cents=current,
        )

    # --- 3 calls: confirm, because a 200 is not evidence ---------------------

    status, after = client.get_offer(offer_id)
    if classify(status) is not ResponseClass.OK or not isinstance(after, dict):
        _record_failure(
            conn, proposal,
            f"update accepted but the confirming read failed with HTTP {status}",
            stage="confirm",
        )
        return ApplyResult(
            ApplyOutcome.UNCONFIRMED,
            "update was accepted but could not be confirmed; re-run to verify",
            calls_made=3, marketplace_ref=ref,
        )

    landed, note = verify_echo(after, expected_price_cents=proposal.price_cents)
    if not landed:
        _record_failure(conn, proposal, note, stage="confirm")
        return ApplyResult(ApplyOutcome.UNCONFIRMED, note, calls_made=3,
                           marketplace_ref=ref)

    sp.record_applied(conn, proposal, marketplace_ref=ref)
    warn = describe_errors(resp)
    return ApplyResult(
        ApplyOutcome.APPLIED,
        note + (f"; {warn}" if warn else ""),
        calls_made=3, marketplace_ref=ref, previous_price_cents=current,
    )


def _fail(conn, proposal, status: int, body: Any, *, calls: int, stage: str) -> ApplyResult:
    kind = classify(status)
    detail = f"{stage} returned HTTP {status}"
    errors = describe_errors(body if isinstance(body, dict) else None)
    if errors:
        detail += f": {errors}"
    _record_failure(conn, proposal, detail, stage=stage)
    outcome = (
        ApplyOutcome.FAILED_TRANSIENT
        if kind is ResponseClass.TRANSIENT
        else ApplyOutcome.FAILED_PERMANENT
    )
    if kind is ResponseClass.AUTH:
        detail += " -- the token needs refreshing, not the request retrying"
    return ApplyResult(outcome, detail, calls_made=calls)


def _record_failure(conn, proposal: PriceProposal, detail: str, *, stage: str) -> None:
    """A failed apply leaves the approval standing; only the attempt is recorded."""
    sp.record_apply_failed(conn, proposal, detail=detail, stage=stage)
