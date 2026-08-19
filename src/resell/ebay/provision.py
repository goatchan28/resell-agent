"""One-time seller account provisioning: three business policies + one location.

Deliberately minimal. The goal is exactly one working configuration capable of
publishing a single US fixed-price offer -- not a policy management system.

Every step is check-then-create, keyed by a stable name, so running this twice is
harmless and running it after a partial failure resumes rather than duplicates.
That is the same idempotency property the item state machine will rely on.

Shipping service is a single constant rather than a Metadata API lookup. The
authoritative code list lives in the Trading API's GeteBayDetails, so a dynamic
lookup would reintroduce an XML dependency to solve a problem we do not have: we
need one valid service, not a menu. Revisit when adding a second marketplace or
weight-based service selection.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from resell.db import kv_set, log_event
from resell.ebay.client import EbayApiError, EbayClient

# Stable names. Lookups match on these, so changing one orphans the old policy.
FULFILLMENT_POLICY_NAME = "resell-standard-domestic"
PAYMENT_POLICY_NAME = "resell-immediate-pay"
RETURN_POLICY_NAME = "resell-30day-returns"
LOCATION_KEY = "resell-primary"

# Motors vehicles have their own policy rules and are out of scope.
CATEGORY_TYPE = "ALL_EXCLUDING_MOTORS_VEHICLES"

# One line to change. USPSGroundAdvantage is USPS's current domestic ground
# service; USPSPriority is the long-standing fallback if a marketplace or sandbox
# rejects it.
SHIPPING_CARRIER = os.environ.get("EBAY_SHIPPING_CARRIER", "USPS")
SHIPPING_SERVICE = os.environ.get("EBAY_SHIPPING_SERVICE", "USPSGroundAdvantage")
SHIPPING_FALLBACKS = ("USPSPriority", "USPSFirstClass", "ShippingMethodStandard")


@dataclass
class Step:
    name: str
    status: str  # "exists" | "created" | "failed"
    identifier: str | None = None
    detail: str = ""


def _address() -> dict[str, str]:
    """Ship-from address. Sandbox-safe defaults; production needs real values."""
    return {
        "addressLine1": os.environ.get("SHIP_FROM_ADDRESS1", "1 Test Street"),
        "city": os.environ.get("SHIP_FROM_CITY", "San Jose"),
        "stateOrProvince": os.environ.get("SHIP_FROM_STATE", "CA"),
        "postalCode": os.environ.get("SHIP_FROM_POSTAL", "95125"),
        "country": os.environ.get("SHIP_FROM_COUNTRY", "US"),
    }


# Response container and id field per policy resource, spelled out rather than
# derived. Deriving them algorithmically produced "paymentPolicys" and silently
# broke every existence check, so each run recreated policies it already had.
# API field names are not a naming convention to compute; they are data.
POLICY_FIELDS: dict[str, tuple[str, str]] = {
    "fulfillment_policy": ("fulfillmentPolicies", "fulfillmentPolicyId"),
    "payment_policy": ("paymentPolicies", "paymentPolicyId"),
    "return_policy": ("returnPolicies", "returnPolicyId"),
}


def _find_policy(client: EbayClient, resource: str, name: str) -> str | None:
    """Return the policy id matching `name`, or None.

    Reads the list rather than trusting a stored id: the source of truth is eBay,
    and a locally cached id can outlive the policy it points at.
    """
    container, id_field = POLICY_FIELDS[resource]
    body = client.get(
        f"/sell/account/v1/{resource}",
        params={"marketplace_id": client.config.marketplace_id},
    )
    for policy in (body or {}).get(container) or []:
        if policy.get("name") == name:
            return policy.get(id_field)
    return None


def _create_policy(
    client: EbayClient, resource: str, payload: dict, id_field: str
) -> str:
    _, body, _ = client.request(
        "POST",
        f"/sell/account/v1/{resource}",
        json=payload,
        retry_safe=False,  # creates a named resource; a duplicate is real clutter
    )
    return (body or {}).get(id_field, "")


def ensure_fulfillment_policy(client: EbayClient) -> Step:
    """Flat-rate free domestic shipping, 1 day handling.

    shippingOptions must contain at least one concrete service. An empty array is
    accepted at creation time and then fails at publish with error 25007, which is
    the single most misleading failure in this whole flow -- the policy looks fine
    until you try to use it.
    """
    existing = _find_policy(client, "fulfillment_policy", FULFILLMENT_POLICY_NAME)
    if existing:
        return Step("fulfillment policy", "exists", existing)

    def payload(service: str) -> dict:
        return {
            "name": FULFILLMENT_POLICY_NAME,
            "marketplaceId": client.config.marketplace_id,
            "categoryTypes": [{"name": CATEGORY_TYPE}],
            "handlingTime": {"unit": "DAY", "value": 1},
            "shippingOptions": [
                {
                    "optionType": "DOMESTIC",
                    "costType": "FLAT_RATE",
                    "shippingServices": [
                        {
                            "sortOrder": 1,
                            "shippingCarrierCode": SHIPPING_CARRIER,
                            "shippingServiceCode": service,
                            "freeShipping": True,
                            "buyerResponsibleForShipping": False,
                        }
                    ],
                }
            ],
        }

    candidates = (SHIPPING_SERVICE, *SHIPPING_FALLBACKS)
    last_error: EbayApiError | None = None
    for service in candidates:
        try:
            policy_id = _create_policy(
                client,
                "fulfillment_policy",
                payload(service),
                POLICY_FIELDS["fulfillment_policy"][1],
            )
            detail = f"service={service}"
            if service != SHIPPING_SERVICE:
                detail += f" (fell back from {SHIPPING_SERVICE})"
            return Step("fulfillment policy", "created", policy_id, detail)
        except EbayApiError as exc:
            last_error = exc
            # Only a rejected service code is worth retrying with another code.
            # Anything else (auth, opt-in, malformed payload) recurs identically.
            if not _looks_like_bad_service(exc):
                break
            log_event(
                client.conn,
                "provision.shipping_service_rejected",
                {"service": service, "errors": exc.errors},
            )
    return Step("fulfillment policy", "failed", None, str(last_error))


def _looks_like_bad_service(exc: EbayApiError) -> bool:
    text = str(exc).lower()
    return "shippingservicecode" in text or "shipping service" in text


def ensure_payment_policy(client: EbayClient) -> Step:
    """Immediate payment. Under managed payments no paymentMethods are needed."""
    existing = _find_policy(client, "payment_policy", PAYMENT_POLICY_NAME)
    if existing:
        return Step("payment policy", "exists", existing)
    try:
        policy_id = _create_policy(
            client,
            "payment_policy",
            {
                "name": PAYMENT_POLICY_NAME,
                "marketplaceId": client.config.marketplace_id,
                "categoryTypes": [{"name": CATEGORY_TYPE}],
                "immediatePay": True,
            },
            POLICY_FIELDS["payment_policy"][1],
        )
        return Step("payment policy", "created", policy_id)
    except EbayApiError as exc:
        return Step("payment policy", "failed", None, str(exc))


def ensure_return_policy(client: EbayClient) -> Step:
    """30 day buyer-paid returns -- a conservative default for reselling."""
    existing = _find_policy(client, "return_policy", RETURN_POLICY_NAME)
    if existing:
        return Step("return policy", "exists", existing)
    try:
        policy_id = _create_policy(
            client,
            "return_policy",
            {
                "name": RETURN_POLICY_NAME,
                "marketplaceId": client.config.marketplace_id,
                "categoryTypes": [{"name": CATEGORY_TYPE}],
                "returnsAccepted": True,
                "returnPeriod": {"unit": "DAY", "value": 30},
                "returnShippingCostPayer": "BUYER",
                "refundMethod": "MONEY_BACK",
            },
            POLICY_FIELDS["return_policy"][1],
        )
        return Step("return policy", "created", policy_id)
    except EbayApiError as exc:
        return Step("return policy", "failed", None, str(exc))


def ensure_inventory_location(client: EbayClient) -> Step:
    """Create the merchant location publishOffer requires.

    Checks the single-location GET rather than the list endpoint. That is not a
    style preference: the list endpoint (/location) is currently throwing 500 with
    error 25001 in sandbox, while the keyed endpoint (/location/{key}) is a
    different route. Routing around the broken read also answers the open
    question of whether the outage is cosmetic or blocking.
    """
    path = f"/sell/inventory/v1/location/{LOCATION_KEY}"
    try:
        body = client.get(path)
        status = (body or {}).get("merchantLocationStatus", "?")
        return Step("inventory location", "exists", LOCATION_KEY, f"status={status}")
    except EbayApiError as exc:
        if exc.status_code != 404:
            # A 500 here means the keyed route is broken too, not just the list.
            return Step("inventory location", "failed", None, str(exc))

    try:
        client.request(
            "POST",
            path,
            json={
                "location": {"address": _address()},
                "locationTypes": ["WAREHOUSE"],
                "merchantLocationStatus": "ENABLED",
                "name": "Resell primary location",
            },
            retry_safe=False,
            expect_json=False,
        )
        return Step("inventory location", "created", LOCATION_KEY)
    except EbayApiError as exc:
        return Step("inventory location", "failed", None, str(exc))


def provision(client: EbayClient) -> list[Step]:
    """Run every step, then persist the resulting ids for the publish flow."""
    steps = [
        ensure_payment_policy(client),
        ensure_return_policy(client),
        ensure_fulfillment_policy(client),
        ensure_inventory_location(client),
    ]

    keys = {
        "payment policy": "ebay.payment_policy_id",
        "return policy": "ebay.return_policy_id",
        "fulfillment policy": "ebay.fulfillment_policy_id",
        "inventory location": "ebay.merchant_location_key",
    }
    for step in steps:
        if step.status in {"exists", "created"} and step.identifier:
            kv_set(client.conn, f"{keys[step.name]}:{client.config.env.name}", step.identifier)

    log_event(
        client.conn,
        "provision.completed",
        {
            "environment": client.config.env.name,
            "steps": [
                {"name": s.name, "status": s.status, "id": s.identifier} for s in steps
            ],
        },
    )
    return steps
