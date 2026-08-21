"""The apply executor, against a fake offer client.

The fake mirrors the parts of eBay's behaviour that matter here: updateOffer
replaces rather than patches, and returns nothing useful. That is enough to catch
the failure this module exists to prevent -- a price change that quietly strips the
listing policies off a live listing.
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from resell import store_pricing as sp
from resell.execute_price import ApplyOutcome, apply_price
from resell.pricing.comps import CompBasis, PriceKind
from resell.pricing.lifecycle import PriceProposal, PriceReason
from resell.pricing.offer import (
    build_update_payload,
    cents_to_ebay,
    classify,
    ebay_to_cents,
    validate_offer,
    verify_echo,
)
from resell.pricing.proceeds import FeeBasis

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
OFFER_ID = "offer-88231"

# What getOffer actually returns for a published, policy-bearing offer.
LIVE_OFFER = {
    "offerId": OFFER_ID,
    "sku": "MP-000003",
    "marketplaceId": "EBAY_US",
    "format": "FIXED_PRICE",
    "status": "PUBLISHED",
    "listing": {"listingId": "110588123456", "listingStatus": "ACTIVE",
                "soldQuantity": 0},
    "availableQuantity": 1,
    "categoryId": "57988",
    "listingDescription": "<p>Explorer Slim blazer, new with tags.</p>",
    "listingPolicies": {
        "fulfillmentPolicyId": "6209....", "paymentPolicyId": "6209....",
        "returnPolicyId": "6209....",
    },
    "merchantLocationKey": "home-1",
    "includeCatalogProductDetails": True,
    "pricingSummary": {"price": {"value": "112.00", "currency": "USD"}},
    "tax": {"applyTax": False},
}


class FakeOfferClient:
    """updateOffer replaces the offer wholesale, exactly as eBay's does."""

    def __init__(self, offer: dict | None = None, *, update_status: int = 200,
                 get_status: int = 200, update_body: dict | None = None):
        self.offer = copy.deepcopy(offer if offer is not None else LIVE_OFFER)
        self.update_status = update_status
        self.get_status = get_status
        self.update_body = update_body
        self.calls: list[str] = []
        self.payloads: list[dict] = []

    def get_offer(self, offer_id):
        self.calls.append(f"get:{offer_id}")
        if self.get_status != 200:
            return self.get_status, {"errors": [{"errorId": 2003, "message": "boom"}]}
        return 200, copy.deepcopy(self.offer)

    def update_offer(self, offer_id, payload):
        self.calls.append(f"put:{offer_id}")
        self.payloads.append(copy.deepcopy(payload))
        if self.update_status >= 300:
            return self.update_status, (
                self.update_body
                or {"errors": [{"errorId": 25002, "message": "invalid price"}]}
            )
        # the replacement semantics that make read-modify-write necessary
        preserved = {k: self.offer[k] for k in
                     ("offerId", "sku", "marketplaceId", "format", "status", "listing")
                     if k in self.offer}
        self.offer = {**payload, **preserved}
        return self.update_status, self.update_body


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE item (sku TEXT PRIMARY KEY, state TEXT)")
    conn.execute("INSERT INTO item VALUES ('MP-000003','listed')")
    sp.apply_schema(conn)
    return conn


def prop(pid="pp_reprice", price=5900, reason=PriceReason.REPRICE_OPERATOR,
         **kw) -> PriceProposal:
    base = dict(
        proposal_id=pid, sku="MP-000003", reason=reason, price_cents=price,
        created_at=NOW, basis=CompBasis.ACTIVE_SIMILAR, price_kind=PriceKind.ASKING,
        fee_basis=FeeBasis.CATEGORY_VERIFIED, floor_ok=True, net_proceeds_cents=4172,
        previous_price_cents=11200,
    )
    base.update(kw)
    return PriceProposal(**base)


def approved(conn, p: PriceProposal) -> PriceProposal:
    sp.record_proposal(conn, p)
    sp.approve_proposal(conn, p)
    return p


def live_at(conn, cents: int) -> None:
    """Put the database in the state that follows a successful initial publish."""
    first = prop("pp_initial", cents, reason=PriceReason.INITIAL)
    sp.record_proposal(conn, first)
    sp.approve_proposal(conn, first)
    sp.record_applied(conn, first, marketplace_ref="110588123456")


# --- payload construction -------------------------------------------------------


