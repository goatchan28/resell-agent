"""eBay's own APIs as a comp source.

Different from every other retrieval path in one way that changes the design: the
data arrives structured, so no model reads it. That removes the extraction stage,
removes the excerpt, and -- because eBay's licence forbids this data reaching a
third-party AI without written consent -- removes the judging stage too, replacing
it with a comparability computed from catalogue identifiers.

These tests use recorded response shapes rather than live calls. Nothing here
reaches eBay.
"""

from __future__ import annotations

import pytest

from resell import db, store_pricing as sp
from resell.domain import FeeModel
from resell.ebay.comps import (
    BROWSE_SOURCE,
    INSIGHTS_SOURCE,
    CompAccessError,
    EbayCompAdapter,
    comparability_from_identifiers,
    observation_from_item_summary,
)
from resell.gateway import Gateway
from resell.pricing.comps import (
    CompBasis,
    Comparability,
    ConditionBand,
    ModelVisibility,
    PriceKind,
)

# A Browse API item_summary, trimmed to the fields the adapter reads.
ACTIVE_ITEM = {
    "itemId": "v1|110590229841|0",
    "title": "Beats Pill Portable Speaker Red",
    "epid": "26057191400",
    "categoryId": "111694",
    "condition": "Pre-owned",
    "conditionId": "3000",
    "price": {"value": "149.99", "currency": "USD"},
    "shippingOptions": [{"shippingCost": {"value": "5.00", "currency": "USD"},
                         "shippingCostType": "FIXED"}],
    "itemWebUrl": "https://www.ebay.com/itm/110590229841",
    "buyingOptions": ["FIXED_PRICE"],
    "seller": {"sellerAccountType": "INDIVIDUAL"},
}

SOLD_ITEM = dict(
    ACTIVE_ITEM,
    lastSoldPrice={"value": "132.00", "currency": "USD"},
    lastSoldDate="2026-08-01T10:00:00.000Z",
)
SOLD_ITEM.pop("price")


