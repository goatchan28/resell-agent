"""Which pages are shops, and what a shop page is worth.

MP-000047 is the case. Comp research fetched `recoveryforathletes.com`, whose
page carries `$399.00` four times for the exact Achedaway massage gun on the
table, handed it to the comp extractor, and got nothing back. The extractor was
obeying its instructions: it is told it is *"reading one marketplace page and
listing the individual listings it shows"*, and a shop selling one product new is
not that. Eight extraction calls went to shop pages that round and produced zero
observations, while the item was priced from a single $45 eBay ask.

`retail_from_source` in `comp_reading` can only reclassify an observation the
comp extractor already produced, so a shop page that does not look like a
listings page has never been able to become retail evidence. The retention table
and the anchor were built on evidence that could not arrive.

So: classify the page *before* choosing an extractor, and give shop pages their
own one.

**What this module decides and what it does not.** It decides whether a URL is a
shop we recognise. It does not decide whether the page is about the item on the
table -- that is the judge's question, answered the same way it is for comps, and
recorded as `RetailReference.match`. Two separate doubts, kept separate: *is this
a real retail price* and *is it a retail price for this thing*.

**Allowlist, failing closed**, for the same reason `authority.py` is one. A host
nobody has classified is not a shop as far as this is concerned. The cost is that
a genuine retailer contributes nothing until somebody adds it, and that is the
right way round -- the alternative guesses upward, and a price scraped from a
blog quoting a rumour would anchor a real listing.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from resell.reasoning.authority import authority_for_url
from resell.reasoning.comp_reading import makers_own_site
from resell.reasoning.research import SourceAuthority

__all__ = [
    "RETAIL_AUTHORITIES", "SOURCE_TRUST", "ValidatedProduct",
    "is_retail_source", "shop_page_first", "trust_for", "validate_product_page",
]


# Whose stated price is a *retail* price: what the thing costs new, from someone
# entitled to sell it new.
#
# `RESELLER` is deliberately absent even though Amazon and Walmart sit under it in
# the authority table. Their pages mix the retailer's own new price with
# third-party offers of every condition, often on one page -- so a number lifted
# from one is not reliably "what this costs new", which is the only thing the
# retention table knows how to reason down from. They stay marketplace pages until
# somebody works out how to read them properly, which is its own piece of work
# with its own failure modes.
RETAIL_AUTHORITIES: frozenset[SourceAuthority] = frozenset({
    SourceAuthority.MANUFACTURER,
    SourceAuthority.AUTHORISED_RETAILER,
})


def is_retail_source(url: str, brand: str | None = None) -> tuple[bool, str]:
    """Whether a price read from this URL is evidence of what the thing costs new.

    Two ways to qualify, and the second is why this delegates rather than keeping
    its own table. `makers_own_site` already answers "is this the brand selling
    its own product" by matching the host label against the brand -- `bowflex.com`
    for a Bowflex, `achedaway.com` for an Achedaway -- so a maker nobody has
    registered still counts. Requiring registration here would have *removed*
    working behaviour: MP-000022's nine current shop prices come from
    `bowflex.com`, which is not in the authority table.

    The table still wins where it has an opinion, because it also covers shops
    that do not share the brand's name.

    Returns the decision and the reason. "Nothing was found on that page" and "we
    do not treat that host as a shop" are different answers, and a round that
    cannot tell them apart cannot be diagnosed later.
    """
    authority, why = authority_for_url(url)
    host = urlsplit(url).netloc.casefold()
    for prefix in ("www.", "shop.", "store."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if makers_own_site(host, brand, authority):
        if authority in RETAIL_AUTHORITIES:
            return True, f"{authority}: {why}"
        return True, f"{host} is {brand}'s own site"
    if authority is SourceAuthority.RESELLER:
        # Amazon, Walmart and the like. One page carries the retailer's own new
        # price and third-party offers of every condition, so a number lifted
        # from it is not reliably "what this costs new". Reading them properly is
        # its own piece of work.
        return False, f"{authority} pages mix new and third-party offers"
    # Unknown. Not a shop by reputation, and possibly a shop by evidence: fetch
    # it and let `validate_product_page` decide. Failing closed here is what kept
    # `recoveryforathletes.com` -- a real retailer with a machine-readable price
    # for the exact item -- out of MP-000047's pricing entirely.
    return True, "unknown host; the page must prove itself"


def trust_for(url: str, brand: str | None) -> float:
    """How far this source's own statement of its price is believed."""
    authority, _ = authority_for_url(url)
    if authority in RETAIL_AUTHORITIES:
        return SOURCE_TRUST[str(authority)]
    host = urlsplit(url).netloc.casefold()
    for prefix in ("www.", "shop.", "store."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if makers_own_site(host, brand, authority):
        return SOURCE_TRUST["manufacturer"]
    return SOURCE_TRUST["page_validated"]


# Path fragments a shop uses for the pages that carry prices. Not a crawl: these
# only reorder results the search already returned, so a round that may read two
# pages spends them on the two most likely to be priced.
#
# MP-000047 is why. A retail search returned `achedaway.com/`,
# `achedaway.com/blogs/massage-gun` and
# `achedaway.com/collections/achedaway-massage-gun`; the first two sell nothing
# and the third has the prices. Reading in the order returned spends both slots
# before reaching it.
_PRICED_PATHS = ("/products/", "/product/", "/collections/", "/collection/",
                 "/shop/", "/store/", "/p/", "/buy/")
_UNPRICED_PATHS = ("/blogs/", "/blog/", "/news/", "/pages/", "/support/",
                   "/help/", "/about")


def shop_page_first(documents):
    """Reorder retrieved shop pages, likeliest to carry a price first.

    Stable within each group, so the search engine's own ranking still decides
    between two pages of the same kind.
    """
    def rank(document) -> int:
        url = (getattr(document, "url", "") or "").casefold()
        path = url.split("//", 1)[-1]
        path = path[path.find("/"):] if "/" in path else "/"
        if any(fragment in path for fragment in _UNPRICED_PATHS):
            return 2
        if any(fragment in path for fragment in _PRICED_PATHS):
            return 0
        if path in ("/", ""):
            return 2          # a brand's front door is navigation
        return 1

    return sorted(documents, key=rank)


# --- unknown shops, admitted on what the page can prove -----------------------
#
# `recoveryforathletes.com` is the case. It sells the exact Achedaway massage gun
# on MP-000047's table, its page carries a machine-readable price, and it is
# neither the maker's site nor registered -- so under host reputation alone it
# contributes nothing, and the item prices from a single $45 ask.
#
# The trade this makes: page-level proof stands in for host reputation, and the
# resulting evidence is trusted less. An unknown shop must satisfy *every* test
# below, where a maker's own site satisfies none of them, because a maker's
# domain is itself the attribution.

# How far a shop's own statement of its price is believed, by what the source is.
# Multiplied into `anchor_trust` alongside match quality and category quality.
SOURCE_TRUST: dict[str, float] = {
    "manufacturer": 1.00,
    "authorised_retailer": 0.90,
    # Unknown, but the page proved itself. Low enough that a strong marketplace
    # sample still dominates, high enough to beat nothing at all -- which is what
    # such a shop is worth today.
    "page_validated": 0.60,
}

# URL shapes that are not a shop's product page whatever else they carry.
_NOT_A_PRODUCT_PAGE = (
    "/itm/", "/listing/", "/listings/", "/sch/", "/b/", "/usr/",       # marketplaces
    "/coupon", "/deals", "/promo", "/discount",                        # aggregators
    "/review", "/reviews", "/vs-", "/compare", "/blog", "/news",       # commentary
    "/search", "/collections/", "/category/", "/c/",                   # many products
)


@dataclass(frozen=True)
class ValidatedProduct:
    """One product a page proved it sells, at a price it stated in machine form."""

    title: str
    price_cents: int
    currency: str
    brand: str | None
    in_stock: bool | None
    excerpt: str


def validate_product_page(url: str, html: str, brand: str | None) -> tuple[
        ValidatedProduct | None, str]:
    """What an unknown shop must prove before its price counts.

    Every test has to pass, and each answers a different way of being wrong:

      the URL is a single product's page      -- not a grid, a search or a listing
      `schema.org/Product` with an offer      -- the site's own machine-readable claim
      exactly one product on the page         -- so the price is unambiguously attributed
      a price and a currency, both stated     -- no ranges, no "from", no inference
      the brand matches the item's            -- the right maker, at least
      a commerce affordance                   -- somebody can actually buy it here

    Returns the product and why, or None and why not. The reason is returned
    rather than logged so a round can say which test a page failed, which is the
    difference between "that shop had nothing" and "that shop is not a shop".
    """
    import json
    import re

    path = url.split("//", 1)[-1]
    path = path[path.find("/"):].casefold() if "/" in path else "/"
    for shape in _NOT_A_PRODUCT_PAGE:
        if shape in path:
            return None, f"{shape} is not a single product's page"

    blocks = re.findall(
        r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.S
    )
    products = []
    for block in blocks:
        try:
            data = json.loads(block.strip())
        except Exception:  # noqa: BLE001 - malformed JSON-LD is simply no evidence
            continue
        for node in (data if isinstance(data, list) else [data]):
            if isinstance(node, dict) and "Product" in str(node.get("@type", "")):
                products.append(node)
    if not products:
        return None, "no schema.org Product with an offer"

    # Shops routinely emit the same product twice (one per variant or per
    # renderer). Distinct *names* is what matters: two entries for one product is
    # one product, two names is a grid in disguise.
    names = {str(n.get("name", "")).strip().casefold() for n in products}
    if len(names) > 1:
        return None, f"{len(names)} products on one page; attribution too weak"

    node = products[0]
    offers = node.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None, "no usable offer"
    raw_price, currency = offers.get("price"), offers.get("priceCurrency")
    if raw_price is None or not currency:
        return None, "the offer states no price and currency"
    try:
        price_cents = int(round(float(raw_price) * 100))
    except (TypeError, ValueError):
        return None, f"the offer's price {raw_price!r} is not a number"
    if price_cents <= 0:
        return None, "the offer's price is not positive"

    stated_brand = node.get("brand")
    if isinstance(stated_brand, dict):
        stated_brand = stated_brand.get("name")
    if brand:
        folded = "".join(c for c in brand.casefold() if c.isalnum())
        haystack = "".join(
            c for c in f"{stated_brand or ''} {node.get('name', '')}".casefold()
            if c.isalnum()
        )
        if folded and folded not in haystack:
            return None, f"the page's product is not a {brand}"

    if not re.search(r"add[ _-]?to[ _-]?(cart|bag|basket)|buy[ _-]?now", html, re.I):
        return None, "nothing on the page can be bought"

    availability = str(offers.get("availability", ""))
    return ValidatedProduct(
        title=str(node.get("name", "")).strip(),
        price_cents=price_cents,
        currency=str(currency),
        brand=str(stated_brand) if stated_brand else None,
        in_stock=("InStock" in availability) if availability else None,
        excerpt=f"{node.get('name', '')} {raw_price} {currency}".strip(),
    ), "schema.org Product, one product, brand matches, purchasable"
