"""The transport adapter: exceptions in, status codes out."""

from __future__ import annotations

import httpx
import pytest

from resell.ebay.client import EbayApiError
from resell.ebay_offer_client import TRANSPORT_FAILURE_STATUS, EbayOfferClient
from resell.pricing.offer import ResponseClass, classify, describe_errors

OFFER = {"offerId": "offer-1", "sku": "MP-000003", "status": "PUBLISHED"}


class FakeEbayClient:
    """Mirrors EbayClient.request: three-tuple on success, raises on non-2xx."""

    def __init__(self, *, raises=None, body=None, status=200):
        self.raises = raises
        self.body = OFFER if body is None else body
        self.status = status
        self.calls: list[dict] = []

    def request(self, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.status, self.body, {}


def test_a_successful_read_returns_status_and_body():
    c = FakeEbayClient()
    status, body = EbayOfferClient(c).get_offer("offer-1")
    assert status == 200 and body == OFFER


def test_an_api_error_becomes_a_status_code_not_an_exception():
    """Without this the executor never records apply_failed; it just crashes."""
    err = EbayApiError(400, [{"errorId": 25002, "message": "invalid price"}],
                       method="PUT", url="/offer/offer-1")
    status, body = EbayOfferClient(FakeEbayClient(raises=err)).update_offer("offer-1", {})
    assert status == 400
    assert classify(status) is ResponseClass.PERMANENT
    assert "25002" in describe_errors(body)


def test_the_error_array_survives_in_the_shape_describe_errors_reads():
    err = EbayApiError(
        400,
        [{"errorId": 25709, "message": "Invalid value",
          "parameters": [{"name": "categoryId", "value": "0"}]}],
        method="PUT", url="/offer/offer-1",
    )
    _, body = EbayOfferClient(FakeEbayClient(raises=err)).update_offer("offer-1", {})
    assert "categoryId=0" in describe_errors(body)


def test_auth_failures_keep_their_status_so_they_are_not_retried_as_outages():
    err = EbayApiError(401, [], method="GET", url="/offer/offer-1")
    status, _ = EbayOfferClient(FakeEbayClient(raises=err)).get_offer("offer-1")
    assert classify(status) is ResponseClass.AUTH


def test_a_503_stays_transient():
    err = EbayApiError(503, [], method="PUT", url="/offer/offer-1")
    status, _ = EbayOfferClient(FakeEbayClient(raises=err)).update_offer("offer-1", {})
    assert classify(status) is ResponseClass.TRANSIENT


def test_a_connection_failure_is_transient_but_visibly_not_from_ebay():
    c = FakeEbayClient(raises=httpx.ConnectTimeout("timed out"))
    status, body = EbayOfferClient(c).get_offer("offer-1")
    assert status == TRANSPORT_FAILURE_STATUS
    assert classify(status) is ResponseClass.TRANSIENT
    assert "ConnectTimeout reaching eBay" in describe_errors(body)


def test_writes_carry_content_language():
    c = FakeEbayClient()
    EbayOfferClient(c).update_offer("offer-1", {"pricingSummary": {}})
    assert c.calls[0]["headers"] == {"Content-Language": "en-US"}


def test_reads_do_not_send_write_headers():
    c = FakeEbayClient()
    EbayOfferClient(c).get_offer("offer-1")
    assert "headers" not in c.calls[0]


def test_content_language_is_configurable_per_marketplace():
    c = FakeEbayClient()
    EbayOfferClient(c, content_language="en-GB").update_offer("offer-1", {})
    assert c.calls[0]["headers"] == {"Content-Language": "en-GB"}


def test_the_write_is_not_auto_retried_so_the_revision_budget_stays_honest():
    c = FakeEbayClient()
    EbayOfferClient(c).update_offer("offer-1", {})
    assert c.calls[0]["retry_safe"] is False


def test_the_read_is_retry_safe():
    c = FakeEbayClient()
    EbayOfferClient(c).get_offer("offer-1")
    assert c.calls[0]["retry_safe"] is True


def test_calls_use_the_user_token_not_the_application_token():
    c = FakeEbayClient()
    EbayOfferClient(c).update_offer("offer-1", {})
    assert c.calls[0]["auth"] == "user"


def test_the_path_is_the_inventory_offer_endpoint():
    c = FakeEbayClient()
    EbayOfferClient(c).get_offer("offer-9")
    assert c.calls[0]["path"] == "/sell/inventory/v1/offer/offer-9"
    assert c.calls[0]["method"] == "GET"


def test_a_non_dict_body_becomes_none_rather_than_confusing_the_executor():
    status, body = EbayOfferClient(FakeEbayClient(body="")).update_offer("offer-1", {})
    assert status == 200 and body is None


def test_a_non_2xx_returned_rather_than_raised_is_still_handled():
    """Defensive: not every path through the client necessarily raises."""
    c = FakeEbayClient(status=409, body={"errors": [{"errorId": 25001}]})
    status, body = EbayOfferClient(c).update_offer("offer-1", {})
    assert status == 409 and classify(status) is ResponseClass.PERMANENT
