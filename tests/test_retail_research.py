"""Retail research: what a thing costs new, kept apart from what used ones fetch.

MP-000047, the first item a beta tester put through the deployed app. Comp
research fetched `recoveryforathletes.com`, whose page carries `$399.00` four
times for that exact Achedaway massage gun, handed it to the comp extractor, and
got nothing back. The extractor was obeying its instructions -- it is told it is
reading *"one marketplace page"* and listing *"the individual listings it
shows"*, and a shop selling one product new is not that. Eight extractions that
round went to shop pages, produced zero observations, and the marketplace
searches planned as 4 and 5 never ran. The item was priced from a single $45 ask.

So pricing research now has two objectives, counted and stored separately:

    marketplace research  ->  comp_observation  ->  a distribution
    retail research       ->  retail_observation -> a RetailReference

They never meet. `load_scored_comps` cannot reach a retail row because it does
not know the table exists, which is a stronger guarantee than the old one -- a
`price_kind='reference'` comp that every reader had to remember to skip.
"""

from __future__ import annotations

import pytest

from resell import db, store_pricing as sp
from resell.reasoning.comp_loop import (
    RETAIL_EXTRACT_PER_ROUND,
    _price_in_excerpt,
    _squashed,
    retail_query_for,
)
from resell.reasoning.retail_reading import is_retail_source, shop_page_first


def item(tmp_path, **identification):
    from resell.domain import FeeModel
    from resell.gateway import Gateway

    conn = db.connect(tmp_path / "r.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox",
                      fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=3000).sku
    if identification:
        gateway.propose_identification(sku, **identification)
    return conn, gateway, sku


# --- which pages are shops ------------------------------------------------------------


@pytest.mark.parametrize("url,brand,fetched,trust", [
    ("https://achedaway.com/collections/all", "Achedaway", True, 1.00),
    ("https://www.bowflex.com/p/552", "Bowflex", True, 1.00),
    ("https://brooksbrothers.com/p/1", "Brooks Brothers", True, 1.00),
    # Unknown hosts are fetched and must then prove themselves; they are not
    # refused on reputation, and they are believed less if they pass.
    ("https://www.recoveryforathletes.com/products/x", "Achedaway", True, 0.60),
    ("https://pineapple.com/x", "Apple", True, 0.60),
    # Resellers stay out: one page mixes new and third-party offers.
    ("https://www.amazon.com/dp/B08", "Achedaway", False, None),
    ("https://poshmark.com/listing/1", "Achedaway", False, None),
])
def test_who_gets_fetched_and_how_far_they_are_believed(url, brand, fetched, trust):
    from resell.reasoning.retail_reading import trust_for

    assert is_retail_source(url, brand)[0] is fetched
    if trust is not None:
        assert trust_for(url, brand) == trust


def test_a_maker_nobody_registered_still_counts():
    """The first draft of this was an allowlist over `authority.py`, which does
    not contain `bowflex.com` -- and MP-000022's nine current shop prices come
    from exactly there. Requiring registration would have removed working
    behaviour, so this delegates to `makers_own_site` instead."""
    from resell.reasoning.authority import AUTHORITY_BY_DOMAIN

    assert "bowflex.com" not in AUTHORITY_BY_DOMAIN
    assert is_retail_source("https://bowflex.com/p/552", "Bowflex")[0]


def test_amazon_is_not_a_shop_for_this_purpose():
    """It is in the authority table, as a reseller. Its pages mix its own new
    price with third-party offers of every condition, so a number lifted from one
    is not reliably "what this costs new" -- which is the only thing the
    retention table knows how to reason down from."""
    allowed, why = is_retail_source("https://www.amazon.com/dp/B08", "Achedaway")
    assert not allowed
    assert "reseller" in why


def test_the_refusal_says_which_kind_of_no_it_is():
    """"Nothing was found on that page", "that host mixes new and used offers"
    and "the page could not prove itself" are different answers, and a round that
    cannot tell them apart cannot be diagnosed."""
    _, reseller = is_retail_source("https://www.amazon.com/dp/B08", "Achedaway")
    assert "mix new and third-party offers" in reseller
    _, unknown = is_retail_source("https://randomblog.example/post", "Achedaway")
    assert "must prove itself" in unknown


