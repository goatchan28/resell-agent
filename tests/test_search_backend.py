"""Turning a query into pages, and a priced result into an asking comp.

The seam that had been empty since the research loops were written: both could
plan queries and read pages, neither could find one. Two things are tested here
and they pull in opposite directions.

The first is that a search result may become pricing evidence at all. A structured
price from an index is weaker than a fetched listing and stronger than nothing, and
the estimator already has a shape for exactly that -- an asking comp with an
unknown condition band.

The second is that it must never become more than that. Brave returns no sold
listings and no condition, so `realized` and any condition band are unavailable by
construction rather than by policy, and the tests below fix that in place: the
parser is fed the real captured response and asked what it refuses to say.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from resell.pricing.comps import (
    ConditionBand, ConditionSource, PriceKind, RetrievalMethod,
)
from resell.reasoning.adapters.research import ResearchError, ResearchQuery
from resell.reasoning.adapters.search import (
    BraveSearchBackend, NoSearchBackend, SearchHit, get_search_backend,
    hits_as_asking_comps,
)

NOW = datetime(2026, 8, 22, tzinfo=UTC)
QUERY = ResearchQuery("Beats Pill+ A3211 red", "marketplace", "find comparable asks")

# Trimmed from a real response. The shapes matter: `product.offers[]` for a single
# listing, `product_cluster[]` for a category page, and a result with neither.
BRAVE_PAYLOAD = {
    "web": {"results": [
        {
            "url": "https://www.ebay.com/itm/388632651611",
            "title": "Beats Pill Wireless Portable Bluetooth Speaker Red A3211 | eBay",
            "description": "Beats Pill Wireless Portable Bluetooth Speaker Red A3211",
            "extra_snippets": ["MPN · A3211 · Charger Included · No"],
            "page_age": "2026-04-04T14:45:22",
            "product": {
                "name": "Beats Pill Wireless Portable Bluetooth Speaker Red A3211",
                "price": "78.0",
                "offers": [{"url": "https://www.ebay.com/itm/388632651611",
                            "priceCurrency": "USD", "price": "78.0"}],
            },
        },
        {
            "url": "https://www.ebay.com/b/beats-pill",
            "title": "Beats Pill Bluetooth Docks & Mini Speakers for sale - eBay",
            "description": "Get the best deals",
            "product_cluster": [
                {"name": "Beats Pill Portable Speaker A3211 Black",
                 "url": "https://www.ebay.com/itm/236894454572", "price": "74.0"},
                {"name": "2x OEM Apple BEATS MICRO USB CABLE CHARGER",
                 "url": "https://www.ebay.com/itm/257680906175", "price": "9.99"},
            ],
        },
        {
            "url": "https://www.t-mobile.com/beats-pill",
            "title": "Beats Pill: Prices, Colors, Features & Specs",
            "description": "24 hours of battery life",
        },
    ]},
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        return FakeResponse(self.payload)


def backend():
    return BraveSearchBackend(api_key="test-key", client=FakeClient(BRAVE_PAYLOAD))


# --- the backend returns URLs, and says what it costs --------------------------------


def test_a_query_becomes_hits():
    hits = backend().find(QUERY, limit=10)
    assert [h.url for h in hits][:1] == ["https://www.ebay.com/itm/388632651611"]
    assert any(h.host == "t-mobile.com" for h in hits)


def test_a_cluster_entry_becomes_its_own_hit():
    """Flattening them onto the page they were found on would attribute a dozen
    different prices to one URL, and nothing downstream could separate them."""
    hits = backend().find(QUERY, limit=20)
    cluster = [h for h in hits if h.url.endswith("236894454572")]
    assert len(cluster) == 1
    assert cluster[0].price_cents == 7400


def test_a_result_with_no_commerce_data_still_routes():
    """Identification research wants the page whether or not it has a price."""
    hits = backend().find(QUERY, limit=20)
    spec_page = next(h for h in hits if h.host == "t-mobile.com")
    assert not spec_page.priced
    assert spec_page.url


def test_the_search_asks_for_the_extra_snippets():
    """They are free -- billing is per request -- and the specification text lives
    in them rather than in the description."""
    client = FakeClient(BRAVE_PAYLOAD)
    BraveSearchBackend(api_key="k", client=client).find(QUERY)
    assert client.calls[0][1]["extra_snippets"] == "true"
    assert client.calls[0][2]["X-Subscription-Token"] == "k"


def test_the_price_of_a_search_is_per_request_not_per_result():
    assert BraveSearchBackend(api_key="k").cost_micros_per_search() == 5000


def test_a_missing_key_is_refused_before_any_request(monkeypatch):
    """No key means no request, rather than a request that fails at the far end.

    monkeypatch is doing real work here: loading config puts a real key from .env
    into the process environment, and the backend falls back to it -- which is the
    behaviour we want in production and hides this check in a test suite.
    """
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(ResearchError, match="BRAVE_API_KEY"):
        BraveSearchBackend(api_key="").find(QUERY)


def test_no_backend_refuses_rather_than_returning_nothing():
    """An empty list would be read as "searched, found nothing", which moves an
    item to `searched_not_found` -- a claim about the object that would be false."""
    with pytest.raises(ResearchError, match="no search backend"):
        NoSearchBackend().find(QUERY)


def test_the_default_backend_is_none(monkeypatch):
    monkeypatch.delenv("RESELL_SEARCH_BACKEND", raising=False)
    assert isinstance(get_search_backend(), NoSearchBackend)


# --- a priced hit as an asking comp, and the four things it may never claim ---------


def priced_comps(**kwargs):
    hits = backend().find(QUERY, limit=20)
    kwargs.setdefault("identity_terms", ("Beats", "Pill", "A3211"))
    kwargs.setdefault("now", NOW)
    return hits_as_asking_comps(hits, **kwargs)


def test_a_priced_result_becomes_an_asking_comp():
    comps, _ = priced_comps()
    assert comps
    assert all(c.price_kind is PriceKind.ASKING for c in comps)


def test_it_is_never_a_realized_sale():
    """Search engines do not index eBay's sold pages: 69 eBay results across five
    live queries returned zero. So this is unavailable by construction, and the
    constant is the guard."""
    comps, _ = priced_comps()
    assert not any(c.price_kind is PriceKind.REALIZED for c in comps)


def test_no_condition_is_inferred():
    """Every `offers` object Brave returned carried exactly url, priceCurrency and
    price. Condition words appear only in catalogue boilerplate that is present
    whatever is listed."""
    comps, _ = priced_comps()
    assert all(c.condition_band is ConditionBand.UNKNOWN for c in comps)
    assert all(c.condition_source is ConditionSource.UNSTATED for c in comps)
    assert all(c.condition_declared_raw is None for c in comps)


def test_the_provenance_is_not_a_fetch():
    """The bytes were never loaded. Recording this as `automated_fetch` would make
    an index's summary indistinguishable from a page we read."""
    comps, _ = priced_comps()
    assert all(c.retrieval_method is RetrievalMethod.SEARCH_INDEX for c in comps)


