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

# What eBay's documentation says may be absent from an updateOffer body: `sku`,
# `marketplaceId` and `format` are named as the exceptions to "resend
# everything", and the rest are response-only containers getOffer adds.
#
# Held separately from READ_ONLY_OFFER_FIELDS below, which is what the payload
# builder actually strips, and the two are asserted equal by a test. The
# duplication is deliberate: OfferDiff.is_safe judges removals against *this*
# set, so checking it against the stripping set would be a tautology -- a wrong
# strip list would silently vindicate itself. Adding a field to one and not the
# other fails a test rather than quietly widening what a write may destroy.
DOCUMENTED_NON_UPDATE_FIELDS = frozenset({
    "offerId",
    "sku",
    "marketplaceId",
    "format",
    "status",
    "statusDetails",
    "listing",
})

# What build_update_payload strips.
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


def validate_offer_structure(
    offer: dict, *, sku: str, currency: str
) -> tuple[bool, OfferProblem | None, str]:
    """Is this the right offer, in a state that can be written to at all.

    Split from the drift check deliberately. These are facts about the offer that
    make a write impossible or wrong; drift is a fact about the *relationship*
    between the listing and our records, and the two have to be evaluated in that
    order -- see `detect_price_drift` for why.
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

    return True, None, f"offer is live at {cents_to_ebay(current)}"


def detect_price_drift(
    offer: dict, *, expected_price_cents: int | None
) -> tuple[bool, OfferProblem | None, str]:
    """Has the listing price moved outside this system.

    `expected_price_cents` is what our own records say is live. A mismatch means
    somebody edited the listing in the eBay UI, or an earlier apply half-succeeded.
    Overwriting would erase a change nobody here recorded, so it is refused.

    **This must be evaluated after checking whether the listing already carries
    the price this proposal wants.** If a PUT succeeds and the response is lost,
    the retry sees a listing at the new price and records showing the old one --
    which is drift by this definition, and refusing it strands the item with no
    route forward but manual repair. That case is not drift, it is a write that
    landed unrecorded, and the caller resolves it before asking this question.
    """
    if expected_price_cents is None:
        return True, None, "no recorded price to compare against"
    current = offer_price_cents(offer)
    if current is None:
        return False, OfferProblem.NO_PRICE, "offer has no readable price"
    if current != expected_price_cents:
        return False, OfferProblem.PRICE_DRIFT, (
            f"listing is at {cents_to_ebay(current)} but our records say "
            f"{cents_to_ebay(expected_price_cents)}; the price moved outside this "
            "system, so nothing here is safe to overwrite"
        )
    return True, None, f"listing matches our records at {cents_to_ebay(current)}"


def validate_offer(
    offer: dict,
    *,
    sku: str,
    expected_price_cents: int | None,
    currency: str,
) -> tuple[bool, OfferProblem | None, str]:
    """Structure then drift, in that order. Retained for callers wanting both."""
    ok, problem, reason = validate_offer_structure(offer, sku=sku, currency=currency)
    if not ok:
        return ok, problem, reason
    return detect_price_drift(offer, expected_price_cents=expected_price_cents)


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


# --- previewing a write ---------------------------------------------------------


def _flatten(value: object, prefix: str = "") -> dict[str, object]:
    """Dotted paths to scalars, so a diff can point at pricingSummary.price.value."""
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, child in value.items():
            out.update(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(value, list):
        out = {}
        for index, child in enumerate(value):
            out.update(_flatten(child, f"{prefix}[{index}]"))
        return out
    return {prefix: value}


@dataclass(frozen=True)
class OfferDiff:
    """Exactly what a PUT would change, computed before sending it.

    The reassuring output is a boring one: a single changed path under
    pricingSummary, and a removed list containing nothing but fields eBay rejects
    on update. Anything else in `removed` is a field the listing is about to lose.
    """

    changed: tuple[tuple[str, object, object], ...] = ()
    removed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()

    @property
    def only_the_price_changed(self) -> bool:
        return all(path.startswith("pricingSummary.price") for path, _, _ in self.changed)

    @property
    def removes_only_read_only_fields(self) -> bool:
        """Judged against the documented set, never against what we stripped."""
        return all(
            path.split(".")[0].split("[")[0] in DOCUMENTED_NON_UPDATE_FIELDS
            for path in self.removed
        )

    @property
    def is_safe(self) -> bool:
        return (
            self.only_the_price_changed
            and self.removes_only_read_only_fields
            and not self.added
        )


def diff_payload(offer: dict, payload: dict) -> OfferDiff:
    before, after = _flatten(offer), _flatten(payload)
    return OfferDiff(
        changed=tuple(
            (path, before[path], after[path])
            for path in sorted(before.keys() & after.keys())
            if before[path] != after[path]
        ),
        removed=tuple(sorted(before.keys() - after.keys())),
        added=tuple(sorted(after.keys() - before.keys())),
    )