# --- which page to read first ---------------------------------------------------------


def test_priced_pages_are_read_before_front_doors():
    """MP-000047's retail search returned the homepage, a blog post and the
    collection that has the prices. Reading in the order returned spends both
    slots before reaching it."""
    class Doc:
        def __init__(self, url): self.url = url

    ordered = shop_page_first([
        Doc("https://achedaway.com/"),
        Doc("https://achedaway.com/blogs/massage-gun"),
        Doc("https://achedaway.com/collections/achedaway-massage-gun"),
    ])
    assert ordered[0].url.endswith("/collections/achedaway-massage-gun")


def test_only_two_shop_pages_a_round():
    """One good shop price is the whole requirement; a second is corroboration.
    Kept apart from `EXTRACT_PER_ROUND` so a shop page can never again consume an
    extraction the resale market needed."""
    from resell.reasoning.comp_loop import EXTRACT_PER_ROUND

    assert RETAIL_EXTRACT_PER_ROUND == 2
    assert RETAIL_EXTRACT_PER_ROUND < EXTRACT_PER_ROUND


# --- what to search for ---------------------------------------------------------------


def test_the_query_names_the_thing_not_just_the_maker():
    """"Achedaway price new official store" returns the brand's homepage, which
    sells nothing. Naming the product turns up the collection that has prices."""
    said = retail_query_for("Achedaway", None,
                            "Achedaway Percussion Massage Gun - Cordless, USED Excellent")
    assert "Massage Gun" in said
    assert "official site" in said


def test_the_condition_is_stripped_out():
    """The title is written to sell a used one. This query asks a shop what a new
    one costs, and "USED Excellent" asks for the wrong market."""
    said = retail_query_for("XD Design", None, "XD Design Bobby Hero Backpack, New Other")
    for grade in ("used", "new other", "excellent"):
        assert grade not in said.casefold().replace("price official site", "")


def test_a_resolved_model_is_preferred_to_the_title():
    assert retail_query_for("Bowflex", "SelectTech 552", "anything at all") == (
        "Bowflex SelectTech 552 price official site"
    )


def test_nothing_identified_means_no_query():
    assert retail_query_for(None, None, None) == ""


# --- the quotation has to contain the price -------------------------------------------


def test_whitespace_is_normalised_before_comparing():
    """Compared raw, every product on a shop's grid page failed: the model joins
    a title and the price beneath it with a space where the page has a newline,
    and eleven real prices were dropped as fabrications."""
    page = "Achedaway Cupper\n$299.00\nAdd to cart"
    assert _squashed("Achedaway Cupper $299.00") in _squashed(page)


@pytest.mark.parametrize("cents,excerpt,expected", [
    (39900, "Achedaway Pro $399.00 In stock", True),
    (129900, "Deluxe $1,299.00 today", True),
    (39900, "was $499.00 now $299.00", False),
    (39900, "399 dollars", True),
])
def test_a_price_must_be_in_the_words_quoted_for_it(cents, excerpt, expected):
    """A shop page is dense with numbers that are not the product's price --
    delivery thresholds, finance offers, review counts. The whole value of a
    retail reference is that it is the price of a specific thing."""
    assert _price_in_excerpt(cents, excerpt) is expected


# --- storage, and the firewall --------------------------------------------------------


def test_a_shop_price_is_not_a_comp(tmp_path):
    """The guarantee the separate table buys. `load_scored_comps` cannot reach a
    retail row because it does not know the table exists."""
    conn, _, sku = item(tmp_path)
    retail_id = sp.record_retail_observation(
        conn, sku=sku, url="https://achedaway.com/c", host="achedaway.com",
        product_title="Achedaway Pro", price_cents=39900,
        source_authority="manufacturer", source_excerpt="Achedaway Pro $399.00")
    sp.record_retail_claim(conn, sku=sku, retail_id=retail_id, match="same_product",
                           item_citations=("1",), retail_citations=("title",))
    assert sp.load_scored_comps(conn, sku) == []
    assert len(sp.load_retail_references(conn, sku)) == 1