def test_shipping_is_unknown_rather_than_free():
    comps, _ = priced_comps()
    assert all(c.shipping_cents is None for c in comps)


def test_the_page_age_becomes_the_observation_time():
    """So a price the index last saw months ago is stale on arrival, and the
    staleness qualifier fires without anything special-casing search comps."""
    comps, _ = priced_comps()
    dated = next(c for c in comps if c.external_id == "388632651611")
    assert dated.observed_at == datetime(2026, 4, 4, 14, 45, 22, tzinfo=UTC)


def test_a_hit_with_no_page_age_falls_back_to_the_capture_time():
    comps, _ = priced_comps()
    undated = next(c for c in comps if c.external_id == "236894454572")
    assert undated.observed_at == NOW


def test_the_index_text_is_kept_but_not_offered_as_an_excerpt():
    comps, _ = priced_comps()
    speaker = next(c for c in comps if c.external_id == "388632651611")
    assert "A3211" in speaker.source_excerpt
    assert speaker.adapter == "brave"
    assert speaker.query_text == QUERY.query


# --- the noise that comes with it ---------------------------------------------------


def test_an_off_product_result_is_dropped_before_it_costs_anything():
    """eBay's related-items carousels put charging cables in the same response as
    the speaker; one real query returned 229 priced entries this way."""
    comps, notes = priced_comps()
    assert not any(c.external_id == "257680906175" for c in comps)
    assert any("off-product" in n for n in notes)


def test_the_cap_is_reported_rather_than_applied_silently():
    comps, notes = priced_comps(max_comps=1)
    assert len(comps) == 1
    assert any("more were available" in n for n in notes)


def test_the_same_listing_found_twice_is_one_comp():
    """URL tracking parameters differ between searches, so the path identifies the
    listing. Without this the sample double-counts and nobody can see it."""
    hits = [
        SearchHit(url="https://www.ebay.com/itm/999?_trk=abc", title="Beats Pill A3211",
                  price_cents=7000),
        SearchHit(url="https://www.ebay.com/itm/999?_trk=xyz", title="Beats Pill A3211",
                  price_cents=7000),
    ]
    comps, _ = hits_as_asking_comps(
        hits, identity_terms=("Beats", "Pill"), now=NOW,
    )
    assert len(comps) == 1


def test_a_marketplace_filter_keeps_other_hosts_out():
    comps, _ = priced_comps(marketplace="ebay.com")
    assert {c.marketplace for c in comps} == {"ebay.com"}
