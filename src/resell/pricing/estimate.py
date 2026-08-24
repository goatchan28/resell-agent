"""Distributions, qualifiers, and the price recommendation.

Pure: no database, no model, no HTTP.

The output is a **band plus a statement of what it rests on**, not a number. A
scalar confidence is computed for diagnostics and nothing branches on it; the
qualifiers are what carry meaning, and they are machine-readable so the review
view and the approval gate read the same thing.

Two structural refusals live here:

  reference prices are rejected at the boundary of every distribution function,
  so "40% of retail" cannot be written by accident;

  realized and asking samples are summarised separately and never pooled.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from .comps import (
    AdjustmentSource,
    CompBasis,
    CompClaim,
    CompObservation,
    Comparability,
    ComparisonBasis,
    ConditionAdjustment,
    ConditionBand,
    PriceKind,
    RetailKind,
    ladder_steps,
    refuse_cross_kind,
    validate_adjustment,
)

DEFAULT_WINDOW_DAYS = 90
WIDE_DISPERSION_IQR_RATIO = 0.50
WIDE_DISPERSION_RANGE_RATIO = 0.80
THIN_SAMPLE_N = 3

# One ladder step is not a material difference -- new-without-tags and
# new-with-tags are the same market. Two or more is, and so is any comparison
# involving an unknown band, because "unknown" is not a position on the ladder.
MATERIAL_CONDITION_STEPS = 2


def condition_comparable(a: ConditionBand, b: ConditionBand) -> bool:
    steps = ladder_steps(a, b)
    return steps is not None and steps < MATERIAL_CONDITION_STEPS


class PriceQualifier(StrEnum):
    """Named, machine-readable, and the honest replacement for a confidence score."""

    SINGLE_COMP = "single_comp"
    THIN_SAMPLE = "thin_sample"
    WIDE_DISPERSION = "wide_dispersion"
    ASKING_ONLY = "asking_only"
    POSITIONED_ON_ASKS = "positioned_on_asks"
    SOLD_EVIDENCE_OUT_OF_BAND = "sold_evidence_out_of_band"
    SOLD_EVIDENCE_DIVERGES = "sold_evidence_diverges"
    ADJACENT_CONDITION_POOLED = "adjacent_condition_pooled"
    STALE_ASKS_EXCLUDED = "stale_asks_excluded"
    NO_SAME_PRODUCT_COMPS = "no_same_product_comps"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    STALE_COMPS = "stale_comps"
    CONDITION_MISMATCH = "condition_mismatch"
    CONDITION_UNKNOWN_IN_SAMPLE = "condition_unknown_in_sample"
    # The band itself came from asks whose condition nobody stated -- typically a
    # search index, which returns a price and no condition. Distinct from
    # `condition_mismatch`, which asserts a difference was observed. Nothing was
    # observed here, and a band built on that must not claim otherwise.
    ASKING_UNKNOWN_CONDITION = "asking_unknown_condition"
    SHIPPING_UNKNOWN = "shipping_unknown"
    MIXED_COMPARISON_BASIS = "mixed_comparison_basis"
    RETAIL_ONLY = "retail_only"
    # No comparable evidence at all: the number is the operator's judgement. The
    # loudest qualifier there is, and the reason a manual price still goes through
    # the proposal seam rather than around it -- the price is theirs to set, and
    # the record has to say the market never supported it.
    OPERATOR_JUDGEMENT = "operator_judgement"
    # A thin condition-matched pool widened with marketplace asks whose condition
    # nobody stated. Both are asking prices from marketplaces -- no price kind was
    # crossed -- and the alternative was letting one observation speak over twelve.
    POOLED_UNKNOWN_CONDITION = "pooled_unknown_condition"
    ABOVE_RETAIL_CEILING = "above_retail_ceiling"
    ADJUSTED = "adjusted"
    LONG_DAYS_ON_MARKET = "long_days_on_market"


# --- distribution ------------------------------------------------------------


def _percentile(sorted_values: list[int], q: float) -> float:
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


@dataclass(frozen=True)
class Distribution:
    """Median-based throughout, so one £190 outlier does not move the centre."""

    kind: PriceKind
    n: int
    min_cents: int
    p25_cents: int
    median_cents: int
    p75_cents: int
    max_cents: int
    mad_cents: int

    @property
    def iqr_cents(self) -> int:
        return self.p75_cents - self.p25_cents

    @property
    def range_cents(self) -> int:
        return self.max_cents - self.min_cents

    @property
    def is_widely_dispersed(self) -> bool:
        if self.median_cents <= 0:
            return False
        if self.n >= 4:
            return self.iqr_cents / self.median_cents > WIDE_DISPERSION_IQR_RATIO
        return self.range_cents / self.median_cents > WIDE_DISPERSION_RANGE_RATIO


def summarize(prices: list[int], kind: PriceKind) -> Distribution:
    """Summarise one sample of one kind.

    Rejects `reference` by type. Retail is context, and the way to keep it context
    is to make it impossible to feed into the arithmetic.
    """
    if kind is PriceKind.REFERENCE:
        raise ValueError(
            "retail reference prices are context only and must not enter a "
            "distribution; use retail_ceiling_check instead"
        )
    if not prices:
        raise ValueError("cannot summarise an empty sample")
    s = sorted(prices)
    median = statistics.median(s)
    mad = statistics.median([abs(p - median) for p in s])
    return Distribution(
        kind=kind,
        n=len(s),
        min_cents=s[0],
        p25_cents=round(_percentile(s, 0.25)),
        median_cents=round(median),
        p75_cents=round(_percentile(s, 0.75)),
        max_cents=s[-1],
        mad_cents=round(mad),
    )


# --- inputs ------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredComp:
    """A claim paired with the observation it points at."""

    claim: CompClaim
    observation: CompObservation


@dataclass(frozen=True)
class RetailReference:
    """Never enters a distribution. Ceiling check and operator context only."""

    price_cents: int
    kind: RetailKind
    as_of: datetime | None = None
    source: str | None = None
    citation: str | None = None


class EvidenceRole(StrEnum):
    """What a piece of evidence was allowed to do.

    Named rather than inferred from the output, because the interesting cases are
    the ones where evidence was present and did nothing: a retail price that is
    context, a sold comp in the wrong condition that was retained and not applied,
    an ask that has sat too long. Those all look identical to "absent" in a band,
    and they are not the same thing at all.
    """

    SET_THE_BAND = "set the band"
    RETAINED_NOT_APPLIED = "retained, not applied"
    CONTEXT_ONLY = "context only"
    CEILING_CHECK = "ceiling check"
    DEMAND_ONLY = "demand signal only"
    EXCLUDED = "excluded"
    RECLASSIFIED = "reclassified"


@dataclass(frozen=True)
class EvidenceContribution:
    """One line of the account of how the number was reached."""

    source: str
    n: int
    role: EvidenceRole
    detail: str
    low_cents: int | None = None
    high_cents: int | None = None
    # The middle of the pool, and where the observations came from. Both were
    # computed and then thrown away: a line reading "n=12, $50-$95" says less than
    # it knows, and "search index" against "fetched page" is the difference
    # between a third party's summary and bytes we loaded.
    median_cents: int | None = None
    origins: tuple[str, ...] = ()

    @property
    def contributed(self) -> bool:
        return self.role is EvidenceRole.SET_THE_BAND


@dataclass(frozen=True)
class DemandSignal:
    """How long things sit, modelled apart from what they cost.

    Deliberately not folded into the band. Days on market says something about
    liquidity, not about value, and averaging it into a price would be mixing two
    quantities the way pooling asks with sales would. It informs which strategy an
    operator picks; it moves no number on its own.

    Only asks carry it usefully. A sold comp's days-on-market is the time that sale
    took, which is a fact about a completed transaction rather than about the
    market as it stands.
    """

    n_asks: int = 0
    n_with_days: int = 0
    median_days: int | None = None
    max_days: int | None = None
    n_beyond_window: int = 0
    window_days: int = DEFAULT_WINDOW_DAYS

    @property
    def measured(self) -> bool:
        return self.n_with_days > 0

    def describe(self) -> str:
        if not self.measured:
            return (
                f"no days-on-market recorded on {self.n_asks} ask(s); liquidity is "
                f"unmeasured, which is not the same as fast"
            )
        parts = [
            f"{self.n_with_days} of {self.n_asks} ask(s) report days on market, "
            f"median {self.median_days}d"
        ]
        if self.n_beyond_window:
            parts.append(
                f"{self.n_beyond_window} beyond the {self.window_days}-day window, "
                f"which is evidence the asking price is wrong rather than evidence "
                f"of what the market pays"
            )
        return "; ".join(parts)


def measure_demand(asking, window_days: int) -> DemandSignal:
    """Days-on-market across the asking pool. No prices touched."""
    days = [
        c.observation.days_on_market for c in asking
        if c.observation.days_on_market is not None
    ]
    if not days:
        return DemandSignal(n_asks=len(asking), window_days=window_days)
    ordered = sorted(days)
    middle = len(ordered) // 2
    median = (
        ordered[middle] if len(ordered) % 2
        else round((ordered[middle - 1] + ordered[middle]) / 2)
    )
    return DemandSignal(
        n_asks=len(asking),
        n_with_days=len(days),
        median_days=median,
        max_days=ordered[-1],
        n_beyond_window=sum(1 for d in days if d > window_days),
        window_days=window_days,
    )


@dataclass(frozen=True)
class PricingInput:
    sku: str
    item_condition_band: ConditionBand
    identity_resolution: str
    comps: tuple[ScoredComp, ...] = ()
    retail: tuple[RetailReference, ...] = ()
    adjustments: tuple[ConditionAdjustment, ...] = ()
    window_days: int = DEFAULT_WINDOW_DAYS
    now: datetime | None = None

    def as_of(self) -> datetime:
        return self.now or datetime.now(timezone.utc)


# --- output ------------------------------------------------------------------


@dataclass(frozen=True)
class PriceRecommendation:
    sku: str
    unpriceable: bool
    reason: str
    basis: CompBasis | None = None
    price_kind: PriceKind | None = None
    band_low_cents: int | None = None
    band_central_cents: int | None = None
    band_high_cents: int | None = None
    realized: Distribution | None = None
    asking: Distribution | None = None
    # Stratified by condition as well as by kind, because the strategy layer
    # needs to know *which* sample a number came from, not just its kind.
    realized_comparable: Distribution | None = None
    realized_off_band: Distribution | None = None
    asking_comparable: Distribution | None = None
    asking_off_band: Distribution | None = None
    # The pool the band actually came from, handed to the strategy layer so its
    # anchors and the printed distribution can never disagree.
    basis_distribution: Distribution | None = None
    band_relation: str = "none"
    off_band_steps: int | None = None
    adjustment_factor: float = 1.0
    sample_exclusions: tuple[str, ...] = ()
    n_included: int = 0
    n_excluded: int = 0
    # How many comps actually produced the band. Not the same as n_included:
    # a set can include five comps and centre on the one realized in-band comp,
    # and reporting the larger number would overstate the evidence.
    n_in_basis: int = 0
    comparability_profile: dict[str, int] = field(default_factory=dict)
    identity_ceiling: Comparability = Comparability.SAME_FAMILY_VARIANT
    adjustments: tuple[ConditionAdjustment, ...] = ()
    qualifiers: tuple[PriceQualifier, ...] = ()
    retail_context: tuple[RetailReference, ...] = ()
    diagnostic_confidence: float = 0.0
    # The account of how the number was reached, including the evidence that was
    # present and did nothing.
    contributions: tuple[EvidenceContribution, ...] = ()
    demand: DemandSignal | None = None

    def has(self, q: PriceQualifier) -> bool:
        return q in self.qualifiers

    def describe(self) -> str:
        """Operator-facing one-liner. Never says "market price" for asking data."""
        if self.unpriceable:
            return f"{self.sku}: unpriceable -- {self.reason}"
        kind = self.price_kind
        assert kind is not None and self.band_central_cents is not None
        n = self.n_in_basis
        noun = "comp" if n == 1 else "comps"
        of = f" of {self.n_included}" if self.n_included != n else ""
        return (
            f"{self.sku}: {n}{of} {noun} {kind.verb} "
            f"{_money(self.band_low_cents)}-{_money(self.band_high_cents)}, "
            f"centre {_money(self.band_central_cents)} "
            f"[{self.basis}]"
            + (f" ({', '.join(self.qualifiers)})" if self.qualifiers else "")
        )


def _money(cents: int | None) -> str:
    if cents is None:
        return "n/a"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"


# --- language gate -----------------------------------------------------------

_MARKET_CLAIMS = ("market price", "market value", "worth", "sells for", "going rate")


def check_price_language(text: str, kind: PriceKind) -> tuple[bool, str]:
    """Same discipline as the listing-draft prohibited-terms checker.

    An asking distribution may not be described as what something sells for. This
    is the "preserve the distinction everywhere" rule made mechanical, because in
    prose it is exactly the distinction that erodes first.
    """
    if kind is PriceKind.REALIZED:
        return True, "realized prices may be described as sale prices"
    lowered = text.lower()
    hits = [p for p in _MARKET_CLAIMS if p in lowered]
    if hits:
        return False, (
            f"{kind} prices cannot be described with {hits!r}; "
            f"use {kind.verb!r}"
        )
    return True, "no market claims over non-realized prices"


# --- retail as context -------------------------------------------------------


def retail_ceiling_check(
    price_cents: int, retail: tuple[RetailReference, ...]
) -> tuple[bool, str]:
    """Only `retail_current` is a real ceiling.

    Original retail is a marketing number of unknown date -- MP-000003's $398 swing
    tag -- and says nothing about what the item can be bought new for today. A
    breach is a flag, not a refusal: discontinued and collectible items exceed
    retail legitimately, and that is the operator's call to make explicitly.
    """
    current = [r for r in retail if r.kind is RetailKind.CURRENT]
    if not current:
        return True, "no current retail reference to check against"
    lowest = min(r.price_cents for r in current)
    if price_cents > lowest:
        return False, (
            f"{_money(price_cents)} exceeds current retail {_money(lowest)}; "
            "legitimate only if discontinued or collectible, and that needs saying"
        )
    return True, f"below current retail {_money(lowest)}"


# --- the estimator -----------------------------------------------------------


def recommend(inp: PricingInput) -> PriceRecommendation:
    """Compute a band and its qualifiers. Deterministic; the model proposes into it."""
    now = inp.as_of()
    quals: set[PriceQualifier] = set()

    included = [c for c in inp.comps if c.claim.contributes]
    excluded = [c for c in inp.comps if not c.claim.contributes]

    profile: dict[str, int] = {}
    for c in inp.comps:
        key = str(c.claim.comparability)
        profile[key] = profile.get(key, 0) + 1

    ceiling = _ceiling(inp.identity_resolution)
    if inp.identity_resolution != "resolved":
        quals.add(PriceQualifier.IDENTITY_UNRESOLVED)
    if not any(
        c.claim.comparability is Comparability.SAME_PRODUCT for c in included
    ):
        quals.add(PriceQualifier.NO_SAME_PRODUCT_COMPS)

    # Retail never joins the sample. It used to be filtered out silently, which
    # made a retail price recorded as a comp indistinguishable from one nobody
    # recorded -- so an operator who supplied it saw no trace of it anywhere.
    # It is reclassified as context, and the reclassification is reported.
    reference_comps = [
        c for c in included if c.observation.price_kind is PriceKind.REFERENCE
    ]
    included = [c for c in included if c.observation.price_kind is not PriceKind.REFERENCE]

    realized = [c for c in included if c.observation.price_kind is PriceKind.REALIZED]
    asking = [c for c in included if c.observation.price_kind is PriceKind.ASKING]

    if any(not c.observation.shipping_known for c in included):
        quals.add(PriceQualifier.SHIPPING_UNKNOWN)
    bases = {c.observation.comparison_basis for c in included}
    if len(bases) > 1:
        quals.add(PriceQualifier.MIXED_COMPARISON_BASIS)
    if any(c.observation.condition_band is ConditionBand.UNKNOWN for c in included):
        quals.add(PriceQualifier.CONDITION_UNKNOWN_IN_SAMPLE)

    cutoff = now - timedelta(days=inp.window_days)
    if any(_observed(c.observation) < cutoff for c in included):
        quals.add(PriceQualifier.STALE_COMPS)
    if any(
        (c.observation.days_on_market or 0) > inp.window_days for c in asking
    ):
        quals.add(PriceQualifier.LONG_DAYS_ON_MARKET)

    # An ask that has sat far past the window is evidence that the price is
    # wrong, not evidence of what the market pays. Excluded from the sample with
    # a recorded reason, and only while at least two asks survive.
    exclusions: list[str] = []
    asking_before_staleness = list(asking)
    fresh_asking = [
        c for c in asking
        if (c.observation.days_on_market or 0) <= inp.window_days
    ]
    if asking and len(fresh_asking) >= 2 and len(fresh_asking) < len(asking):
        for c in asking:
            if c not in fresh_asking:
                exclusions.append(
                    f"{c.observation.comp_id}: ask has sat "
                    f"{c.observation.days_on_market} days, beyond the "
                    f"{inp.window_days}-day window"
                )
        asking = fresh_asking
        quals.add(PriceQualifier.STALE_ASKS_EXCLUDED)

    # Measured across every ask, including the ones excluded from the band for
    # sitting too long -- an ask that has sat 200 days is the most informative
    # thing in the sample about liquidity and the least informative about value.
    demand = measure_demand(asking_before_staleness, inp.window_days)

    item_band = inp.item_condition_band
    r_comp = [c for c in realized if condition_comparable(c.observation.condition_band, item_band)]
    r_off = [c for c in realized if c not in r_comp]
    a_comp = [c for c in asking if condition_comparable(c.observation.condition_band, item_band)]
    a_off = [c for c in asking if c not in a_comp]

    if any(c.observation.condition_band is not item_band for c in r_comp + a_comp):
        quals.add(PriceQualifier.ADJACENT_CONDITION_POOLED)

    def dist(pool, kind):
        return summarize([c.observation.comparison_price_cents for c in pool], kind) if pool else None

    realized_dist = dist(realized, PriceKind.REALIZED)
    asking_dist = dist(asking, PriceKind.ASKING)
    r_comp_dist = dist(r_comp, PriceKind.REALIZED)
    r_off_dist = dist(r_off, PriceKind.REALIZED)
    a_comp_dist = dist(a_comp, PriceKind.ASKING)
    a_off_dist = dist(a_off, PriceKind.ASKING)

    off_steps = min(
        (s for s in (ladder_steps(c.observation.condition_band, item_band) for c in r_off)
         if s is not None),
        default=None,
    )

    chosen, chosen_kind, chosen_pool, relation = _choose(
        r_comp, r_off, a_comp, a_off,
        r_comp_dist, r_off_dist, a_comp_dist, a_off_dist, quals,
    )

    contributions = build_contributions(
        chosen_pool=chosen_pool,
        r_comp=r_comp, r_off=r_off, a_comp=a_comp, a_off=a_off,
        r_comp_d=r_comp_dist, r_off_d=r_off_dist,
        a_comp_d=a_comp_dist, a_off_d=a_off_dist,
        excluded=excluded, reference_comps=reference_comps, retail=inp.retail,
        demand=demand, exclusions=exclusions,
    )

    base = PriceRecommendation(
        sku=inp.sku,
        unpriceable=True,
        reason="",
        realized=realized_dist,
        asking=asking_dist,
        realized_comparable=r_comp_dist,
        realized_off_band=r_off_dist,
        asking_comparable=a_comp_dist,
        asking_off_band=a_off_dist,
        off_band_steps=off_steps,
        sample_exclusions=tuple(exclusions),
        n_included=len(included),
        n_excluded=len(excluded),
        comparability_profile=profile,
        identity_ceiling=ceiling,
        retail_context=inp.retail,
        demand=demand,
        contributions=contributions,
    )

    if chosen is None:
        if inp.retail:
            quals.add(PriceQualifier.RETAIL_ONLY)
            reason = (
                "no realized or asking comps; a retail reference is context and "
                "cannot become a price on its own"
            )
        else:
            reason = "no comps and no retail reference; ask the operator"
        return _replace(base, reason=reason, qualifiers=_sorted(quals))

    # Adjustments: validated, capped, and never allowed to cross price kinds.
    applied: list[ConditionAdjustment] = []
    factor = 1.0
    for adj in inp.adjustments:
        ok, why = validate_adjustment(adj)
        if not ok:
            raise ValueError(f"invalid condition adjustment: {why}")
        refuse_cross_kind(chosen_kind, chosen_kind)  # documents the invariant
        factor *= 1 + adj.magnitude_pct
        applied.append(adj)
    if applied:
        quals.add(PriceQualifier.ADJUSTED)

    low = round(chosen.p25_cents * factor)
    central = round(chosen.median_cents * factor)
    high = round(chosen.p75_cents * factor)
    if chosen.n < 4:  # p25/p75 on a tiny sample is theatre; show the real spread
        low, high = round(chosen.min_cents * factor), round(chosen.max_cents * factor)

    if chosen.n == 1:
        quals.add(PriceQualifier.SINGLE_COMP)
    elif chosen.n < THIN_SAMPLE_N:
        quals.add(PriceQualifier.THIN_SAMPLE)
    if chosen.is_widely_dispersed:
        quals.add(PriceQualifier.WIDE_DISPERSION)

    ceiling_ok, _ = retail_ceiling_check(central, inp.retail)
    if not ceiling_ok:
        quals.add(PriceQualifier.ABOVE_RETAIL_CEILING)

    basis = _basis_for(chosen_kind, chosen_pool)

    # Retained sold evidence that sits outside the band the price came from is
    # worth seeing, in either direction: below suggests the condition premium is
    # real, above suggests the current asks are underpriced. Neither is applied.
    if r_off_dist is not None and relation == "condition_matched_asks":
        if not (low <= r_off_dist.median_cents <= high):
            quals.add(PriceQualifier.SOLD_EVIDENCE_DIVERGES)

    return _replace(
        base,
        unpriceable=False,
        reason=f"{chosen.n} contributing comps, {chosen_kind}",
        basis=basis,
        price_kind=chosen_kind,
        band_low_cents=low,
        band_central_cents=central,
        band_high_cents=high,
        basis_distribution=chosen,
        band_relation=relation,
        adjustment_factor=factor,
        adjustments=tuple(applied),
        qualifiers=_sorted(quals),
        n_in_basis=chosen.n,
        diagnostic_confidence=_confidence(chosen, quals),
    )


# How a marketplace observation reached us. A search engine's structured summary
# of a listing and a page whose bytes we loaded are different things, and a line
# that reports twelve observations without saying which is overstating them.
_ORIGIN_NAMES = {
    "search_index": "search index",
    "automated_fetch": "fetched page",
    "operator_transcribed": "typed by you",
}


def _origins(pool) -> tuple[str, ...]:
    """Where a pool's observations came from, most common first."""
    counts: dict[str, int] = {}
    for scored in pool:
        name = _ORIGIN_NAMES.get(
            str(scored.observation.retrieval_method),
            str(scored.observation.retrieval_method),
        )
        counts[name] = counts.get(name, 0) + 1
    return tuple(
        name if len(counts) == 1 else f"{name} ({n})"
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )


