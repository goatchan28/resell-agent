"""Adapter from the executor's `OfferClient` seam to `resell.ebay.client.EbayClient`.

Transport only. Nothing here decides anything about price; it converts between
the shape `EbayClient` speaks and the shape `execute_price` expects, and the
conversion is entirely about failures.

`EbayClient.request` returns a three-tuple and **raises `EbayApiError` on non-2xx**.
The executor classifies on a status code, so without this translation an eBay 400
would propagate straight out of `apply_price`, past the `apply_failed` recording,
leaving a traceback and no record that the attempt happened. Catching the error
and handing back `(status_code, {"errors": [...]})` is the whole job.

Two deliberate choices, both matching conventions already in the codebase:

`Content-Language` is passed per write call, mirroring `spike.WRITE_HEADERS`,
because eBay requires it on Inventory API writes and its absence produces a 400
that reads like a payload problem. httpx sets `Content-Type` from `json=`.

`retry_safe=False` on the write. A PUT of the whole offer is idempotent in
content, so the client's automatic retry would be harmless for correctness -- but
each accepted revision counts against eBay's 250-per-day ceiling, and the
executor's budget counts recorded applications rather than HTTP attempts. Silent
retries would put those two counts out of step. The client's own comment says the
caller or the state machine should decide whether to come back later, and here
that decision belongs to the executor, which reports FAILED_TRANSIENT.
"""

from __future__ import annotations

from typing import Any

import httpx

from .ebay.client import EbayApiError

OFFER_PATH = "/sell/inventory/v1/offer/{offer_id}"

# Matches spike.WRITE_HEADERS. Content-Type comes from httpx via `json=`.
WRITE_HEADERS = {"Content-Language": "en-US"}

# Not an eBay status. A connection reset or timeout has no status code at all,
# and inventing a 503 would make a local network failure indistinguishable from
# an eBay outage in the event log. 599 classifies as transient and is obviously
# synthetic to anyone reading the trace.
TRANSPORT_FAILURE_STATUS = 599


class EbayOfferClient:
    """Implements the `OfferClient` protocol over the shared authenticated client."""

    def __init__(self, client: Any, *, content_language: str = "en-US"):
        self._client = client
        self._write_headers = {"Content-Language": content_language}

    def get_offer(self, offer_id: str) -> tuple[int, dict | None]:
        # Reads are free and do not count as revisions, so the client's retry is
        # welcome here.
        return self._call(
            "GET", OFFER_PATH.format(offer_id=offer_id), retry_safe=True
        )

    def update_offer(self, offer_id: str, payload: dict) -> tuple[int, dict | None]:
        return self._call(
            "PUT",
            OFFER_PATH.format(offer_id=offer_id),
            json=payload,
            headers=self._write_headers,
            retry_safe=False,
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict | None]:
        try:
            status, body, _headers = self._client.request(
                method, path, auth="user", host="api", **kwargs
            )
        except EbayApiError as exc:
            # The structured error array is what makes an eBay failure actionable;
            # describe_errors() in pricing/offer.py reads exactly this shape.
            return exc.status_code, {"errors": exc.errors or []}
        except httpx.RequestError as exc:
            return TRANSPORT_FAILURE_STATUS, {
                "errors": [{
                    "errorId": 0,
                    "message": f"{type(exc).__name__} reaching eBay: {exc}",
                }]
            }
        return status, body if isinstance(body, dict) else None
