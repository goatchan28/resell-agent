"""Seller strategy: turning one evidence base into several defensible prices.

Pure: no database, no model, no HTTP.

Pricing evidence answers "what does the market show". Strategy answers "what do
you want out of this sale". They are different questions and the second one is
not a property of the comps, so it lives in a different module and is stated
explicitly rather than baked into a single number.

**The rule that keeps this honest: a strategy takes a position in the evidence
the estimator produced, and names which evidence that was. It never invents a
number of its own.** Where there is a marketplace sample, fast-sale is not "the
median minus fifteen percent" -- it is the lowest comparable current ask, a real
listing you can point at, and max-proceeds is the upper quartile of those asks,
again a real position in a real market.

That rule used to read "an observed statistic from an observed distribution",
which was the same discipline stated one layer too specifically. It made the
retail-derived anchor unusable: an item with no comparable listings and a known
current shop price produced a perfectly defensible band in `estimate` and no
strategies at all, so the seller was sent to type a number with strictly less
information than the agent had. The evidence model now has two shapes -- an
observed distribution and a computed anchor -- and the rule is about *not
inventing*, which both satisfy. An anchor band is arithmetic, but it is
`estimate`'s arithmetic, done once, stated in `RetailAnchor.basis`, and taken
here rather than redone.

The two never merge. An anchor is not turned into a fake distribution and its
positions are not described as comps; `PriceAnchor.source` says which of the two
a price came from, and the seller-facing note says it in words. The only
arithmetic in this file remains the floor, which is a constraint rather than an
estimate.

Brand strength participates the same way -- it selects *which* statistic an
objective takes, and it must be cited or it is treated as unknown. It never
multiplies anything.

Starting high is cheap precisely because repricing is first-class: a price that
does not move can be moved, and the price history records that it was. Starting
low is not reversible in the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .comps import PriceKind
from .estimate import Distribution, PriceQualifier, PriceRecommendation
from .proceeds import (
    PROVISIONAL_DEFAULT,
    CostLines,
    FeeSchedule,
    gross_from_net,
    net_from_gross,
)


class SellerObjective(StrEnum):
    FAST_SALE = "fast_sale"
    BALANCED = "balanced"
    MAX_PROCEEDS = "max_proceeds"


class BrandStrength(StrEnum):
    UNKNOWN = "unknown"
    COMMODITY = "commodity"
    ESTABLISHED = "established"
    PREMIUM = "premium"


@dataclass(frozen=True)
class BrandSignal:
    """A categorical market signal, evidence-backed like everything else.

    Uncited brand strength is treated as unknown. This is the same discipline as
    aspects: an assertion nobody can trace is not evidence, and "it's a good
    brand" is exactly the kind of claim that would otherwise quietly add fifteen
    percent to a price.

    A retail reference is legitimate evidence here -- MP-000003's $398 swing tag
    says something real about where the line is positioned -- and using it this
    way keeps retail categorical rather than turning it into a formula.
    """

    strength: BrandStrength = BrandStrength.UNKNOWN
    citations: tuple[str, ...] = ()
    rationale: str = ""

    @property
    def effective(self) -> BrandStrength:
        return self.strength if self.citations else BrandStrength.UNKNOWN

    @property
    def is_uncited(self) -> bool:
        return self.strength is not BrandStrength.UNKNOWN and not self.citations


class Statistic(StrEnum):
    MIN = "min"
    P25 = "p25"
    MEDIAN = "median"
    P75 = "p75"
    MAX = "max"


# Which observed statistic each objective takes, by brand strength. Every cell
# is a position in a real sample; none of them is a computed adjustment.
_SELECTION: dict[tuple[SellerObjective, BrandStrength], Statistic] = {
    (SellerObjective.FAST_SALE, BrandStrength.UNKNOWN): Statistic.MIN,
    (SellerObjective.FAST_SALE, BrandStrength.COMMODITY): Statistic.MIN,
    (SellerObjective.FAST_SALE, BrandStrength.ESTABLISHED): Statistic.P25,
    (SellerObjective.FAST_SALE, BrandStrength.PREMIUM): Statistic.P25,
    (SellerObjective.BALANCED, BrandStrength.UNKNOWN): Statistic.MEDIAN,
    (SellerObjective.BALANCED, BrandStrength.COMMODITY): Statistic.MEDIAN,
    (SellerObjective.BALANCED, BrandStrength.ESTABLISHED): Statistic.MEDIAN,
    (SellerObjective.BALANCED, BrandStrength.PREMIUM): Statistic.MEDIAN,
    (SellerObjective.MAX_PROCEEDS, BrandStrength.UNKNOWN): Statistic.P75,
    (SellerObjective.MAX_PROCEEDS, BrandStrength.COMMODITY): Statistic.P75,
    (SellerObjective.MAX_PROCEEDS, BrandStrength.ESTABLISHED): Statistic.P75,
    (SellerObjective.MAX_PROCEEDS, BrandStrength.PREMIUM): Statistic.MAX,
}

_TRADEOFF = {
    SellerObjective.FAST_SALE: "quickest turn; gives up the top of the range",
    SellerObjective.BALANCED: "mid-market; sells at a normal pace for the category",
    SellerObjective.MAX_PROCEEDS: "highest proceeds if it sells; expect to reprice down",
}


def _stat(d: Distribution, s: Statistic) -> int:
    """On thin samples the quartiles are theatre, so they collapse to the extremes."""
    if d.n < 4:
        if s is Statistic.P25:
            s = Statistic.MIN
        elif s is Statistic.P75:
            s = Statistic.MAX
    return {
        Statistic.MIN: d.min_cents,
        Statistic.P25: d.p25_cents,
        Statistic.MEDIAN: d.median_cents,
        Statistic.P75: d.p75_cents,
        Statistic.MAX: d.max_cents,
    }[s]


# Where in the anchor band a position sits, said in words rather than in sample
# statistics. See `PriceAnchor.describe`.
_ANCHOR_WORD: dict["Statistic", str] = {}


class AnchorSource(StrEnum):
    """Which kind of evidence a strategy took its position in.

    Never inferred from the numbers. A caller reading a price has to be able to
    tell "the lowest of nine real asks" from "12% under a depreciated shop
    price" without knowing anything about how either was produced.
    """

    MARKETPLACE = "marketplace"
    RETAIL_ANCHOR = "retail anchor"


@dataclass(frozen=True)
class PriceAnchor:
    """Where a strategy's number came from, and what kind of thing it is.

    Two shapes, kept apart. A marketplace position names a statistic, a price
    kind and a sample size. A retail-anchored position has none of those -- there
    is no sample -- and carries the anchor's own account of itself instead.
    """

    statistic: Statistic
    price_kind: PriceKind | None
    band_relation: str
    value_cents: int
    n: int
    source: AnchorSource = AnchorSource.MARKETPLACE
    # `RetailAnchor.basis`, verbatim, when there is one. Restating the retention
    # arithmetic here would be a second copy of it.
    basis: str = ""

    @property
    def is_observed(self) -> bool:
        return self.source is AnchorSource.MARKETPLACE

    def describe(self) -> str:
        if self.source is AnchorSource.RETAIL_ANCHOR:
            # Not "p75 of the anchor". `min`/`p75` are sample vocabulary and
            # there is no sample; saying it would imply a distribution that does
            # not exist behind a number that is one shop price and a rate.
            return (
                f"{_ANCHOR_WORD[self.statistic]} of the retail-derived anchor, "
                f"{self.basis}"
            )
        return (
            f"{self.statistic} of {self.n} {self.price_kind} comps "
            f"({self.band_relation})"
        )


@dataclass(frozen=True)
class StrategyPrice:
    objective: SellerObjective
    price_cents: int
    anchor: PriceAnchor
    net_proceeds_cents: int
    floor_bound: bool
    selection_reason: str
    tradeoff: str


@dataclass(frozen=True)
class StrategySet:
    sku: str
    prices: dict[SellerObjective, StrategyPrice]
    default_objective: SellerObjective
    brand: BrandSignal
    sold_evidence_note: str = ""
    uncertainty_note: str = ""
    notes: tuple[str, ...] = ()

    def get(self, objective: SellerObjective) -> StrategyPrice:
        return self.prices[objective]


def build_strategies(
    rec: PriceRecommendation,
    *,
    brand: BrandSignal | None = None,
    schedule: FeeSchedule | None = None,
    costs: CostLines | None = None,
    minimum_net_proceeds_cents: int = 500,
    default_objective: SellerObjective = SellerObjective.BALANCED,
) -> StrategySet | None:
    """Three prices from the best evidence there is.

    The hierarchy, in order, and each rung is tried only because the one above it
    is empty:

      a marketplace sample, on its own;
      a marketplace sample the estimator has already blended with the anchor;
      the retail-derived anchor alone, when no comparable listing survived
      judging but a current shop price for this product did;
      and None -- which is the seller typing a number, and is now reached only
      when there is genuinely nothing.

    Returns None when the evidence supports none of them.
    """
    if rec.unpriceable:
        return None
    anchor_only = rec.basis_distribution is None or rec.price_kind is None
    if anchor_only and rec.retail_anchor is None:
        return None

    sig = brand or BrandSignal()
    strength = sig.effective
    d = rec.basis_distribution
    sched = schedule or PROVISIONAL_DEFAULT
    c = costs or CostLines()
    floor_price = gross_from_net(
        minimum_net_proceeds_cents, schedule=sched, costs=c
    )

    prices: dict[SellerObjective, StrategyPrice] = {}
    for objective in SellerObjective:
        stat = _SELECTION[(objective, strength)]
        if anchor_only:
            raw, anchor = _from_anchor(rec, objective, stat)
        else:
            raw = round(_stat(d, stat) * rec.adjustment_factor)
            anchor = PriceAnchor(
                statistic=stat, price_kind=rec.price_kind,
                band_relation=rec.band_relation, value_cents=raw, n=d.n,
            )
        price = max(raw, floor_price)
        floor_bound = price > raw
        reason = f"{objective}: {anchor.describe()}"
        if strength is not BrandStrength.UNKNOWN and not anchor_only:
            # Brand strength selects *which* statistic of a sample to take. With
            # no sample it selects nothing, and saying it did would dress up a
            # depreciation rate as a market judgement.
            reason += f", brand strength {strength}"
        if floor_bound:
            reason += "; raised to the minimum net proceeds floor"
        prices[objective] = StrategyPrice(
            objective=objective,
            price_cents=price,
            anchor=anchor,
            net_proceeds_cents=net_from_gross(
                price, schedule=sched, costs=c
            ).net_cents,
            floor_bound=floor_bound,
            selection_reason=reason,
            tradeoff=_TRADEOFF[objective],
        )

    return StrategySet(
        sku=rec.sku,
        prices=prices,
        default_objective=default_objective,
        brand=sig,
        sold_evidence_note=_sold_note(rec),
        uncertainty_note=_uncertainty_note(rec),
        notes=_notes(rec, sig),
    )


# Where each objective sits in the anchor band. The band already expresses the
# uncertainty in a retail-derived figure -- `ANCHOR_SPREAD`, either side of the
# point -- so the three objectives take its bottom, middle and top rather than
# applying a second discount of their own.
#
# Brand strength is deliberately not consulted. It selects which statistic of a
# *sample* to take, and there is no sample here; a strong brand's standing is
# already in the shop price the anchor depreciates.
_ANCHOR_POSITION: dict[Statistic, str] = {
    Statistic.MIN: "low_cents",
    Statistic.P25: "low_cents",
    Statistic.MEDIAN: "point_cents",
    Statistic.P75: "high_cents",
    Statistic.MAX: "high_cents",
}

_ANCHOR_WORD.update({
    Statistic.MIN: "the bottom", Statistic.P25: "the bottom",
    Statistic.MEDIAN: "the middle",
    Statistic.P75: "the top", Statistic.MAX: "the top",
})


def _from_anchor(
    rec: PriceRecommendation, objective: SellerObjective, stat: Statistic
) -> tuple[int, PriceAnchor]:
    """A position in the retail-derived anchor band, labelled as one.

    `adjustment_factor` is not applied. Condition is already in the anchor --
    `retention_for` scales the whole table by it -- and applying a condition
    adjustment on top would count the same fact twice.
    """
    band = rec.retail_anchor
    raw = getattr(band, _ANCHOR_POSITION[stat])
    return raw, PriceAnchor(
        statistic=stat, price_kind=None, band_relation="retail_anchored",
        value_cents=raw, n=0, source=AnchorSource.RETAIL_ANCHOR, basis=band.basis,
    )


def _sold_note(rec: PriceRecommendation) -> str:
    """What the realized evidence says, when it did not set the number."""
    if rec.band_relation != "condition_matched_asks" or rec.realized_off_band is None:
        return ""
    d = rec.realized_off_band
    steps = f"{rec.off_band_steps} ladder steps away" if rec.off_band_steps else "a different condition"
    direction = ""
    if rec.band_central_cents is not None:
        if d.median_cents < rec.band_central_cents:
            direction = " below the asking position"
        elif d.median_cents > rec.band_central_cents:
            direction = " above the asking position, which suggests the current asks may be low"
    return (
        f"{d.n} realized comp(s) at {steps}, median ${d.median_cents / 100:.2f}"
        f"{direction}. Retained as evidence; no adjustment applied, because the "
        f"gap between those objects and this one is not a number anyone here can "
        f"defend."
    )


def _uncertainty_note(rec: PriceRecommendation) -> str:
    parts: list[str] = []
    if rec.has(PriceQualifier.RETAIL_ANCHORED):
        # First, because it is the most limiting thing true of the price: there
        # is no marketplace evidence under it at all.
        parts.append(
            "no comparable listing survived judging, so the band is reasoned "
            "down from the current shop price rather than observed"
        )
    if rec.has(PriceQualifier.POSITIONED_ON_ASKS):
        parts.append(
            "priced against current asking prices, not realized sales, because "
            "the realized comps are a materially different condition"
        )
    if rec.has(PriceQualifier.ASKING_ONLY):
        parts.append("no realized sales in evidence at all")
    if rec.has(PriceQualifier.SINGLE_COMP):
        parts.append("the band rests on one comp")
    elif rec.has(PriceQualifier.THIN_SAMPLE):
        parts.append("the sample is thin")
    if rec.has(PriceQualifier.WIDE_DISPERSION):
        parts.append("the sample is widely dispersed, so the band is genuinely uncertain")
    if rec.has(PriceQualifier.IDENTITY_UNRESOLVED):
        parts.append("the product was never resolved, so no comp is an exact match")
    if rec.has(PriceQualifier.SOLD_EVIDENCE_DIVERGES):
        parts.append("the realized evidence sits outside the band")
    return "; ".join(parts)


def _notes(rec: PriceRecommendation, sig: BrandSignal) -> tuple[str, ...]:
    out: list[str] = []
    if sig.is_uncited:
        out.append(
            f"brand strength {sig.strength} was asserted without a citation and "
            "has been treated as unknown"
        )
    if rec.sample_exclusions:
        out.append(
            f"{len(rec.sample_exclusions)} ask(s) excluded from the sample: "
            + "; ".join(rec.sample_exclusions)
        )
    if rec.has(PriceQualifier.ADJACENT_CONDITION_POOLED):
        out.append("adjacent condition bands were pooled without adjustment")
    if rec.has(PriceQualifier.LONG_DAYS_ON_MARKET) and rec.price_kind is PriceKind.ASKING:
        out.append(
            "the ask sample includes listings that have sat past the window, so "
            "the top of the range may be a price the market has already declined; "
            "max_proceeds is the objective most exposed to this"
        )
    return tuple(out)
