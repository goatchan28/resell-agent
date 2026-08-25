"""How much of its shop price a thing keeps when it is resold.

The gap this fills, from MP-000041. An XD Design Bobby Hero backpack, new with
tags, listed at **$75** on the strength of one eBay listing for a Bobby
*Original* -- a cheaper sub-line, a different colour, condition unknown, asking
rather than sold. The item's own record already held the shop price for this
exact model, twice over: $139.00 and "starts from $137.65". Both were on disk
and neither could touch the price, because a retail figure is barred from every
distribution in `estimate` and there was nothing else it was allowed to be.

That bar is right and stays. A shop price is not a comparable sale and pooling
the two would corrupt the sample. What was missing is the other thing a person
does with it: a new-in-box item worth $139 new is worth something like two
thirds of that second-hand, and the fraction depends on what kind of thing it is.
A phone and a wool coat and a Lego set do not decay alike.

So this module is one table and one function. It produces a *separate*, clearly
labelled anchor -- never a comp, never inside a distribution -- and the estimator
weighs it alongside marketplace evidence rather than instead of it.

The numbers are stated rather than learned, and stated here rather than spread
through the pricing code, so that changing an opinion about how backpacks hold
their value is a one-line diff with a name on it. They are informed defaults and
nothing more; when there are enough realized sales to measure retention per
category, measurement should replace them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .comps import ConditionBand

# eBay's own top-level groupings, which is why the category path is captured.
# Keyed on the first segment so an unseen leaf still lands somewhere sensible.
#
# Each entry is the fraction of *current* shop price a resale in mint,
# still-sealed condition would be expected to fetch. Condition scales it down
# from there. Sources are judgement and public resale-market rules of thumb, not
# measurement -- which is exactly why they are visible and named.
BASE_RETENTION: dict[str, float] = {
    # Depreciates hard and fast: superseded by a new model on a known cadence,
    # and buyers price accordingly.
    "Cell Phones & Accessories": 0.55,
    "Consumer Electronics": 0.55,
    "Computers/Tablets & Networking": 0.55,
    "Cameras & Photo": 0.60,
    "Video Games & Consoles": 0.60,
    # Holds value: no model cycle, condition is most of the story.
    "Clothing, Shoes & Accessories": 0.65,
    "Jewelry & Watches": 0.70,
    "Musical Instruments & Gear": 0.70,
    "Sporting Goods": 0.60,
    "Home & Garden": 0.60,
    "Baby": 0.55,
    "Toys & Hobbies": 0.65,
    # Where a shop price is the least informative: what a collector pays has
    # little to do with what a shop charged, so the anchor is weak and says so.
    "Collectibles": 0.60,
    "Art": 0.60,
    "Antiques": 0.60,
    # Bulky, costly to ship, thin second-hand demand.
    "Furniture": 0.45,
    "Business & Industrial": 0.50,
}

# What an unrecognised or missing category gets. Deliberately middling and
# deliberately not silent -- `RetailAnchor.basis` says the default was used.
DEFAULT_RETENTION = 0.60

# Multipliers on the base, by condition. The ladder is already ordered and
# already knows two steps is a material difference; this is the same ordering
# expressed as money.
CONDITION_FACTOR: dict[ConditionBand, float] = {
    ConditionBand.NEW_WITH_TAGS: 1.00,
    ConditionBand.NEW_WITHOUT_TAGS: 0.95,
    ConditionBand.NEW_OTHER: 0.90,
    ConditionBand.REFURBISHED: 0.80,
    ConditionBand.USED_EXCELLENT: 0.75,
    ConditionBand.USED_GOOD: 0.62,
    ConditionBand.USED_FAIR: 0.45,
    ConditionBand.FOR_PARTS: 0.20,
}

# An unknown condition is not a middling one. Assuming "probably fine" of an item
# nobody has graded is how a listing ends up priced as new and arrives used, so
# an ungraded item is anchored near the bottom of the used range and the
# uncertainty is reported rather than averaged away.
UNKNOWN_CONDITION_FACTOR = 0.55

# The width of the anchor, either side of the point estimate. A retail-derived
# figure is an inference and should never present as a precise one.
ANCHOR_SPREAD = 0.12


@dataclass(frozen=True)
class RetailAnchor:
    """A resale value inferred from a shop price. Never a comparable sale."""

    low_cents: int
    point_cents: int
    high_cents: int
    retail_cents: int
    retention: float
    category: str
    condition: ConditionBand
    basis: str

    @property
    def is_default_category(self) -> bool:
        return self.category == ""


def top_level(category_path: str | None) -> str:
    """eBay's first path segment, which is the grouping the table is keyed on."""
    if not category_path:
        return ""
    return category_path.split(">")[0].strip()


def retention_for(category_path: str | None, condition: ConditionBand) -> float:
    base = BASE_RETENTION.get(top_level(category_path), DEFAULT_RETENTION)
    factor = CONDITION_FACTOR.get(condition, UNKNOWN_CONDITION_FACTOR)
    return base * factor


def anchor_from_retail(
    retail_cents: int, category_path: str | None, condition: ConditionBand
) -> RetailAnchor:
    """A band, not a number, because it is inferred and not observed."""
    retention = retention_for(category_path, condition)
    point = round(retail_cents * retention)
    group = top_level(category_path)
    known = group in BASE_RETENTION
    return RetailAnchor(
        low_cents=round(point * (1 - ANCHOR_SPREAD)),
        point_cents=point,
        high_cents=round(point * (1 + ANCHOR_SPREAD)),
        retail_cents=retail_cents,
        retention=retention,
        category=group if known else "",
        condition=condition,
        basis=(
            f"{retention:.0%} of the {_money(retail_cents)} shop price"
            + (f" for {group}" if known else " (no category match; default rate)")
            + f", {condition.name.lower().replace('_', ' ')}"
        ),
    )


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"