def test_update_payload_carries_the_whole_offer_not_just_the_price():
    """The bug this prevents: a patch would strip policies off a live listing."""
    payload = build_update_payload(LIVE_OFFER, new_price_cents=5900, currency="USD")
    assert payload["listingPolicies"] == LIVE_OFFER["listingPolicies"]
    assert payload["listingDescription"] == LIVE_OFFER["listingDescription"]
    assert payload["availableQuantity"] == 1
    assert payload["categoryId"] == "57988"
    assert payload["merchantLocationKey"] == "home-1"
    assert payload["includeCatalogProductDetails"] is True
    assert payload["tax"] == {"applyTax": False}


def test_update_payload_strips_the_fields_ebay_rejects():
    payload = build_update_payload(LIVE_OFFER, new_price_cents=5900, currency="USD")
    for field in ("sku", "marketplaceId", "format", "offerId", "status", "listing"):
        assert field not in payload


def test_update_payload_changes_exactly_the_price():
    payload = build_update_payload(LIVE_OFFER, new_price_cents=5900, currency="USD")
    assert payload["pricingSummary"]["price"] == {"value": "59.00", "currency": "USD"}


def test_building_a_payload_does_not_mutate_the_offer_it_read():
    before = copy.deepcopy(LIVE_OFFER)
    build_update_payload(LIVE_OFFER, new_price_cents=5900, currency="USD")
    assert LIVE_OFFER == before


def test_money_crosses_the_boundary_as_a_decimal_string():
    assert cents_to_ebay(5900) == "59.00"
    assert cents_to_ebay(11205) == "112.05"
    assert ebay_to_cents("112.00") == 11200
    assert ebay_to_cents("0.99") == 99


# --- the happy path -------------------------------------------------------------


def test_a_full_reprice_cycle():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    client = FakeOfferClient()

    result = apply_price(conn, p, client=client, offer_id=OFFER_ID, now=NOW)

    assert result.outcome is ApplyOutcome.APPLIED
    assert result.calls_made == 3  # read, write, confirm
    assert client.calls == [f"get:{OFFER_ID}", f"put:{OFFER_ID}", f"get:{OFFER_ID}"]
    assert client.offer["pricingSummary"]["price"]["value"] == "59.00"
    assert client.offer["listingPolicies"] == LIVE_OFFER["listingPolicies"]
    assert sp.current_price_state(conn, "MP-000003")["live_price_cents"] == 5900
    assert result.previous_price_cents == 11200


def test_the_history_shows_the_whole_arc():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    apply_price(conn, p, client=FakeOfferClient(), offer_id=OFFER_ID, now=NOW)
    kinds = [e["event_type"] for e in sp.price_history(conn, "MP-000003")]
    assert kinds == ["proposed", "approved", "applied",
                     "proposed", "approved", "applied"]


def test_rerunning_an_applied_proposal_makes_zero_calls():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    client = FakeOfferClient()
    apply_price(conn, p, client=client, offer_id=OFFER_ID, now=NOW)
    before = len(client.calls)

    again = apply_price(conn, p, client=client, offer_id=OFFER_ID, now=NOW)
    assert again.outcome is ApplyOutcome.NO_CHANGE
    assert again.calls_made == 0
    assert len(client.calls) == before


def test_confirm_can_be_skipped_at_two_calls():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    client = FakeOfferClient()
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW, confirm=False)
    assert r.outcome is ApplyOutcome.APPLIED and r.calls_made == 2


# --- refusals before any write ----------------------------------------------------


def test_no_live_approval_means_no_calls():
    conn = db()
    live_at(conn, 11200)
    p = prop()
    sp.record_proposal(conn, p)  # never approved
    client = FakeOfferClient()
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW)
    assert r.outcome is ApplyOutcome.REFUSED
    assert client.calls == []


def test_a_voided_approval_stops_the_write():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    app = sp.live_approval(conn, p.proposal_id)
    sp.void_approval(conn, app.approval_id, "changed my mind")
    client = FakeOfferClient()
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW)
    assert r.outcome is ApplyOutcome.REFUSED
    assert client.calls == []


def test_a_reprice_needs_a_listed_item():
    conn = db()
    conn.execute("UPDATE item SET state='pricing' WHERE sku='MP-000003'")
    live_at(conn, 11200)
    p = approved(conn, prop())
    r = apply_price(conn, p, client=FakeOfferClient(), offer_id=OFFER_ID, now=NOW)
    assert r.outcome is ApplyOutcome.REFUSED and "live listing" in r.detail


