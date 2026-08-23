"""Turning extracted listing text into comp vocabulary, deterministically.

The extraction stage reads a page and reports what it says. This module decides
what those readings *mean* in the pricing layer's terms -- and it is separate, and
it is code rather than prompt, because two of the decisions carry more weight than
anything else in comp research:

  sold or asking   `realized` means somebody paid it. `asking` means nobody has
                   yet. The whole pricing layer is built on that distinction and
                   an inflated sample of "sold" prices is the most expensive
                   mistake this system can make.
  condition band   what stratifies the sample and sets the comparability ladder.

A model asked "is this sold?" will answer confidently either way. So the answer is
computed from the text it quoted, and it fails toward `asking` and `unknown`, which
are the readings that claim less. `estimate.py` already reports an asking-only
sample as `asking_only` and an unknown band as off-band, so degrading here shows up
downstream as reduced confidence rather than as a silent wrong number.

Pure: no database, no model, no HTTP.
"""

from __future__ import annotations

import re

from resell.pricing.comps import CompBasis, ConditionBand, PriceKind, RetailKind

__all__ = [
    "SOLD_MARKERS", "band_for_declared_condition", "basis_for",
    "price_supported_by", "read_price_kind",
]


# Phrases that evidence a completed sale rather than an offer. Kept narrow and
# checked against the quoted text, never against the model's own summary: "sold"
# appearing in a page's navigation is not evidence that *this* listing sold, so the
# marker has to occur in the excerpt the price came from.
SOLD_MARKERS: tuple[str, ...] = (
    "sold for", "sold on", "sold ", "sold\n", "winning bid", "final price",
    "ended:", "price paid", "hammer price", "realised", "realized",
)

# Declared condition text to the coarse ladder. Ordered: the first phrase found in
# the text wins, so "new without tags" is matched before "new".
# Superseded by `pricing.condition.CONDITION_ALIASES`, which resolves to eBay
# condition ids instead of straight to a band. Kept only so the older tests that
# name it keep working; nothing reads it at runtime.
_CONDITION_PHRASES: tuple[tuple[str, ConditionBand], ...] = (
    ("new with tags", ConditionBand.NEW_WITH_TAGS),
    ("new with box", ConditionBand.NEW_WITH_TAGS),
    ("brand new", ConditionBand.NEW_WITH_TAGS),
    ("new without tags", ConditionBand.NEW_WITHOUT_TAGS),
    ("new without box", ConditionBand.NEW_WITHOUT_TAGS),
    ("new other", ConditionBand.NEW_OTHER),
    ("open box", ConditionBand.NEW_OTHER),
    ("open-box", ConditionBand.NEW_OTHER),
    ("refurbished", ConditionBand.REFURBISHED),
    ("renewed", ConditionBand.REFURBISHED),
    ("like new", ConditionBand.USED_EXCELLENT),
    ("excellent", ConditionBand.USED_EXCELLENT),
    ("very good", ConditionBand.USED_EXCELLENT),
    ("pre-owned", ConditionBand.USED_GOOD),
    ("preowned", ConditionBand.USED_GOOD),
    ("used", ConditionBand.USED_GOOD),
    ("good", ConditionBand.USED_GOOD),
    ("acceptable", ConditionBand.USED_FAIR),
    ("fair", ConditionBand.USED_FAIR),
    ("heavily worn", ConditionBand.USED_FAIR),
    ("for parts", ConditionBand.FOR_PARTS),
    ("not working", ConditionBand.FOR_PARTS),
    ("spares or repair", ConditionBand.FOR_PARTS),
    ("as-is", ConditionBand.FOR_PARTS),
)


def read_price_kind(declared: str, excerpt: str, sale_date_given: bool) -> tuple[PriceKind, str]:
    """Whether this listing's price was paid or merely asked, and why.

    A `realized` reading needs support in the quoted text: a sale-marker phrase, or
    a sale date the extractor also found. Claiming `sold` without either is the one
    error worth being unfair about, because a sample of asking prices dressed as
    sales reads as a firm market and is not one. Everything unsupported becomes
    `asking`, which the estimator already treats as the weaker evidence.
    """
    wants_realized = declared.strip().lower() in ("sold", "realized", "realised", "sold_listing")
    if not wants_realized:
        return PriceKind.ASKING, "read as an asking price"

    haystack = excerpt.casefold()
    marker = next((m for m in SOLD_MARKERS if m in haystack), None)
    if marker:
        return PriceKind.REALIZED, f"the quoted text says {marker.strip()!r}"
    if sale_date_given:
        return PriceKind.REALIZED, "a sale date was read from the listing"
    return PriceKind.ASKING, (
        "recorded as asking: the quoted text shows no sale marker and no sale date, "
        "so nothing supports the claim that this price was paid"
    )