def build_contributions(
    *,
    chosen_pool,
    r_comp, r_off, a_comp, a_off,
    r_comp_d, r_off_d, a_comp_d, a_off_d,
    excluded, reference_comps, retail, demand, exclusions,
) -> tuple[EvidenceContribution, ...]:
    """One line per evidence type, saying what it was allowed to do.

    Written so the absent lines are as informative as the present ones. "No
    realised sales" and "realised sales that were retained and applied to nothing"
    produce the same band and mean very different things about how much to trust
    it, and only one of them is visible in a distribution table.

    Order is fixed rather than sorted by size: marketplace evidence first, strongest
    first, then the things that are not comps at all. What set the number should be
    the first thing read.
    """
    chosen_ids = {id(c) for c in chosen_pool}
    lines: list[EvidenceContribution] = []

    def pool_line(source: str, pool, dist, when_unused: EvidenceRole, detail: str):
        if not pool:
            return
        used = bool(pool) and id(pool[0]) in chosen_ids
        lines.append(EvidenceContribution(
            source=source, n=len(pool),
            role=EvidenceRole.SET_THE_BAND if used else when_unused,
            detail=detail,
            low_cents=dist.min_cents if dist else None,
            high_cents=dist.max_cents if dist else None,
            median_cents=dist.median_cents if dist else None,
            origins=_origins(pool),
        ))

    pool_line(
        "sold, condition matched", r_comp, r_comp_d, EvidenceRole.RETAINED_NOT_APPLIED,
        "realised sales in this item's condition -- the strongest evidence there is",
    )
    pool_line(
        "sold, other condition", r_off, r_off_d, EvidenceRole.RETAINED_NOT_APPLIED,
        "realised, but for a materially different condition, so it estimates "
        "something else",
    )
    pool_line(
        "asks, condition matched", a_comp, a_comp_d, EvidenceRole.RETAINED_NOT_APPLIED,
        "what comparable examples are being offered at; nobody has paid these",
    )
    # Split for reporting only -- both halves are one pool in `_choose`, and the
    # number is unchanged. "Other condition" and "condition unknown" are different
    # claims, and a line that merges them tells an operator a difference was seen
    # when none was.
    a_off_known = [c for c in a_off if c.observation.condition_band is not ConditionBand.UNKNOWN]
    a_off_unknown = [c for c in a_off if c.observation.condition_band is ConditionBand.UNKNOWN]
    pool_line(
        "asks, other condition", a_off_known, summarize(
            [c.observation.comparison_price_cents for c in a_off_known], PriceKind.ASKING
        ) if a_off_known else None,
        EvidenceRole.RETAINED_NOT_APPLIED,
        "offers for a different condition; the weakest pool that can still set a band",
    )
    pool_line(
        "asks, condition unstated", a_off_unknown, summarize(
            [c.observation.comparison_price_cents for c in a_off_unknown], PriceKind.ASKING
        ) if a_off_unknown else None,
        EvidenceRole.RETAINED_NOT_APPLIED,
        "an approximate asking market: prices with no condition attached, so the "
        "spread is real and its position on the ladder is not",
    )

    if reference_comps:
        lines.append(EvidenceContribution(
            source="retail recorded as a comp", n=len(reference_comps),
            role=EvidenceRole.RECLASSIFIED,
            detail="a retail price is not a market observation; moved to context and "
                   "kept out of every distribution",
        ))
    if retail:
        prices = [r.price_cents for r in retail]
        lines.append(EvidenceContribution(
            source="retail context", n=len(retail), role=EvidenceRole.CEILING_CHECK,
            detail="what it costs new; bounds the answer and never joins a sample",
            low_cents=min(prices), high_cents=max(prices),
        ))
    if demand is not None and demand.n_asks:
        lines.append(EvidenceContribution(
            source="days on market", n=demand.n_with_days,
            role=EvidenceRole.DEMAND_ONLY, detail=demand.describe(),
        ))
    if excluded:
        lines.append(EvidenceContribution(
            source="ruled out as different", n=len(excluded),
            role=EvidenceRole.EXCLUDED,
            detail="not the same sort of thing -- parts, cradles, bundles; retained "
                   "so the exclusion is auditable",
        ))
    if exclusions:
        lines.append(EvidenceContribution(
            source="dropped from the sample", n=len(exclusions),
            role=EvidenceRole.EXCLUDED,
            detail="; ".join(exclusions)[:200],
        ))
    return tuple(lines)


