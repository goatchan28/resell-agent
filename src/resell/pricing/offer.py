"""Offer payload construction and response classification for repricing.

Pure: no HTTP, no database. Everything here is a function of a dict and an int,
which is what makes the risky part of repricing testable without a network.

The load-bearing fact about `updateOffer` is that it is **not a patch**. Except
for `sku`, `marketplaceId` and `format`, every field already set on the offer has
to be sent again even when unchanged, and a published offer additionally requires
`listingDescription`. Sending `{"pricingSummary": {...}}` on its own does not
change the price of a listing -- it strips the listing policies, the quantity, the
category and the description off it. So a price change is a read-modify-write:
fetch the current offer, alter exactly one leaf, send the whole thing back.

Two further constraints shape the executor around this module. eBay does not
return the offer in the `updateOffer` response, so the only evidence that a price
is actually live is a confirming read. And a listing may be revised 250 times in
a calendar day, which is generous for a human but not for anything automated, so
the budget is tracked rather than discovered by being blocked.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum

# Returned by getOffer, rejected or ignored by updateOffer. `sku`,
# `marketplaceId` and `format` are named in the documentation as the exceptions
# to "resend everything"; the rest are read-only response containers.
READ_ONLY_OFFER_FIELDS = frozenset({
    "offerId",
    "sku",
    "marketplaceId",
    "format",
    "status",
    "statusDetails",
    "listing",
})

# eBay's own ceiling is 250 revisions per listing per calendar day, on their
# clock rather than ours. The default leaves headroom rather than discovering
# the limit by being blocked mid-reprice.
DEFAULT_DAILY_REVISION_BUDGET = 200
EBAY_DAILY_REVISION_LIMIT = 250


def cents_to_ebay(cents: int) -> str:
    """Money crosses this boundary as a decimal string, never as a float."""
    return str(
        (Decimal(cents) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def ebay_to_cents(value: str | int | float) -> int:
    return int(
        (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def offer_price_cents(offer: dict) -> int | None:
    try:
        return ebay_to_cents(offer["pricingSummary"]["price"]["value"])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def offer_currency(offer: dict) -> str | None:
    try:
        return offer["pricingSummary"]["price"]["currency"]
    except (KeyError, TypeError):
        return None


def is_published(offer: dict) -> bool:
    return str(offer.get("status", "")).upper() == "PUBLISHED"


def listing_id(offer: dict) -> str | None:
    return (offer.get("listing") or {}).get("listingId")


def sold_quantity(offer: dict) -> int:
    return int((offer.get("listing") or {}).get("soldQuantity") or 0)


class OfferProblem(StrEnum):
    WRONG_SKU = "wrong_sku"
    NOT_PUBLISHED = "not_published"
    NO_PRICE = "no_price"
    NO_DESCRIPTION = "no_description"
    CURRENCY_MISMATCH = "currency_mismatch"
    PRICE_DRIFT = "price_drift"


def validate_offer(
    offer: dict,
    *,
    sku: str,
    expected_price_cents: int | None,
    currency: str,
) -> tuple[bool, OfferProblem | None, str]:
    """Everything checked before a write, so a bad write is never attempted.

    `expected_price_cents` is what our own records say is live. A mismatch means
    the price moved outside this system -- somebody edited the listing in the eBay
    UI, or an earlier apply half-succeeded. Overwriting it would erase a change
    nobody here recorded, so it is refused rather than resolved.
    """
    if offer.get("sku") != sku:
        return False, OfferProblem.WRONG_SKU, (
            f"offer belongs to sku {offer.get('sku')!r}, not {sku!r}"
        )
    if not is_published(offer):
        return False, OfferProblem.NOT_PUBLISHED, (
            f"offer status is {offer.get('status')!r}; a reprice needs a live listing"
        )
    if not offer.get("listingDescription"):
        return False, OfferProblem.NO_DESCRIPTION, (
            "published offer has no listingDescription, which updateOffer requires; "
            "sending it without one would strip the description from the listing"
        )
    current = offer_price_cents(offer)
    if current is None:
        return False, OfferProblem.NO_PRICE, "offer has no readable pricingSummary price"

    actual_currency = offer_currency(offer)
    if actual_currency and actual_currency != currency:
        return False, OfferProblem.CURRENCY_MISMATCH, (
            f"offer is priced in {actual_currency}, not {currency}"
        )

    if expected_price_cents is not None and current != expected_price_cents:
        return False, OfferProblem.PRICE_DRIFT, (
            f"listing is at {cents_to_ebay(current)} but our records say "
            f"{cents_to_ebay(expected_price_cents)}; the price moved outside this "
            "system, so nothing here is safe to overwrite"
        )
    return True, None, f"offer is live at {cents_to_ebay(current)}"


def build_update_payload(offer: dict, *, new_price_cents: int, currency: str) -> dict:
    """The whole offer back, with one leaf changed.

    Read-modify-write rather than a patch, because updateOffer replaces. Fields
    eBay rejects on update are stripped; everything else the seller had set --
    policies, quantity, category, description, catalog opt-in -- is carried
    through untouched precisely because it is carried through at all.
    """
    payload = copy.deepcopy(offer)
    for field in READ_ONLY_OFFER_FIELDS:
        payload.pop(field, None)

    summary = payload.setdefault("pricingSummary", {})
    price = summary.setdefault("price", {})
    price["value"] = cents_to_ebay(new_price_cents)
    price["currency"] = currency
    return payload


def verify_echo(offer: dict, *, expected_price_cents: int) -> tuple[bool, str]:
    """Did the price actually land.

    updateOffer does not return the offer, so a 200 means the request was
    accepted, not that the listing changed. This is the same discipline as
    checking a migration's own claim: the call reporting success is not evidence.
    """
    actual = offer_price_cents(offer)
    if actual is None:
        return False, "confirming read returned no readable price"
    if actual != expected_price_cents:
        return False, (
            f"confirming read shows {cents_to_ebay(actual)}, expected "
            f"{cents_to_ebay(expected_price_cents)}"
        )
    return True, f"listing confirmed at {cents_to_ebay(actual)}"


class ResponseClass(StrEnum):
    OK = "ok"
    PERMANENT = "permanent"
    TRANSIENT = "transient"
    AUTH = "auth"


def classify(status_code: int) -> ResponseClass:
    """A 400 is our bug and a 503 is eBay's weather; conflating them costs hours.

    The Taxonomy incident is the precedent -- an invalid category id read as an
    outage and retried, when no amount of retrying was ever going to help.
    """
    if 200 <= status_code < 300:
        return ResponseClass.OK
    if status_code in (401, 403):
        return ResponseClass.AUTH
    if status_code == 429:
        return ResponseClass.TRANSIENT
    if 400 <= status_code < 500:
        return ResponseClass.PERMANENT
    return ResponseClass.TRANSIENT


def describe_errors(body: dict | None) -> str:
    """eBay returns a structured error list; flatten it rather than dumping JSON."""
    if not isinstance(body, dict):
        return ""
    out = []
    for err in (body.get("errors") or []):
        eid = err.get("errorId")
        msg = err.get("message") or err.get("longMessage") or ""
        params = ", ".join(
            f"{p.get('name')}={p.get('value')}" for p in (err.get("parameters") or [])
        )
        out.append(f"[{eid}] {msg}" + (f" ({params})" if params else ""))
    for warn in (body.get("warnings") or []):
        out.append(f"warning [{warn.get('errorId')}] {warn.get('message', '')}")
    return "; ".join(out)


@dataclass(frozen=True)
class RevisionBudget:
    used_today: int
    budget: int = DEFAULT_DAILY_REVISION_BUDGET

    @property
    def exhausted(self) -> bool:
        return self.used_today >= self.budget

    def describe(self) -> str:
        return (
            f"{self.used_today}/{self.budget} revisions used today "
            f"(eBay's own limit is {EBAY_DAILY_REVISION_LIMIT})"
        )