def fixture(tmp_path):
    conn = db.connect(tmp_path / "ebay_comps.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    return conn, gateway, sku


# --- mapping the response ---------------------------------------------------------


def observation(item, **kw):
    kw.setdefault("marketplace", "EBAY_US")
    kw.setdefault("query", "beats pill")
    kw.setdefault("price_kind", PriceKind.ASKING)
    kw.setdefault("exact", False)
    kw.setdefault("visibility", ModelVisibility.DERIVED_ONLY)
    kw.setdefault("source", BROWSE_SOURCE)
    return observation_from_item_summary(item, **kw)


def test_an_active_item_becomes_an_asking_comp():
    obs = observation(ACTIVE_ITEM)
    assert obs.price_kind is PriceKind.ASKING
    assert obs.basis is CompBasis.ACTIVE_SIMILAR
    assert obs.price_cents == 14999
    assert obs.shipping_cents == 500


def test_a_sold_item_becomes_a_realised_comp_with_its_date():
    obs = observation(SOLD_ITEM, price_kind=PriceKind.REALIZED, source=INSIGHTS_SOURCE)
    assert obs.price_kind is PriceKind.REALIZED
    assert obs.basis is CompBasis.SOLD_SIMILAR
    assert obs.price_cents == 13200
    assert obs.sale_date.isoformat() == "2026-08-01"


def test_an_exact_catalogue_match_upgrades_the_basis():
    obs = observation(ACTIVE_ITEM, exact=True)
    assert obs.basis is CompBasis.ACTIVE_EXACT


def test_the_condition_id_maps_onto_the_ladder():
    """eBay's numeric condition, through the table the pricing layer already owns."""
    obs = observation(ACTIVE_ITEM)
    assert obs.condition_band is ConditionBand.USED_GOOD
    assert obs.condition_declared_raw == "Pre-owned"


def test_calculated_postage_is_unknown_not_free():
    """`CALCULATED` with no figure means it depends on the destination. Recording
    zero would assert free postage, which is a different and usually wrong claim."""
    item = dict(ACTIVE_ITEM, shippingOptions=[{"shippingCostType": "CALCULATED"}])
    assert observation(item).shipping_cents is None


def test_no_shipping_options_is_unknown():
    item = dict(ACTIVE_ITEM)
    item.pop("shippingOptions")
    assert observation(item).shipping_cents is None


def test_free_postage_is_recorded_as_zero_not_as_unknown():
    item = dict(ACTIVE_ITEM, shippingOptions=[
        {"shippingCost": {"value": "0.00", "currency": "USD"},
         "shippingCostType": "FIXED"}])
    assert observation(item).shipping_cents == 0


def test_an_item_with_no_price_is_skipped():
    item = dict(ACTIVE_ITEM)
    item.pop("price")
    assert observation(item) is None


def test_the_payload_hash_anchors_the_row_without_copying_ebays_data():
    a = observation(ACTIVE_ITEM)
    b = observation(ACTIVE_ITEM)
    assert a.raw_payload_hash == b.raw_payload_hash
    assert observation(dict(ACTIVE_ITEM, title="different")).raw_payload_hash != (
        a.raw_payload_hash
    )


def test_no_excerpt_is_stored_for_a_structured_source():
    """The excerpt exists because a model read prose. Here the number arrived in a
    field named `price.value`, and the payload hash is the audit anchor."""
    assert observation(ACTIVE_ITEM).source_excerpt is None


# --- comparability from identifiers, not from titles ---------------------------------


def ladder(item, *, epid=None, category="111694", ceiling=Comparability.SAME_PRODUCT):
    return comparability_from_identifiers(
        item, item_epid=epid, item_category_id=category, ceiling=ceiling
    )


def test_a_matching_epid_is_the_same_product():
    """eBay's own catalogue saying so, rather than a model comparing two titles."""
    rung, why = ladder(ACTIVE_ITEM, epid="26057191400")
    assert rung is Comparability.SAME_PRODUCT
    assert "ePID 26057191400 matches" in why


def test_a_different_epid_is_a_family_variant():
    rung, why = ladder(dict(ACTIVE_ITEM, epid="99999"), epid="26057191400")
    assert rung is Comparability.SAME_FAMILY_VARIANT
    assert "different catalogue product" in why


def test_no_epid_on_either_side_falls_to_the_category():
    item = dict(ACTIVE_ITEM)
    item.pop("epid")
    rung, _ = ladder(item, epid=None)
    assert rung is Comparability.CATEGORY_ATTRIBUTE


def test_nothing_matching_supports_nothing():
    item = dict(ACTIVE_ITEM, categoryId="99999")
    item.pop("epid")
    rung, _ = ladder(item, epid=None)
    assert rung is Comparability.SUPERFICIAL
    assert not rung.contributes


def test_the_identity_ceiling_caps_the_rung():
    """Pricing inherits identification's limits even when the catalogue agrees."""
    rung, why = ladder(
        ACTIVE_ITEM, epid="26057191400", ceiling=Comparability.SAME_FAMILY_VARIANT
    )
    assert rung is Comparability.SAME_FAMILY_VARIANT
    assert "caps this at" in why


# --- the policy gate ----------------------------------------------------------------


class FakeClient:
    def __init__(self, body=None, error=None):
        self._body = body or {}
        self._error = error
        self.calls: list[tuple] = []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self._error:
            raise self._error
        return self._body


def test_calling_without_a_recorded_policy_is_refused(tmp_path):
    """Everywhere else an unrecorded source falls back to derived_only. Here the
    absence means nobody has read the licence and decided, and defaulting would
    make that decision by omission."""
    conn, _, _ = fixture(tmp_path)
    adapter = EbayCompAdapter(FakeClient(), conn)
    with pytest.raises(CompAccessError, match="no source policy recorded"):
        adapter.search_active("beats pill")


def test_the_refusal_names_the_command_that_fixes_it(tmp_path):
    conn, _, _ = fixture(tmp_path)
    adapter = EbayCompAdapter(FakeClient(), conn)
    with pytest.raises(CompAccessError, match="resell price source-policy set"):
        adapter.search_active("beats pill")


def test_the_two_apis_are_separate_grants(tmp_path):
    """A decision about Browse is not a decision about Marketplace Insights."""
    conn, _, _ = fixture(tmp_path)
    sp.set_source_policy(
        conn, source=BROWSE_SOURCE, model_visibility="derived_only",
        policy_version="2025-06-24",
    )
    adapter = EbayCompAdapter(FakeClient({"itemSummaries": []}), conn)
    adapter.search_active("beats pill")          # permitted
    with pytest.raises(CompAccessError, match=INSIGHTS_SOURCE):
        adapter.search_sold("beats pill")


def test_the_recorded_visibility_travels_onto_every_row(tmp_path):
    conn, _, _ = fixture(tmp_path)
    sp.set_source_policy(
        conn, source=BROWSE_SOURCE, model_visibility="derived_only",
        policy_version="2025-06-24",
    )
    adapter = EbayCompAdapter(FakeClient({"itemSummaries": [ACTIVE_ITEM]}), conn)
    result = adapter.search_active("beats pill")
    assert [o.model_visibility for o in result.observations] == [
        ModelVisibility.DERIVED_ONLY
    ]


def test_a_403_is_reported_as_an_access_grant_not_a_broken_token(tmp_path):
    """They are indistinguishable from the status code, and the remedy is entirely
    different: one is re-auth, the other is an eBay Partner Network application."""
    from resell.ebay.client import EbayApiError

    conn, _, _ = fixture(tmp_path)
    sp.set_source_policy(
        conn, source=BROWSE_SOURCE, model_visibility="derived_only",
        policy_version="2025-06-24",
    )
    error = EbayApiError(403, [{"message": "Insufficient permissions"}],
                         method="GET", url="x")
    adapter = EbayCompAdapter(FakeClient(error=error), conn)
    with pytest.raises(CompAccessError, match="eBay Partner Network"):
        adapter.search_active("beats pill")


def test_the_marketplace_header_is_sent(tmp_path):
    conn, _, _ = fixture(tmp_path)
    sp.set_source_policy(
        conn, source=BROWSE_SOURCE, model_visibility="derived_only",
        policy_version="2025-06-24",
    )
    client = FakeClient({"itemSummaries": []})
    EbayCompAdapter(client, conn, marketplace="EBAY_GB").search_active("beats")
    _, kwargs = client.calls[0]
    assert kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"
    assert kwargs["auth"] == "app"


# --- what these rows may be shown to -------------------------------------------------


def test_ebay_rows_are_priceable_but_never_promptable(tmp_path):
    """The licence question, as a property of the system rather than a promise.

    `estimate.py` reads storage and never sees a prompt, so a derived_only comp
    prices the item exactly as a full one does.
    """
    conn, gateway, sku = fixture(tmp_path)
    sp.set_source_policy(
        conn, source=BROWSE_SOURCE, model_visibility="derived_only",
        policy_version="2025-06-24",
    )
    adapter = EbayCompAdapter(FakeClient({"itemSummaries": [ACTIVE_ITEM]}), conn)
    for obs in adapter.search_active("beats pill").observations:
        sp.record_comp_observation(conn, obs)
        sp.record_comp_claim(
            conn,
            __import__("resell.pricing.comps", fromlist=["CompClaim"]).CompClaim(
                claim_id="claim_1", sku=sku, comp_id=obs.comp_id,
                comparability=Comparability.CATEGORY_ATTRIBUTE,
                item_citations=("epid",), comp_citations=("categoryId",),
                rationale="same category",
            ),
            identity_resolution="searched_not_found",
        )

    promptable, withheld = sp.promptable_comps(conn, sku)
    assert promptable == []
    assert len(withheld) == 1
    # And still fully available to the arithmetic.
    assert len(sp.load_scored_comps(conn, sku)) == 1


def test_a_source_marked_full_would_be_promptable(tmp_path):
    """The mechanism is a policy, not a hardcoded opinion about eBay. If written
    consent were obtained, recording it changes the behaviour."""
    conn, gateway, sku = fixture(tmp_path)
    sp.set_source_policy(
        conn, source=BROWSE_SOURCE, model_visibility="full",
        policy_version="2026-01-01", licence_ref="written consent on file",
    )
    adapter = EbayCompAdapter(FakeClient({"itemSummaries": [ACTIVE_ITEM]}), conn)
    [obs] = adapter.search_active("beats pill").observations
    assert obs.model_visibility is ModelVisibility.FULL