def test_an_unjudged_price_is_not_yet_evidence(tmp_path):
    """A shop price nobody has matched to the item says nothing about the item."""
    conn, _, sku = item(tmp_path)
    sp.record_retail_observation(
        conn, sku=sku, url="https://achedaway.com/c", host="achedaway.com",
        product_title="Achedaway Cupper", price_cents=29900,
        source_authority="manufacturer", source_excerpt="Cupper $299.00")
    assert sp.load_retail_references(conn, sku) == []
    assert len(sp.unjudged_retail(conn, sku)) == 1
    assert not sp.has_trustworthy_retail(conn, sku)


def test_an_exclusion_must_say_why(tmp_path):
    """Same discipline as comps: a shop price neither counted nor accounted for
    is what makes a pricing round impossible to read afterwards."""
    conn, _, sku = item(tmp_path)
    retail_id = sp.record_retail_observation(
        conn, sku=sku, url="https://achedaway.com/c", host="achedaway.com",
        product_title="Tablet Holder", price_cents=2999,
        source_authority="manufacturer", source_excerpt="Tablet Holder $29.99")
    with pytest.raises(ValueError, match="must say why"):
        sp.record_retail_claim(conn, sku=sku, retail_id=retail_id, match="excluded")


def test_discovery_stops_once_the_question_is_answered(tmp_path):
    """A shop price does not change between two rounds of one run. Asking again
    spends a lookup to learn something already on the record."""
    conn, _, sku = item(tmp_path)
    assert not sp.has_trustworthy_retail(conn, sku)
    retail_id = sp.record_retail_observation(
        conn, sku=sku, url="https://achedaway.com/c", host="achedaway.com",
        product_title="Achedaway Pro", price_cents=39900,
        source_authority="manufacturer", source_excerpt="Achedaway Pro $399.00")
    sp.record_retail_claim(conn, sku=sku, retail_id=retail_id, match="same_product",
                           item_citations=("1",), retail_citations=("title",))
    assert sp.has_trustworthy_retail(conn, sku)


def test_a_variant_does_not_close_the_question(tmp_path):
    """`same_family_variant` may anchor, but it is not the answer discovery was
    looking for -- a later round may still find the exact product."""
    conn, _, sku = item(tmp_path)
    retail_id = sp.record_retail_observation(
        conn, sku=sku, url="https://bowflex.com/c", host="bowflex.com",
        product_title="SelectTech 1090", price_cents=69900,
        source_authority="unknown", source_excerpt="1090 $699.00")
    sp.record_retail_claim(conn, sku=sku, retail_id=retail_id,
                           match="same_family_variant",
                           item_citations=("1",), retail_citations=("title",))
    assert not sp.has_trustworthy_retail(conn, sku)


def test_the_source_and_how_far_it_was_believed_are_both_recorded(tmp_path):
    """Stored rather than recomputed at read time, so widening which hosts may
    contribute later cannot silently re-weight evidence gathered under the older
    rule. Today everything reaching here is believed outright; the column is what
    lets an unknown retailer be admitted below 1.0 without a migration."""
    conn, _, sku = item(tmp_path)
    retail_id = sp.record_retail_observation(
        conn, sku=sku, url="https://achedaway.com/c", host="achedaway.com",
        product_title="Achedaway Pro", price_cents=39900,
        source_authority="manufacturer", source_trust=0.5,
        source_excerpt="Achedaway Pro $399.00")
    sp.record_retail_claim(conn, sku=sku, retail_id=retail_id, match="same_product",
                           item_citations=("1",), retail_citations=("title",))
    row = sp.load_retail_references(conn, sku)[0]
    assert row["source_authority"] == "manufacturer"
    assert row["source_trust"] == 0.5