def test_the_executor_reads_state_and_cannot_be_told_otherwise():
    """No caller-supplied item_state: the gate reads the item record."""
    conn = db()
    conn.execute("UPDATE item SET state='publishing' WHERE sku='MP-000003'")
    live_at(conn, 11200)
    p = approved(conn, prop())
    client = FakeOfferClient()
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID, now=NOW)
    assert r.outcome is ApplyOutcome.REFUSED
    assert client.calls == []


def test_production_refuses_a_provisional_fee_basis():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop(fee_basis=FeeBasis.PROVISIONAL_ESTIMATE))
    client = FakeOfferClient()
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW, production=True)
    assert r.outcome is ApplyOutcome.REFUSED
    assert client.calls == []


def test_the_daily_revision_budget_is_respected():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    client = FakeOfferClient()
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW, daily_revision_budget=1)  # the initial publish used it
    assert r.outcome is ApplyOutcome.REFUSED
    assert "revision budget" in r.detail
    assert client.calls == []


# --- refusals after reading the offer ---------------------------------------------


def test_price_drift_is_refused_rather_than_overwritten():
    """Somebody edited the listing in the eBay UI. Do not erase that silently."""
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    drifted = {**LIVE_OFFER,
               "pricingSummary": {"price": {"value": "95.00", "currency": "USD"}}}
    client = FakeOfferClient(drifted)

    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW)
    assert r.outcome is ApplyOutcome.REFUSED
    assert "moved outside this system" in r.detail
    assert client.calls == [f"get:{OFFER_ID}"]  # read only, never written
    assert client.offer["pricingSummary"]["price"]["value"] == "95.00"


def test_an_unpublished_offer_is_refused():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    client = FakeOfferClient({**LIVE_OFFER, "status": "UNPUBLISHED"})
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW)
    assert r.outcome is ApplyOutcome.REFUSED and "live listing" in r.detail


def test_an_offer_for_another_sku_is_refused():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    client = FakeOfferClient({**LIVE_OFFER, "sku": "MP-000009"})
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW)
    assert r.outcome is ApplyOutcome.REFUSED and "belongs to sku" in r.detail


def test_a_published_offer_without_a_description_is_refused():
    """updateOffer requires it; sending without would strip it from the listing."""
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    offer = {k: v for k, v in LIVE_OFFER.items() if k != "listingDescription"}
    client = FakeOfferClient(offer)
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW)
    assert r.outcome is ApplyOutcome.REFUSED and "listingDescription" in r.detail


def test_a_listing_with_sales_is_not_repriced_automatically():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    sold = copy.deepcopy(LIVE_OFFER)
    sold["listing"]["soldQuantity"] = 1
    client = FakeOfferClient(sold)
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW)
    assert r.outcome is ApplyOutcome.REFUSED and "already has 1 sold" in r.detail


def test_a_listing_already_at_the_target_price_is_recorded_not_rewritten():
    conn = db()
    live_at(conn, 5900)
    p = approved(conn, prop(pid="pp_same"))
    already = {**LIVE_OFFER,
               "pricingSummary": {"price": {"value": "59.00", "currency": "USD"}}}
    client = FakeOfferClient(already)
    r = apply_price(conn, p, client=client, offer_id=OFFER_ID,
                    now=NOW)
    assert r.outcome is ApplyOutcome.NO_CHANGE
    assert client.calls == [f"get:{OFFER_ID}"]
    assert sp.already_applied(conn, p.proposal_id)


# --- failures -----------------------------------------------------------------------


def test_a_400_is_permanent_and_not_worth_retrying():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    r = apply_price(conn, p, client=FakeOfferClient(update_status=400),
                    offer_id=OFFER_ID, now=NOW)
    assert r.outcome is ApplyOutcome.FAILED_PERMANENT
    assert not r.outcome.is_retryable
    assert "25002" in r.detail


def test_a_503_is_transient():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    r = apply_price(conn, p, client=FakeOfferClient(update_status=503),
                    offer_id=OFFER_ID, now=NOW)
    assert r.outcome is ApplyOutcome.FAILED_TRANSIENT
    assert r.outcome.is_retryable


def test_a_401_says_the_token_needs_refreshing():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    r = apply_price(conn, p, client=FakeOfferClient(update_status=401),
                    offer_id=OFFER_ID, now=NOW)
    assert "token needs refreshing" in r.detail


def test_a_failed_apply_keeps_the_approval_and_records_the_attempt():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    apply_price(conn, p, client=FakeOfferClient(update_status=500),
                offer_id=OFFER_ID, now=NOW)
    assert sp.live_approval(conn, p.proposal_id) is not None
    kinds = [e["event_type"] for e in sp.price_history(conn, "MP-000003")]
    assert "apply_failed" in kinds
    assert not sp.already_applied(conn, p.proposal_id)