def _choose(r_comp, r_off, a_comp, a_off, r_comp_d, r_off_d, a_comp_d, a_off_d, quals):
    """Which sample sets the number.

    The old rule was "realized beats asking, always", which produced the
    liquidation failure: a single used sold comp anchored a new-with-tags item
    below every comparable current ask, and the ladder cap correctly refused to
    adjust the gap away, leaving no route to a sensible number.

    The rule now is that a **material condition gap outranks price kind**. A sold
    price for a materially different object is not a better estimate of this
    object's value than the asking prices of objects in its own condition -- it is
    an estimate of something else. So when the only realized comps are out of
    band and condition-matched asks exist, the asks position the price and the
    realized evidence is retained, reported, and applied to nothing.

    Nothing is pooled across kinds and no number is transformed. One pooling
    happens *within* a kind: see the thin-matched case below.
    """
    if r_comp:
        return r_comp_d, PriceKind.REALIZED, r_comp, "condition_matched"

    if a_comp:
        quals.add(PriceQualifier.CONDITION_MISMATCH)
        if r_off:
            quals.add(PriceQualifier.POSITIONED_ON_ASKS)
            quals.add(PriceQualifier.SOLD_EVIDENCE_OUT_OF_BAND)
        else:
            quals.add(PriceQualifier.ASKING_ONLY)

        # A thin matched pool, widened by marketplace asks whose condition nobody
        # stated. One matched ask at $110 used to speak over twelve observations
        # saying $50-$95, and the recommendation collapsed to a single point --
        # marketplace evidence discarded for lacking a label, which is exactly
        # what "unknown condition should reduce weight, not remove the data"
        # rules out.
        #
        # No price kind is crossed: both pools are asking prices from
        # marketplaces, differing only in whether anyone said what condition the
        # goods were in. That is a reason to trust the band less, and the
        # qualifier says so -- it is not a reason to pretend the observations do
        # not exist.
        unstated = [c for c in a_off
                    if c.observation.condition_band is ConditionBand.UNKNOWN]
        if len(a_comp) < THIN_SAMPLE_N and len(unstated) > len(a_comp):
            pooled = a_comp + unstated
            quals.add(PriceQualifier.POOLED_UNKNOWN_CONDITION)
            quals.add(PriceQualifier.CONDITION_UNKNOWN_IN_SAMPLE)
            return (
                summarize(
                    [c.observation.comparison_price_cents for c in pooled],
                    PriceKind.ASKING,
                ),
                PriceKind.ASKING, pooled, "asks_widened_by_unstated",
            )
        return a_comp_d, PriceKind.ASKING, a_comp, "condition_matched_asks"

    if r_off:
        quals.add(PriceQualifier.CONDITION_MISMATCH)
        return r_off_d, PriceKind.REALIZED, r_off, "condition_mismatched"

    if a_off:
        quals.add(PriceQualifier.ASKING_ONLY)
        # `condition_mismatch` says a difference was observed. When every ask in
        # the pool has an unstated condition, none was: the honest claim is that
        # the band is an asking spread of unknown condition, which is weaker and
        # differently weak. A mixed pool is still a mismatch.
        if all(c.observation.condition_band is ConditionBand.UNKNOWN for c in a_off):
            quals.add(PriceQualifier.ASKING_UNKNOWN_CONDITION)
            return a_off_d, PriceKind.ASKING, a_off, "asking_condition_unstated"
        quals.add(PriceQualifier.CONDITION_MISMATCH)
        return a_off_d, PriceKind.ASKING, a_off, "condition_mismatched"

    return None, None, [], "none"


