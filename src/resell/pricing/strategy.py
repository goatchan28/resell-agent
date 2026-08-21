"""Seller strategy: turning one evidence base into several defensible prices.

Pure: no database, no model, no HTTP.

Pricing evidence answers "what does the market show". Strategy answers "what do
you want out of this sale". They are different questions and the second one is
not a property of the comps, so it lives in a different module and is stated
explicitly rather than baked into a single number.

**The rule that keeps this honest: a strategy selects an observed statistic from
an observed distribution. It never computes a new number.** Fast-sale is not
"the median minus fifteen percent"; it is the lowest comparable current ask, a
real listing you can point at. Max-proceeds is not "the median plus a premium";
it is the upper quartile of the comparable asks, again a real position in a real
market. The only arithmetic anywhere in this file is the floor, which is a
constraint rather than an estimate.

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


@dataclass(frozen=True)
class PriceAnchor:
    """An observed point in an observed sample, and where it came from."""

    statistic: Statistic
    price_kind: PriceKind
    band_relation: str
    value_cents: int
    n: int

    def describe(self) -> str:
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
    """Three prices from one comp set. Returns None when the evidence supports none."""
    if rec.unpriceable or rec.basis_distribution is None or rec.price_kind is None:
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
        raw = round(_stat(d, stat) * rec.adjustment_factor)
        anchor = PriceAnchor(
            statistic=stat, price_kind=rec.price_kind,
            band_relation=rec.band_relation, value_cents=raw, n=d.n,
        )
        price = max(raw, floor_price)
        floor_bound = price > raw
        reason = f"{objective}: {anchor.describe()}"
        if strength is not BrandStrength.UNKNOWN:
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