def test_a_failed_apply_can_be_retried_and_then_succeeds():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    flaky = FakeOfferClient(update_status=503)
    assert apply_price(conn, p, client=flaky, offer_id=OFFER_ID, now=NOW).outcome is ApplyOutcome.FAILED_TRANSIENT
    flaky.update_status = 200
    r = apply_price(conn, p, client=flaky, offer_id=OFFER_ID,
                    now=NOW)
    assert r.outcome is ApplyOutcome.APPLIED
    assert sp.current_price_state(conn, "MP-000003")["live_price_cents"] == 5900


# --- a 200 is not evidence ------------------------------------------------------------


class LyingClient(FakeOfferClient):
    """Accepts the write and changes nothing, which is a real eBay failure mode."""

    def update_offer(self, offer_id, payload):
        self.calls.append(f"put:{offer_id}")
        return 200, None


def test_an_accepted_write_that_did_not_land_is_not_recorded_as_applied():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())
    r = apply_price(conn, p, client=LyingClient(), offer_id=OFFER_ID, now=NOW)
    assert r.outcome is ApplyOutcome.UNCONFIRMED
    assert "expected 59.00" in r.detail
    assert not sp.already_applied(conn, p.proposal_id)
    assert sp.current_price_state(conn, "MP-000003")["live_price_cents"] == 11200


def test_a_failed_confirming_read_leaves_the_outcome_honest():
    conn = db()
    live_at(conn, 11200)
    p = approved(conn, prop())

    class ConfirmFails(FakeOfferClient):
        def get_offer(self, offer_id):
            self.calls.append(f"get:{offer_id}")
            if len(self.calls) > 2:
                return 503, None
            return 200, copy.deepcopy(self.offer)

    r = apply_price(conn, p, client=ConfirmFails(), offer_id=OFFER_ID, now=NOW)
    assert r.outcome is ApplyOutcome.UNCONFIRMED
    assert "re-run to verify" in r.detail


# --- pure helpers ---------------------------------------------------------------------


def test_classify_separates_our_bugs_from_ebay_weather():
    assert classify(200).value == "ok"
    assert classify(400).value == "permanent"
    assert classify(404).value == "permanent"
    assert classify(429).value == "transient"
    assert classify(503).value == "transient"
    assert classify(401).value == "auth"


def test_verify_echo_catches_a_mismatch():
    ok, _ = verify_echo(LIVE_OFFER, expected_price_cents=11200)
    assert ok
    bad, why = verify_echo(LIVE_OFFER, expected_price_cents=5900)
    assert not bad and "112.00" in why


def test_validate_offer_accepts_the_first_apply_with_no_recorded_price():
    ok, problem, _ = validate_offer(
        LIVE_OFFER, sku="MP-000003", expected_price_cents=None, currency="USD"
    )
    assert ok and problem is None


def test_currency_mismatch_is_caught():
    offer = {**LIVE_OFFER,
             "pricingSummary": {"price": {"value": "112.00", "currency": "GBP"}}}
    ok, problem, _ = validate_offer(
        offer, sku="MP-000003", expected_price_cents=11200, currency="USD"
    )
    assert not ok and problem.value == "currency_mismatch"


def test_repricing_back_to_an_earlier_price_actually_writes():
    """The bug the already-at-target test exposed.

    $112 -> $59 -> $112. The third proposal carries the same evidence and fee
    schedule as the first, so its content hash is identical. Keyed on that hash,
    the executor concluded the price was already live and sent nothing, leaving
    the listing at $59 while the database claimed $112.
    """
    conn = db()
    live_at(conn, 11200)
    client = FakeOfferClient()

    down = approved(conn, prop("pp_down", 5900))
    assert apply_price(conn, down, client=client, offer_id=OFFER_ID, now=NOW).outcome is ApplyOutcome.APPLIED
    assert client.offer["pricingSummary"]["price"]["value"] == "59.00"

    up = approved(conn, prop("pp_up", 11200, previous_price_cents=5900))
    result = apply_price(conn, up, client=client, offer_id=OFFER_ID, now=NOW)

    assert result.outcome is ApplyOutcome.APPLIED
    assert client.offer["pricingSummary"]["price"]["value"] == "112.00"
    assert sp.current_price_state(conn, "MP-000003")["live_price_cents"] == 11200
