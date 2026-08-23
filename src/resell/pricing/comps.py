"""Comparable-listing vocabulary and validation.

Pure: no database, no model, no HTTP.

A comp is evidence about **another listing**, never about your item. That is the
same problem as `subject=candidate_product`, and it gets the same shape: an
immutable observation with provenance, plus a separate *claim* that connects it
to a SKU and must cite both sides.

Three rules are enforced here rather than trusted:

  identity ceiling   `same_product` is unreachable unless identity resolved
  citation floor     a claim with no citations on both sides is not a claim
  kind separation    realized prices and asking prices are different quantities
                     and no adjustment may convert one into the other (V1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


# --- what kind of number is this ---------------------------------------------


class PriceKind(StrEnum):
    """The single most load-bearing distinction in the whole pricing layer.

    `realized` means somebody paid it. `asking` means nobody has yet. `reference`
    is retail and is not a market observation at all.
    """

    REALIZED = "realized"
    ASKING = "asking"
    REFERENCE = "reference"

    @property
    def verb(self) -> str:
        """The only phrasing permitted for this kind in operator-facing output."""
        return {
            PriceKind.REALIZED: "sold for",
            PriceKind.ASKING: "listed at",
            PriceKind.REFERENCE: "retails at",
        }[self]


class CompBasis(StrEnum):
    """Where a price figure comes from. Travels with every number, everywhere."""

    SOLD_EXACT = "sold_exact"
    SOLD_SIMILAR = "sold_similar"
    ACTIVE_EXACT = "active_exact"
    ACTIVE_SIMILAR = "active_similar"
    RETAIL_REFERENCE = "retail_reference"
    OPERATOR_STATED = "operator_stated"

    @property
    def price_kind(self) -> PriceKind:
        if self in (CompBasis.SOLD_EXACT, CompBasis.SOLD_SIMILAR):
            return PriceKind.REALIZED
        if self in (CompBasis.ACTIVE_EXACT, CompBasis.ACTIVE_SIMILAR):
            return PriceKind.ASKING
        if self is CompBasis.RETAIL_REFERENCE:
            return PriceKind.REFERENCE
        return PriceKind.REALIZED  # operator_stated: you are asserting a real number


class RetailKind(StrEnum):
    """Original retail is a marketing artifact; current retail is a real ceiling."""

    ORIGINAL = "retail_original"
    CURRENT = "retail_current"


# --- how comparable is it ----------------------------------------------------


class Comparability(StrEnum):
    SAME_PRODUCT = "same_product"
    SAME_FAMILY_VARIANT = "same_family_variant"
    CATEGORY_ATTRIBUTE = "category_attribute"
    SUPERFICIAL = "superficial"
    EXCLUDED = "excluded"

    @property
    def rank(self) -> int:
        return {
            Comparability.SAME_PRODUCT: 4,
            Comparability.SAME_FAMILY_VARIANT: 3,
            Comparability.CATEGORY_ATTRIBUTE: 2,
            Comparability.SUPERFICIAL: 1,
            Comparability.EXCLUDED: 0,
        }[self]

    @property
    def contributes(self) -> bool:
        """`superficial` is retained for later analysis and contributes nothing."""
        return self.rank >= 2


def ceiling_for_identity(identity_resolution: str) -> Comparability:
    """Pricing inherits identification's limits.

    `same_product` is a claim that two things are the same product. If the system
    could not resolve which product the item *is*, that claim is unavailable by
    construction -- MP-000003 is the standing example.
    """
    if identity_resolution == "resolved":
        return Comparability.SAME_PRODUCT
    return Comparability.SAME_FAMILY_VARIANT


# --- condition ---------------------------------------------------------------


class ConditionBand(StrEnum):
    """A coarse ladder used only for stratifying and measuring step distance.

    eBay's condition IDs are category-dependent (Taxonomy `getItemConditionPolicies`
    is the authority, and clothing in particular reuses 1000/1500 for "new with
    tags" / "new without tags"). The id is the canonical vocabulary and this ladder
    is derived from it: `pricing.condition` resolves text to an id, and
    `CONDITION_ID_TO_BAND` turns the id into a rung. The band, not the raw id, is
    what stratifies -- ordering is all a comparison needs, and the ordering holds
    across categories even where the labels do not.
    """

    NEW_WITH_TAGS = "new_with_tags"
    NEW_WITHOUT_TAGS = "new_without_tags"
    NEW_OTHER = "new_other"
    REFURBISHED = "refurbished"
    USED_EXCELLENT = "used_excellent"
    USED_GOOD = "used_good"
    USED_FAIR = "used_fair"
    FOR_PARTS = "for_parts"
    UNKNOWN = "unknown"

    @property
    def ordinal(self) -> int | None:
        """Position on the ladder. `UNKNOWN` has no position, deliberately."""
        return {
            ConditionBand.NEW_WITH_TAGS: 8,
            ConditionBand.NEW_WITHOUT_TAGS: 7,
            ConditionBand.NEW_OTHER: 6,
            ConditionBand.REFURBISHED: 5,
            ConditionBand.USED_EXCELLENT: 4,
            ConditionBand.USED_GOOD: 3,
            ConditionBand.USED_FAIR: 2,
            ConditionBand.FOR_PARTS: 1,
        }.get(self)


# Provisional until per-category condition policies are fetched. Any mapping the
# Taxonomy call contradicts is a bug in this table, not in the caller.
CONDITION_ID_TO_BAND: dict[int, ConditionBand] = {
    1000: ConditionBand.NEW_WITH_TAGS,
    # One id, two category labels: "New (other)" nearly everywhere, "New without
    # tags" in apparel. Open-box cameras and tagless garments arrive as the same
    # number and cannot be told apart without the category, so both band to
    # `new_other`. The `new_without_tags` rung stays on the ladder because an
    # operator setting our own item's condition *does* know the category, and the
    # rung above it is then meaningful.
    1500: ConditionBand.NEW_OTHER,
    1750: ConditionBand.NEW_OTHER,
    2000: ConditionBand.REFURBISHED,
    2010: ConditionBand.REFURBISHED,
    2020: ConditionBand.REFURBISHED,
    2030: ConditionBand.REFURBISHED,
    2500: ConditionBand.REFURBISHED,
    2750: ConditionBand.USED_EXCELLENT,
    # 3000 looks like it should be the top used rung -- its Sell API enum name is
    # USED_EXCELLENT -- and it is not. It is the *generic* used condition, shown as
    # "Pre-owned" or "Used" in almost every category, and it is what the large
    # majority of used listings carry. The graded rungs 4000/5000/6000 ("Very
    # Good", "Good", "Acceptable") are mostly books and media. Banding 3000 to the
    # top would inflate every ordinary second-hand comp on the site.
    #
    # This is the clearest case of the enum name and the category label disagreeing,
    # which is why the id is canonical and neither name is.
    3000: ConditionBand.USED_GOOD,
    4000: ConditionBand.USED_EXCELLENT,
    5000: ConditionBand.USED_GOOD,
    6000: ConditionBand.USED_FAIR,
    7000: ConditionBand.FOR_PARTS,
}


def band_for_condition_id(condition_id: int | None) -> ConditionBand:
    if condition_id is None:
        return ConditionBand.UNKNOWN
    return CONDITION_ID_TO_BAND.get(condition_id, ConditionBand.UNKNOWN)


def ladder_steps(a: ConditionBand, b: ConditionBand) -> int | None:
    """Distance in ladder steps, or None when either side is unknown."""
    if a.ordinal is None or b.ordinal is None:
        return None
    return abs(a.ordinal - b.ordinal)


class ConditionSource(StrEnum):
    """Who says so. A comp's condition is always a stranger's word."""

    SELLER_DECLARED = "seller_declared"
    OPERATOR_OBSERVED = "operator_observed"
    EVIDENCE_CITED = "evidence_cited"
    # Nobody said. A search index returns a price with no condition attached, and
    # defaulting that to `seller_declared` would attribute to a seller a statement
    # they never made -- the one thing this enum exists to prevent.
    UNSTATED = "unstated"


# --- provenance and licensing ------------------------------------------------


class RetrievalMethod(StrEnum):
    OPERATOR_TRANSCRIBED = "operator_transcribed"
    AUTOMATED_FETCH = "automated_fetch"
    # A search engine's structured description of a listing, not the listing. The
    # bytes of the page were never loaded, so there is no excerpt in the sense the
    # rest of this codebase means -- what is stored is the index's own words.
    # Weaker than `automated_fetch` and stronger than nothing, and it must stay
    # distinguishable from both.
    SEARCH_INDEX = "search_index"


class ModelVisibility(StrEnum):
    """Derived from the source's licence class, never set ad hoc.

    `derived_only` is the setting that matters: code may compute statistics from
    the rows and the model may see the statistics, but the rows never enter a
    prompt. Because the central estimate is computed deterministically, this
    degrades without changing the answer.
    """

    FULL = "full"
    DERIVED_ONLY = "derived_only"
    NONE = "none"


class ComparisonBasis(StrEnum):
    TOTAL_TO_BUYER = "total_to_buyer"
    ITEM_PRICE = "item_price"


# --- the observation ---------------------------------------------------------


@dataclass(frozen=True)
class CompObservation:
    """A point-in-time snapshot of another listing. Never updated, only superseded.

    `shipping_cents is None` means shipping was not reported, not that it was zero.
    Such comps are **retained** with a `shipping_unknown` qualifier rather than
    excluded: dropping them silently shrinks the sample in a way nobody can see.
    """

    comp_id: str
    marketplace: str
    external_id: str
    price_kind: PriceKind
    basis: CompBasis
    price_cents: int
    observed_at: datetime
    condition_band: ConditionBand = ConditionBand.UNKNOWN
    condition_declared_raw: str | None = None
    condition_source: ConditionSource = ConditionSource.SELLER_DECLARED
    shipping_cents: int | None = None
    sale_date: date | None = None
    days_on_market: int | None = None
    url: str | None = None
    title: str | None = None
    currency: str = "USD"
    listing_format: str | None = None
    quantity: int = 1
    seller_type: str | None = None
    retail_kind: RetailKind | None = None
    source_authority: str | None = None
    retrieval_method: RetrievalMethod = RetrievalMethod.OPERATOR_TRANSCRIBED
    adapter: str | None = None
    query_text: str | None = None
    raw_payload_hash: str | None = None
    # The page text the price was read from. None when the operator transcribed the
    # listing: they are the witness to it, and there is no quotation to keep. An
    # automated extraction has no witness, so it carries the words it read.
    source_excerpt: str | None = None
    model_visibility: ModelVisibility = ModelVisibility.FULL
    retention_expires_at: datetime | None = None

    @property
    def shipping_known(self) -> bool:
        return self.shipping_cents is not None

    @property
    def comparison_basis(self) -> ComparisonBasis:
        return (
            ComparisonBasis.TOTAL_TO_BUYER
            if self.shipping_known
            else ComparisonBasis.ITEM_PRICE
        )

    @property
    def comparison_price_cents(self) -> int:
        """Total to buyer where shipping is known, item price where it is not.

        Free postage is the house policy, so a $40 item with $8 postage is not
        cheaper than a $45 item shipped free. Mixing the two bases inside one
        sample is a real distortion and is reported as `mixed_comparison_basis`
        rather than quietly averaged away.
        """
        return self.price_cents + (self.shipping_cents or 0)


# --- the claim ---------------------------------------------------------------


@dataclass(frozen=True)
class CompClaim:
    """Connects one observation to one SKU. Citations required on both sides."""

    claim_id: str
    sku: str
    comp_id: str
    comparability: Comparability
    item_citations: tuple[str, ...] = ()
    comp_citations: tuple[str, ...] = ()
    rationale: str = ""
    excluded_reason: str | None = None

    @property
    def contributes(self) -> bool:
        return self.comparability.contributes


def validate_claim(claim: CompClaim, *, identity_resolution: str) -> tuple[bool, str]:
    """Deterministic legality of a comp claim."""
    if claim.comparability is Comparability.EXCLUDED:
        if not claim.excluded_reason:
            return False, "an excluded comp must record why it was excluded"
        return True, "excluded with reason"

    ceiling = ceiling_for_identity(identity_resolution)
    if claim.comparability.rank > ceiling.rank:
        return False, (
            f"{claim.comparability} requires identity_resolution=resolved; "
            f"identity is {identity_resolution!r}, so the strongest available "
            f"claim is {ceiling}"
        )
    if not claim.item_citations:
        return False, "a comp claim must cite the item evidence it matched on"
    if not claim.comp_citations:
        return False, "a comp claim must cite the comp fields it matched against"
    return True, f"{claim.comparability} claim cited on both sides"


# --- adjustments -------------------------------------------------------------

# Asymmetric on purpose. Adjusting *up* from worse-condition comps is the
# direction where optimism compounds and the operator is least likely to notice,
# so it gets the tighter cap.
MAX_UPWARD_ADJUSTMENT_PCT = 0.15
MAX_DOWNWARD_ADJUSTMENT_PCT = 0.35
MAX_LADDER_STEPS = 2


class AdjustmentSource(StrEnum):
    STRATIFIED = "stratified"  # no adjustment needed; bands already match
    DERIVED_FROM_SET = "derived_from_set"
    MODEL_PROPOSED = "model_proposed"
    OPERATOR_STATED = "operator_stated"


@dataclass(frozen=True)
class ConditionAdjustment:
    """An itemised line, never a lump-sum haircut."""

    magnitude_pct: float  # signed: +0.10 raises the estimate, -0.20 lowers it
    reason: str
    source: AdjustmentSource
    from_band: ConditionBand
    to_band: ConditionBand
    citations: tuple[str, ...] = ()
    derived_n: int | None = None


def validate_adjustment(adj: ConditionAdjustment) -> tuple[bool, str]:
    if not adj.reason.strip():
        return False, "an adjustment must state a reason"
    if adj.source is AdjustmentSource.MODEL_PROPOSED and not adj.citations:
        return False, "a model-proposed adjustment must cite the evidence behind it"
    if adj.source is AdjustmentSource.DERIVED_FROM_SET and not adj.derived_n:
        return False, "a derived adjustment must record the sample it was derived from"

    if adj.magnitude_pct > MAX_UPWARD_ADJUSTMENT_PCT:
        return False, (
            f"upward adjustment {adj.magnitude_pct:+.0%} exceeds the "
            f"{MAX_UPWARD_ADJUSTMENT_PCT:.0%} cap"
        )
    if adj.magnitude_pct < -MAX_DOWNWARD_ADJUSTMENT_PCT:
        return False, (
            f"downward adjustment {adj.magnitude_pct:+.0%} exceeds the "
            f"{MAX_DOWNWARD_ADJUSTMENT_PCT:.0%} cap"
        )

    steps = ladder_steps(adj.from_band, adj.to_band)
    if steps is None:
        return False, "cannot adjust across an unknown condition band"
    if steps > MAX_LADDER_STEPS:
        return False, (
            f"{adj.from_band} to {adj.to_band} is {steps} ladder steps; "
            f"the limit is {MAX_LADDER_STEPS}"
        )
    return True, f"{adj.magnitude_pct:+.0%} across {steps} step(s)"


class CrossKindAdjustment(ValueError):
    """Raised when something tries to turn an asking price into a sold price.

    V1 forbids this outright. Asking distributions are reported as asking
    distributions; the discount to expected realised price is the operator's
    judgement, not a model-proposed multiplier hidden inside a number.
    """


def refuse_cross_kind(from_kind: PriceKind, to_kind: PriceKind) -> None:
    if from_kind is not to_kind:
        raise CrossKindAdjustment(
            f"cannot convert {from_kind} prices into {to_kind} prices: "
            "asking and realised prices are different quantities, and V1 does "
            "not permit an estimated sold price derived from asking prices"
        )