def basis_for(kind: PriceKind, exact: bool) -> CompBasis:
    """The basis a comp carries, from its kind and whether it is the same product.

    `exact` comes from the comparability ceiling, which is itself computed from
    identity resolution -- so an unresolved item cannot produce a `sold_exact` comp
    however well the listing matches.
    """
    if kind is PriceKind.REALIZED:
        return CompBasis.SOLD_EXACT if exact else CompBasis.SOLD_SIMILAR
    if kind is PriceKind.ASKING:
        return CompBasis.ACTIVE_EXACT if exact else CompBasis.ACTIVE_SIMILAR
    return CompBasis.RETAIL_REFERENCE


def band_for_declared_condition(declared: str) -> tuple[ConditionBand, str]:
    """The coarse band a seller's own condition wording implies.

    A stranger's description of their own goods, and treated as such: this reads
    words and makes no attempt to correct optimism. Unrecognised wording is still
    `unknown`, which stratifies as off-band rather than being guessed into the
    middle of the ladder.

    The reading itself now goes through `pricing.condition`, which resolves text to
    an eBay condition id and derives the band from that. Before, this held its own
    flat list of phrases and had to enumerate every wording anyone might use --
    which is how bare "New", the commonest condition string on any marketplace,
    came to be missing while "new with tags" and "brand new" were present. Every
    Canon T6i comp declaring `New` landed at `unknown` and priced nothing.

    Kept as a function because the signature is what the comp loop calls, and
    because "what band is this" is the only question that layer has.
    """
    from resell.pricing.condition import normalise_condition

    match = normalise_condition(declared)
    return match.band, match.why


_DIGITS = re.compile(r"[0-9]+(?:[.,][0-9]+)*")


def price_supported_by(price_cents: int, excerpt: str) -> bool:
    """Whether the quoted text actually contains this figure.

    The excerpt check on the identity side proves a fact was on the page. Here the
    fact *is* a number, so the check has to reach the number itself -- a quotation
    that supports the existence of a price while the price beside it was invented
    would pass a text-only check and poison a distribution.

    Matches on the dollars, with or without the cents, after stripping separators.
    Deliberately loose about formatting and strict about the digits.
    """
    if price_cents < 0:
        return False
    found = {m.group(0).replace(",", "") for m in _DIGITS.finditer(excerpt or "")}
    whole, cents = divmod(price_cents, 100)
    wanted = {f"{whole}.{cents:02d}", str(whole)}
    if cents == 0:
        wanted.add(f"{whole}.0")
    return bool(wanted & found)


def makers_own_site(host: str, brand: str | None, authority) -> bool:
    """Whether this page is the brand selling its own product.

    Autonomous discovery made this matter. When an operator chose the URLs they
    never pasted the manufacturer's store; a search backend goes there constantly,
    and one round recorded 24 prices from `bowflex.com` as asking comps for a used
    Bowflex -- the manufacturer's own retail list, sitting in the sample as though
    somebody were offering second-hand dumbbells at $699.

    The brand answers it without a hand-maintained table: `bowflex.com` for a
    Bowflex, `beatsbydre.com` for Beats. Prefix rather than substring, so "Apple"
    does not claim `pineapple.com`. The authority table still wins where it has an
    opinion, since it also covers stores that do not share the brand's name.
    """
    from resell.reasoning.research import SourceAuthority

    if authority in (SourceAuthority.MANUFACTURER, SourceAuthority.AUTHORISED_RETAILER):
        return True
    if not brand:
        return False
    label = (host or "").split(".")[0].casefold()
    folded = "".join(c for c in brand.casefold() if c.isalnum())
    return bool(label) and bool(folded) and label.startswith(folded)


def retail_from_source(kind: PriceKind, host: str, brand: str | None, authority):
    """Reclassify a maker's own price as retail context. Returns (kind, retail_kind, why).

    A price on the brand's site is what the thing costs new. The estimator already
    keeps retail out of every distribution and reports it as a ceiling check, so
    typing it correctly here is the whole fix -- nothing downstream changes.

    A realized price is left alone. If the extractor found evidence that something
    actually sold, where it sold does not turn that into a list price.
    """
    if kind is PriceKind.REALIZED:
        return kind, None, "a recorded sale stays a sale"
    if not makers_own_site(host, brand, authority):
        return kind, None, "not the maker's own site"
    return (
        PriceKind.REFERENCE,
        RetailKind.CURRENT,
        f"{host} is {brand}'s own site, so this is what it costs new rather than "
        f"what anyone is asking for a used one",
    )