def test_retail_reaches_pricing_as_a_reference_not_a_comp(tmp_path):
    """End of the path. `views.pricing_input` turns these rows into
    `RetailReference`, which is the only shape pricing accepts them in."""
    from resell import views
    from resell.pricing.comps import Comparability, RetailKind

    conn, _, sku = item(tmp_path, category_id="36449", condition_id="USED_GOOD")
    retail_id = sp.record_retail_observation(
        conn, sku=sku, url="https://achedaway.com/c", host="achedaway.com",
        product_title="Achedaway Pro", price_cents=39900,
        source_authority="manufacturer", source_excerpt="Achedaway Pro $399.00")
    sp.record_retail_claim(conn, sku=sku, retail_id=retail_id, match="same_product",
                           item_citations=("1",), retail_citations=("title",))

    request = views.default_pricing_request(conn, sku, marketplace="EBAY_US")
    built, scored = views.pricing_input(conn, sku, request)
    assert scored == [], "no comps were invented"
    assert len(built.retail) == 1
    reference = built.retail[0]
    assert reference.price_cents == 39900
    assert reference.kind is RetailKind.CURRENT
    assert reference.match is Comparability.SAME_PRODUCT
    assert reference.prices_this_item


def test_an_excluded_shop_price_never_reaches_pricing(tmp_path):
    """MP-000022's tablet holder, arriving by the new path instead of the old."""
    from resell import views

    conn, _, sku = item(tmp_path, category_id="36449", condition_id="USED_GOOD")
    retail_id = sp.record_retail_observation(
        conn, sku=sku, url="https://achedaway.com/c", host="achedaway.com",
        product_title="Achedaway Cupper", price_cents=29900,
        source_authority="manufacturer", source_excerpt="Cupper $299.00")
    sp.record_retail_claim(
        conn, sku=sku, retail_id=retail_id, match="excluded",
        item_citations=("1",), retail_citations=("title",),
        excluded_reason="a cupping device, not a massage gun")
    built, _ = views.pricing_input(
        conn, sku, views.default_pricing_request(conn, sku, marketplace="EBAY_US"))
    assert built.retail == ()


# --- unknown shops, admitted on what the page proves ----------------------------------
#
# `recoveryforathletes.com` sells the exact Achedaway massage gun on MP-000047's
# table and publishes a `schema.org/Product` offer for it. Under host reputation
# alone it was worth nothing, and the item priced from a single $45 ask.
#
# The HTML below is the shape of that page's structured data, not the page: the
# tests must not depend on a third party's markup staying still.

PRODUCT_PAGE = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Achedaway Percussion Massage Gun",
 "brand":{"@type":"Brand","name":"Achedaway"},
 "offers":{"@type":"Offer","price":229.0,"priceCurrency":"USD",
           "availability":"https://schema.org/InStock"}}