def _basis_for(kind: PriceKind, pool: list[ScoredComp]) -> CompBasis:
    exact = any(c.claim.comparability is Comparability.SAME_PRODUCT for c in pool)
    if kind is PriceKind.REALIZED:
        return CompBasis.SOLD_EXACT if exact else CompBasis.SOLD_SIMILAR
    return CompBasis.ACTIVE_EXACT if exact else CompBasis.ACTIVE_SIMILAR


def _confidence(dist: Distribution, quals: set[PriceQualifier]) -> float:
    """Diagnostics only. Nothing in the system branches on this number."""
    score = min(dist.n / 8.0, 1.0)
    if dist.median_cents > 0:
        score *= max(0.0, 1.0 - (dist.iqr_cents / dist.median_cents))
    penalties = {
        PriceQualifier.ASKING_ONLY: 0.6,
        PriceQualifier.CONDITION_MISMATCH: 0.7,
        PriceQualifier.IDENTITY_UNRESOLVED: 0.8,
        PriceQualifier.STALE_COMPS: 0.9,
    }
    for q, f in penalties.items():
        if q in quals:
            score *= f
    return round(max(0.0, min(1.0, score)), 3)


def _observed(obs: CompObservation) -> datetime:
    when = obs.observed_at
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when


def _ceiling(identity_resolution: str) -> Comparability:
    from .comps import ceiling_for_identity

    return ceiling_for_identity(identity_resolution)


def _sorted(quals: set[PriceQualifier]) -> tuple[PriceQualifier, ...]:
    return tuple(sorted(quals, key=str))


def _replace(rec: PriceRecommendation, **kw) -> PriceRecommendation:
    from dataclasses import replace

    return replace(rec, **kw)
