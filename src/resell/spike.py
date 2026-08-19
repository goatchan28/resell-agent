"""THROWAWAY EXECUTION SPIKE. Not architecture. Delete once the state machine lands.

Purpose: prove createOrReplaceInventoryItem -> createOffer -> publishOffer against
the real Sandbox, and settle whether error 25018 ("Incomplete account information")
blocks publishing for a test seller whose getPrivileges reports
sellerRegistrationCompleted: false.

Everything about the item is hardcoded. The one place this is not naive is
required aspects: they are fetched from the Taxonomy API and auto-filled, because
missing aspects are the most common publish failure and a failure there would
teach us nothing about registration. The goal is to make registration the only
remaining variable.

Auto-filling aspects with the first allowed value produces a nonsense listing.
That is fine here and wrong for production -- in the real pipeline the model fills
aspects from evidence about the actual item.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from resell.db import kv_get, log_event
from resell.ebay.client import EbayApiError, EbayClient

SKU = "SPIKE-001"
TITLE = "Test Listing Do Not Buy - API Integration Spike"
DESCRIPTION = (
    "This is an automated Sandbox test listing created to verify API integration. "
    "It is not a real item and is not for sale."
)
PRICE = "19.99"
CONDITION = "USED_EXCELLENT"
QUANTITY = 1

# eBay requires Content-Language on the Inventory API write calls. Omitting it
# produces an error that does not mention the header, so it is easy to lose an
# afternoon to.
WRITE_HEADERS = {"Content-Language": "en-US"}


@dataclass
class SpikeStep:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Prerequisites:
    fulfillment_policy_id: str
    payment_policy_id: str
    return_policy_id: str
    merchant_location_key: str
    image_urls: list[str]


class SpikeAborted(RuntimeError):
    pass


def load_prerequisites(conn: sqlite3.Connection, environment: str) -> Prerequisites:
    """Pull everything the earlier steps produced. Fail loudly if anything is missing."""

    def required_kv(key: str, remedy: str) -> str:
        value = kv_get(conn, f"{key}:{environment}")
        if not value:
            raise SpikeAborted(f"missing {key} for {environment}. Run: {remedy}")
        return value

    rows = conn.execute(
        "SELECT eps_url FROM images WHERE environment = ? AND eps_url IS NOT NULL "
        "ORDER BY uploaded_at DESC LIMIT 3",
        (environment,),
    ).fetchall()
    if not rows:
        raise SpikeAborted(
            "no uploaded images found. Run: resell images upload <photo>"
        )

    return Prerequisites(
        fulfillment_policy_id=required_kv("ebay.fulfillment_policy_id", "resell account provision"),
        payment_policy_id=required_kv("ebay.payment_policy_id", "resell account provision"),
        return_policy_id=required_kv("ebay.return_policy_id", "resell account provision"),
        merchant_location_key=required_kv("ebay.merchant_location_key", "resell account provision"),
        image_urls=[row["eps_url"] for row in rows],
    )


# --- category and aspects ----------------------------------------------------


def resolve_category(client: EbayClient, tree_id: str, query: str) -> tuple[str, str]:
    """Ask Taxonomy for a leaf category matching a query string.

    Application token, not user token -- Taxonomy is metadata, not seller data.
    """
    body = client.get(
        f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions",
        auth="app",
        params={"q": query},
    )
    suggestions = (body or {}).get("categorySuggestions") or []
    if not suggestions:
        raise SpikeAborted(f"Taxonomy returned no category suggestions for {query!r}")
    category = suggestions[0]["category"]
    return category["categoryId"], category.get("categoryName", "?")


def required_aspects(client: EbayClient, tree_id: str, category_id: str) -> dict[str, list[str]]:
    """Fetch required aspects and fill each with a plausible value.

    Injecting the real aspect schema is the right pattern for the production agent
    too -- the model should be handed a form to fill, not asked to invent aspect
    names. What is spike-grade here is the *values*.
    """
    body = client.get(
        f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_item_aspects_for_category",
        auth="app",
        params={"category_id": category_id},
    )
    filled: dict[str, list[str]] = {}
    for aspect in (body or {}).get("aspects") or []:
        constraint = aspect.get("aspectConstraint") or {}
        if not constraint.get("aspectRequired"):
            continue
        name = aspect.get("localizedAspectName")
        if not name:
            continue
        allowed = [
            value.get("localizedValue")
            for value in (aspect.get("aspectValues") or [])
            if value.get("localizedValue")
        ]
        # Prefer an allowed value; fall back to eBay's conventional placeholder for
        # free-text aspects.
        filled[name] = [allowed[0]] if allowed else ["Does not apply"]
    return filled


# --- the three calls ---------------------------------------------------------


def put_inventory_item(client: EbayClient, prerequisites: Prerequisites, aspects: dict) -> SpikeStep:
    """createOrReplaceInventoryItem. A PUT keyed by SKU, so safely repeatable."""
    payload = {
        "availability": {"shipToLocationAvailability": {"quantity": QUANTITY}},
        "condition": CONDITION,
        "product": {
            "title": TITLE,
            "description": DESCRIPTION,
            "imageUrls": prerequisites.image_urls,
            "aspects": aspects,
        },
    }
    try:
        status, _, _ = client.request(
            "PUT",
            f"/sell/inventory/v1/inventory_item/{SKU}",
            json=payload,
            headers=WRITE_HEADERS,
            expect_json=False,
        )
    except EbayApiError as exc:
        return SpikeStep("createOrReplaceInventoryItem", False, str(exc))
    return SpikeStep(
        "createOrReplaceInventoryItem",
        True,
        f"HTTP {status}, sku={SKU}, {len(prerequisites.image_urls)} image(s)",
    )


def find_or_create_offer(client: EbayClient, prerequisites: Prerequisites, category_id: str) -> SpikeStep:
    """createOffer, but check for an existing offer first.

    createOffer is a POST and is not idempotent: called twice for the same SKU it
    errors rather than returning the original. Reading first makes the spike
    re-runnable, which is the same read-before-write pattern the effect gateway
    will need for every non-idempotent command.
    """
    try:
        existing = client.get(
            "/sell/inventory/v1/offer",
            params={"sku": SKU, "marketplace_id": client.config.marketplace_id},
        )
        offers = (existing or {}).get("offers") or []
        if offers:
            offer = offers[0]
            return SpikeStep(
                "createOffer",
                True,
                f"reused existing offer, status={offer.get('status')}",
                {"offerId": offer["offerId"], "listingId": offer.get("listingId")},
            )
    except EbayApiError as exc:
        # 404 here just means no offers exist yet.
        if exc.status_code != 404:
            return SpikeStep("getOffers", False, str(exc))

    payload = {
        "sku": SKU,
        "marketplaceId": client.config.marketplace_id,
        "format": "FIXED_PRICE",
        "availableQuantity": QUANTITY,
        "categoryId": category_id,
        "listingDescription": DESCRIPTION,
        "listingPolicies": {
            "fulfillmentPolicyId": prerequisites.fulfillment_policy_id,
            "paymentPolicyId": prerequisites.payment_policy_id,
            "returnPolicyId": prerequisites.return_policy_id,
        },
        "pricingSummary": {"price": {"value": PRICE, "currency": "USD"}},
        "merchantLocationKey": prerequisites.merchant_location_key,
    }
    try:
        _, body, _ = client.request(
            "POST",
            "/sell/inventory/v1/offer",
            json=payload,
            headers=WRITE_HEADERS,
            retry_safe=False,
        )
    except EbayApiError as exc:
        return SpikeStep("createOffer", False, str(exc))

    offer_id = (body or {}).get("offerId")
    if not offer_id:
        return SpikeStep("createOffer", False, f"no offerId in response: {body!r}")
    return SpikeStep("createOffer", True, f"offerId={offer_id}", {"offerId": offer_id})


def publish_offer(client: EbayClient, offer_id: str) -> SpikeStep:
    """publishOffer. The moment of truth for error 25018."""
    try:
        _, body, _ = client.request(
            "POST",
            f"/sell/inventory/v1/offer/{offer_id}/publish",
            headers=WRITE_HEADERS,
            retry_safe=False,
        )
    except EbayApiError as exc:
        return SpikeStep("publishOffer", False, str(exc), {"errorIds": sorted(exc.error_ids)})

    listing_id = (body or {}).get("listingId")
    if not listing_id:
        # A 2xx with no listingId is not a proven publish. Reporting it as success
        # would let the spike claim victory without evidence, which is the one
        # thing this spike must not do.
        return SpikeStep(
            "publishOffer",
            False,
            f"HTTP success but no listingId in response: {body!r}",
            {"errorIds": []},
        )
    return SpikeStep("publishOffer", True, f"listingId={listing_id}", {"listingId": listing_id})


def diagnose_publish_failure(error_ids: list[int], detail: str) -> str:
    """Name the cause. 25018 is the one this spike exists to settle."""
    if 25018 in error_ids:
        return (
            "ERROR 25018 CONFIRMED: seller registration genuinely blocks publishing.\n"
            "  sellerRegistrationCompleted: false is authoritative, not cosmetic.\n"
            "  Remedy: ValidateTestUserRegistration (Trading API, XML) -- which\n"
            "  reintroduces the XML dependency as a one-time provisioning call."
        )
    if 25007 in error_ids:
        return (
            "Error 25007: the fulfillment policy has no valid shipping service.\n"
            "  The policy was created with USPSPriority after USPSGroundAdvantage was\n"
            "  rejected; sandbox may not accept that service either."
        )
    if 25002 in error_ids:
        return "Error 25002: a user error in the offer or inventory item data. See detail above."
    if 25001 in error_ids:
        return "Error 25001: eBay-side system error, the recurring sandbox condition. Retry later."
    if any(code in detail.lower() for code in ("aspect", "item specific")):
        return "Missing or invalid required aspects despite Taxonomy auto-fill."
    return "Unrecognised failure. The errorId is the actionable part."


def run(client: EbayClient, conn: sqlite3.Connection) -> list[SpikeStep]:
    steps: list[SpikeStep] = []
    prerequisites = load_prerequisites(conn, client.config.env.name)
    steps.append(
        SpikeStep(
            "prerequisites",
            True,
            f"{len(prerequisites.image_urls)} image(s), 3 policies, location="
            f"{prerequisites.merchant_location_key}",
        )
    )

    tree = client.get_default_category_tree_id()
    tree_id = (tree or {}).get("categoryTreeId", "0")
    category_id, category_name = resolve_category(client, tree_id, "used paperback book")
    steps.append(SpikeStep("category", True, f"{category_id} ({category_name})"))

    aspects = required_aspects(client, tree_id, category_id)
    steps.append(
        SpikeStep(
            "required aspects",
            True,
            f"{len(aspects)} required: {', '.join(sorted(aspects)) or 'none'}",
        )
    )

    steps.append(put_inventory_item(client, prerequisites, aspects))
    if not steps[-1].ok:
        return steps

    offer_step = find_or_create_offer(client, prerequisites, category_id)
    steps.append(offer_step)
    if not offer_step.ok:
        return steps

    if offer_step.data.get("listingId"):
        steps.append(
            SpikeStep(
                "publishOffer",
                True,
                f"already published, listingId={offer_step.data['listingId']}",
                {"listingId": offer_step.data["listingId"]},
            )
        )
        return steps

    steps.append(publish_offer(client, offer_step.data["offerId"]))
    log_event(
        conn,
        "spike.completed",
        {
            "environment": client.config.env.name,
            "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail[:200]} for s in steps],
        },
    )
    return steps