</script></head>
<body><button>Add to cart</button></body></html>
"""


def validated(html=PRODUCT_PAGE, url="https://shop.example/products/x", brand="Achedaway"):
    from resell.reasoning.retail_reading import validate_product_page

    return validate_product_page(url, html, brand)


def test_a_page_that_proves_itself_is_admitted():
    product, why = validated()
    assert product is not None, why
    assert product.price_cents == 22900
    assert product.currency == "USD"
    assert product.in_stock is True
    assert "schema.org Product" in why


def test_the_structured_price_is_the_price():
    """No model reads this. A site publishing `schema.org/Product` has asserted
    the price in machine-readable form, which is stronger attribution than a
    model reading prose -- and it is what justifies admitting a host nobody
    vouched for."""
    product, _ = validated()
    assert product.price_cents == 22900
    assert "229" in product.excerpt


@pytest.mark.parametrize("url,expected_reason", [
    ("https://shop.example/collections/all", "not a single product's page"),
    ("https://shop.example/search?q=x", "not a single product's page"),
    ("https://shop.example/coupon/achedaway", "not a single product's page"),
    ("https://shop.example/reviews/achedaway", "not a single product's page"),
    ("https://marketplace.example/itm/12345", "not a single product's page"),
])
def test_pages_that_are_not_one_product_are_refused(url, expected_reason):
    product, why = validated(url=url)
    assert product is None
    assert expected_reason in why


def test_a_page_with_no_structured_offer_is_refused():
    """`couponannie.com/stores/achedaway` has three JSON-LD blocks and no Product
    with an offer. It fails here and never reaches extraction."""
    product, why = validated(html="<html><body>Achedaway $229 great deal</body></html>")
    assert product is None
    assert "no schema.org Product" in why


def test_a_page_for_a_different_brand_is_refused():
    product, why = validated(brand="Bowflex")
    assert product is None
    assert "not a Bowflex" in why


def test_a_page_nobody_can_buy_from_is_refused():
    """A review quoting a price is not a shop. The affordance is what separates
    'this is for sale here' from 'this costs about that'."""
    product, why = validated(html=PRODUCT_PAGE.replace("Add to cart", "Read our review"))
    assert product is None
    assert "nothing on the page can be bought" in why


def test_two_products_on_one_page_are_refused():
    """Attribution is too weak on a grid for a host nobody vouched for, even
    though a known maker's collection page is read happily."""
    second = PRODUCT_PAGE.replace(
        '"name":"Achedaway Percussion Massage Gun"', '"name":"Achedaway Cupper"')
    product, why = validated(html=PRODUCT_PAGE + second)
    assert product is None
    assert "attribution too weak" in why


def test_an_offer_without_a_price_is_refused():
    product, why = validated(html=PRODUCT_PAGE.replace('"price":229.0,', ""))
    assert product is None
    assert "no price and currency" in why


# --- how far each kind of source is believed ------------------------------------------


def test_the_trust_ladder():
    from resell.reasoning.retail_reading import SOURCE_TRUST

    assert SOURCE_TRUST["manufacturer"] == 1.00
    assert SOURCE_TRUST["authorised_retailer"] == 0.90
    assert SOURCE_TRUST["page_validated"] == 0.60
    assert (SOURCE_TRUST["page_validated"] < SOURCE_TRUST["authorised_retailer"]
            < SOURCE_TRUST["manufacturer"])


def test_source_trust_reaches_the_anchor():
    """It used to be impossible for `match` to reach `anchor_trust` at all --
    `RetailAnchor` had no such field, so `getattr` fell through to the default
    and a family-variant shop price was trusted exactly like an exact one."""
    from resell.pricing.comps import Comparability, ConditionBand
    from resell.pricing.estimate import anchor_trust
    from resell.pricing.retention import anchor_from_retail

    def trust(match, source):
        return anchor_trust(anchor_from_retail(
            22900, "Health & Beauty > Massage", ConditionBand.USED_GOOD,
            match=match, source_trust=source))

    exact_maker = trust(Comparability.SAME_PRODUCT, 1.0)
    exact_unknown = trust(Comparability.SAME_PRODUCT, 0.6)
    variant_unknown = trust(Comparability.SAME_FAMILY_VARIANT, 0.6)
    assert exact_maker > exact_unknown > variant_unknown > 0
    assert exact_unknown == pytest.approx(exact_maker * 0.6)


def test_an_unknown_shop_never_outweighs_a_real_market():
    """0.60 is low enough that a strong marketplace sample still dominates and
    high enough to beat nothing at all -- which is what such a shop was worth
    before this existed."""
    from resell.pricing.comps import Comparability, ConditionBand
    from resell.pricing.estimate import anchor_share, anchor_trust
    from resell.pricing.retention import anchor_from_retail

    anchor = anchor_from_retail(22900, None, ConditionBand.USED_GOOD,
                                match=Comparability.SAME_PRODUCT, source_trust=0.6)
    strong_market = anchor_share(0.81, anchor_trust(anchor))
    no_market = anchor_share(0.0, anchor_trust(anchor))
    assert strong_market < 0.02, "twenty realized exact sales all but silence it"
    assert no_market > 0.4, "with nothing else, it is most of the answer"
