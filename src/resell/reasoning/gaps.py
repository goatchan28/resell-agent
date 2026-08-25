"""Deterministic analysis of what the model produced.

The model reasons freely and proposes; everything here decides whether what it
proposed is usable. No judgment, no semantics -- only arithmetic over citations.

The central rule: a candidate value exists only because evidence supports it.
Candidates are generated from observations, never seeded from Taxonomy's allowed
list. Taxonomy validates a proposed string; it does not supply options. That keeps
the candidate set to the two or three readings actually in play rather than every
value the category permits.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from enum import StrEnum

from resell.reasoning.schema import (
    ADJUDICATING_BASES,
    MODES_REQUIRING_NEGATIVE_FINDING,
    EvidenceRef,
    IdentificationEffort,
    IdentificationMode,
    NegativeFinding,
)


class Resolution(StrEnum):
    RESOLVED = "resolved"
    RESOLVED_BY_OPERATOR = "resolved_by_operator"
    # Multi-valued aspect whose values rest on separate evidence: usable, but worth
    # a look before publishing, because "accepts several values" and "these several
    # values belong together" are different claims.
    RESOLVED_UNVERIFIED = "resolved_unverified"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"
    CONTRADICTED = "contradicted"


BLOCKING_RESOLUTIONS = frozenset(
    {Resolution.UNSUPPORTED, Resolution.AMBIGUOUS, Resolution.CONTRADICTED}
)


@dataclass(frozen=True)
class Candidate:
    """A value the evidence materially supports."""

    value: str
    support: tuple[EvidenceRef, ...] = ()

    @property
    def evidence_ids(self) -> frozenset[int]:
        return frozenset(ref.evidence_id for ref in self.support)

    @property
    def has_adjudicating_support(self) -> bool:
        return any(ref.basis in ADJUDICATING_BASES for ref in self.support)


@dataclass(frozen=True)
class AspectOutcome:
    aspect_name: str
    resolution: Resolution
    value: str | None
    candidates: tuple[Candidate, ...]
    explanation: str
    values: tuple[str, ...] = ()
    unsupported_reason: UnsupportedReason | None = None

    def __post_init__(self) -> None:
        if not self.values and self.value:
            object.__setattr__(self, "values", (self.value,))

    @property
    def blocking(self) -> bool:
        return self.resolution in BLOCKING_RESOLUTIONS


def resolve_aspect(
    aspect_name: str,
    candidates: list[Candidate],
    *,
    cardinality: str = "SINGLE",
    unsupported_reason: UnsupportedReason | None = None,
) -> AspectOutcome:
    """Decide whether an aspect is settled, and if not, why not.

    Cardinality changes what several supported values *mean*. On a SINGLE aspect
    they compete, and the item can only be one of them. On a MULTI aspect they
    coexist: a fabric that is 88% wool, 8% polyester and 4% elastane genuinely has
    three materials, and reporting that as an ambiguity to be resolved would be
    asking the operator to choose between three correct answers.

    Multiple candidates arise for two different reasons, and the distinction is
    computable rather than a judgment:

      - supporting evidence sets are DISJOINT -> two independent sources disagree
        -> contradicted. The question is "which reading is right?"
      - supporting evidence sets OVERLAP -> a single observation is itself
        indecisive -> ambiguous. The question is "can you look closer or measure?"

    Contradiction takes precedence in mixed cases: a genuine disagreement between
    sources is more serious than one hedged observation, and asking about it first
    is what an operator would want.
    """
    supported = [c for c in candidates if c.support]
    dropped = len(candidates) - len(supported)
    dropped_note = f" ({dropped} uncited candidate(s) discarded)" if dropped else ""

    if not supported:
        detail = {
            UnsupportedReason.NOT_OBSERVED:
                f"nothing observed speaks to {aspect_name!r}",
            UnsupportedReason.NOT_APPLICABLE:
                f"{aspect_name!r} does not apply to this kind of object",
            UnsupportedReason.NONE_APPLY:
                f"evidence describes {aspect_name!r} but no allowed value is truthful",
            UnsupportedReason.INSUFFICIENT_EVIDENCE:
                f"something was observed but it does not name a value for {aspect_name!r}",
        }.get(
            unsupported_reason,
            f"no evidence supports any value for {aspect_name!r}",
        )
        return AspectOutcome(
            aspect_name, Resolution.UNSUPPORTED, None, tuple(candidates),
            f"{detail}{dropped_note}", unsupported_reason=unsupported_reason,
        )

    if len(supported) == 1:
        only = supported[0]
        return AspectOutcome(
            aspect_name, Resolution.RESOLVED, only.value, tuple(supported),
            f"{only.value!r} supported by evidence "
            f"{sorted(only.evidence_ids)}{dropped_note}",
        )

    if cardinality.upper() == "MULTI":
        values = tuple(c.value for c in supported)
        # Accepting several values is not the same as those values coexisting.
        # Material cites one fabric-content reading for wool, polyester and elastane:
        # the same evidence, three properties the item genuinely has. MPN cites one
        # observation for a product number and a different one for a style code:
        # two codes competing for one field. Disjoint evidence on a multi-valued
        # aspect is worth surfacing, without blocking -- the values may legitimately
        # coexist, but nothing here can tell.
        independently_evidenced = any(
            not (a.evidence_ids & b.evidence_ids)
            for index, a in enumerate(supported)
            for b in supported[index + 1 :]
        )
        if independently_evidenced:
            detail = "; ".join(
                f"{c.value!r} from {sorted(c.evidence_ids)}" for c in supported
            )
            return AspectOutcome(
                aspect_name, Resolution.RESOLVED_UNVERIFIED, values[0], tuple(supported),
                f"{list(values)} each rest on separate evidence ({detail}) -- confirm "
                f"they coexist rather than compete for this field{dropped_note}",
                values=values,
            )
        return AspectOutcome(
            aspect_name, Resolution.RESOLVED, values[0], tuple(supported),
            f"{list(values)} all supported by shared evidence "
            f"{sorted(supported[0].evidence_ids)}; the aspect accepts multiple values"
            f"{dropped_note}",
            values=values,
        )

    # The operator has adjudicated with the object in hand, so their statement
    # settles it. Losing candidates are retained rather than deleted.
    adjudicated = [c for c in supported if c.has_adjudicating_support]
    if len(adjudicated) == 1:
        winner = adjudicated[0]
        others = [c.value for c in supported if c is not winner]
        return AspectOutcome(
            aspect_name, Resolution.RESOLVED_BY_OPERATOR, winner.value, tuple(supported),
            f"{winner.value!r} confirmed by the operator, superseding {others}",
        )

    disjoint_pair = any(
        not (a.evidence_ids & b.evidence_ids)
        for index, a in enumerate(supported)
        for b in supported[index + 1 :]
    )
    values = [c.value for c in supported]
    if disjoint_pair:
        detail = "; ".join(
            f"{c.value!r} from evidence {sorted(c.evidence_ids)}" for c in supported
        )
        return AspectOutcome(
            aspect_name, Resolution.CONTRADICTED, None, tuple(supported),
            f"independent evidence disagrees on {aspect_name!r}: {detail}",
        )

    shared = sorted(set.intersection(*(set(c.evidence_ids) for c in supported)))
    return AspectOutcome(
        aspect_name, Resolution.AMBIGUOUS, None, tuple(supported),
        f"evidence {shared} does not discriminate between {values} for {aspect_name!r}",
    )


# --- gaps --------------------------------------------------------------------


class UnsupportedReason(StrEnum):
    """Why an aspect has no value. Three situations that used to look identical.

    The distinction exists because `none_apply` is not evidence about the item at
    all -- it is evidence about the category. A standalone suit jacket in a category
    whose Style offers only "2 Piece", "3 Piece" and "Tuxedo" has no truthful value
    available, and the pressure of a required field produces a least-wrong answer.
    Asking the operator to supply a Style there wastes their time; the category is
    the thing to question.
    """

    # Nothing in the photographs speaks to it.
    NOT_OBSERVED = "not_observed"
    # The property does not apply to this kind of object at all -- an inseam on a
    # jacket. Weak evidence that the category is aimed at something broader.
    NOT_APPLICABLE = "not_applicable"
    # Evidence describes the property, but no allowed value is truthful. Strong
    # evidence that the category is wrong.
    NONE_APPLY = "none_apply"
    # Something was observed, but not enough to name a value.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


# Reasons that say something about the category rather than about the item.
CATEGORY_SIGNALS = frozenset({UnsupportedReason.NONE_APPLY, UnsupportedReason.NOT_APPLICABLE})


class GapAction(StrEnum):
    """What would close the gap. Determines how the question is worded."""

    ASK_OPERATOR = "ask_operator"
    REQUEST_PHOTO = "request_photo"
    REQUEST_MEASUREMENT = "request_measurement"
    RESEARCH = "research"
    # The aspect cannot be answered truthfully in this category. Forcing a value
    # would be the failure; the category is what needs looking at.
    REVIEW_CATEGORY = "review_category"


@dataclass(frozen=True)
class Gap:
    aspect_name: str
    resolution: Resolution
    action: GapAction
    question: str
    blocking: bool = True


def gap_for(outcome: AspectOutcome) -> Gap | None:
    """Turn a blocking outcome into a specific, answerable question.

    Wording follows the resolution: a contradiction asks which source is right, an
    ambiguity asks for a closer look. Asking "what size is it?" for both would
    waste the operator's time on the case where the answer is already visible.
    """
    if not outcome.blocking:
        return None

    values = [c.value for c in outcome.candidates if c.support]

    if outcome.resolution is Resolution.CONTRADICTED:
        return Gap(
            outcome.aspect_name, outcome.resolution, GapAction.ASK_OPERATOR,
            f"Sources disagree on {outcome.aspect_name}: {' vs '.join(values)}. "
            f"Which is correct? ({outcome.explanation})",
        )

    if outcome.resolution is Resolution.AMBIGUOUS:
        return Gap(
            outcome.aspect_name, outcome.resolution, GapAction.REQUEST_PHOTO,
            f"{outcome.aspect_name} could be any of {values} from the current photos. "
            f"A closer photo of the relevant detail would settle it.",
        )

    if outcome.unsupported_reason is UnsupportedReason.NONE_APPLY:
        return Gap(
            outcome.aspect_name, outcome.resolution, GapAction.REVIEW_CATEGORY,
            f"{outcome.aspect_name} has no truthful value among this category's allowed "
            f"options. This is a question about the category, not the item: forcing a "
            f"least-wrong value here is exactly the failure to avoid.",
        )

    if outcome.unsupported_reason is UnsupportedReason.NOT_APPLICABLE:
        return Gap(
            outcome.aspect_name, outcome.resolution, GapAction.REVIEW_CATEGORY,
            f"{outcome.aspect_name} does not apply to this object. A category "
            f"requiring it may be aimed at something broader than this item.",
        )

    if outcome.unsupported_reason is UnsupportedReason.INSUFFICIENT_EVIDENCE:
        return Gap(
            outcome.aspect_name, outcome.resolution, GapAction.REQUEST_PHOTO,
            f"{outcome.aspect_name} was partly observed but not enough to name a value. "
            f"A closer photo of the relevant detail may settle it.",
        )

    return Gap(
        outcome.aspect_name, outcome.resolution, GapAction.ASK_OPERATOR,
        f"Nothing observed supports a value for {outcome.aspect_name}. "
        f"Can you supply it, or point to where on the item it appears?",
    )


def analyse(
    required_aspects: list[str],
    candidates_by_aspect: dict[str, list[Candidate]],
    cardinality_by_aspect: dict[str, str] | None = None,
    reasons_by_aspect: dict[str, UnsupportedReason] | None = None,
) -> tuple[list[AspectOutcome], list[Gap]]:
    """Resolve every required aspect and collect the gaps."""
    cardinality_by_aspect = cardinality_by_aspect or {}
    reasons_by_aspect = reasons_by_aspect or {}
    outcomes = [
        resolve_aspect(
            name, candidates_by_aspect.get(name, []),
            cardinality=cardinality_by_aspect.get(name, "SINGLE"),
            unsupported_reason=reasons_by_aspect.get(name),
        )
        for name in sorted(required_aspects)
    ]
    gaps = [gap for gap in (gap_for(outcome) for outcome in outcomes) if gap]
    return outcomes, gaps


# --- negative evidence policy ------------------------------------------------


@dataclass(frozen=True)
class EffortPolicy:
    """How much looking is enough before "no identity available" is credible."""

    require_named_surfaces: bool
    minimum_surfaces: int
    require_operator_confirmation: bool
    description: str


EFFORT_POLICIES: dict[IdentificationEffort, EffortPolicy] = {
    # A cheap object gets a cheap answer: it is enough to say the supplied photos
    # showed no branding. No extra photos are requested.
    IdentificationEffort.MINIMAL: EffortPolicy(
        require_named_surfaces=False,
        minimum_surfaces=0,
        require_operator_confirmation=False,
        description="photos reviewed, no branding visible",
    ),
    IdentificationEffort.STANDARD: EffortPolicy(
        require_named_surfaces=True,
        minimum_surfaces=1,
        require_operator_confirmation=False,
        description="named surfaces examined",
    ),
    IdentificationEffort.THOROUGH: EffortPolicy(
        require_named_surfaces=True,
        minimum_surfaces=2,
        require_operator_confirmation=True,
        description="named surfaces examined and confirmed by the operator",
    ),
}


def negative_finding_sufficient(
    effort: IdentificationEffort, finding: NegativeFinding | None
) -> tuple[bool, str]:
    """Is this negative finding enough to justify a no-identity mode at this effort?

    The requirement scales; the concept does not. There is always a citable reason,
    but a three-dollar ornament does not earn a forensic surface sweep.
    """
    policy = EFFORT_POLICIES[effort]

    if finding is None:
        return False, (
            "declaring no discoverable identity needs a cited negative finding, "
            "otherwise giving up is indistinguishable from looking"
        )
    if finding.photos_reviewed < 1:
        return False, "no photos were reviewed"
    if policy.require_named_surfaces and len(finding.surfaces_examined) < policy.minimum_surfaces:
        return False, (
            f"{effort} effort expects at least {policy.minimum_surfaces} named "
            f"surface(s); got {list(finding.surfaces_examined)}"
        )
    if policy.require_operator_confirmation and not finding.operator_confirmed:
        return False, f"{effort} effort expects the operator to confirm no identity is present"
    return True, f"sufficient for {effort} effort: {policy.description}"


# Preference order used only to name the best available mode in a message. Not a
# ranking the code acts on: branded_generic and described_object describe different
# situations rather than different amounts of the same thing.
_MODE_PREFERENCE = (
    IdentificationMode.EXACT_PRODUCT,
    IdentificationMode.PRODUCT_FAMILY,
    IdentificationMode.BRANDED_GENERIC,
    IdentificationMode.DESCRIBED_OBJECT,
)


def mode_is_supported(
    mode: IdentificationMode,
    *,
    effort: IdentificationEffort,
    negative_finding: NegativeFinding | None,
    brand_support: tuple[EvidenceRef, ...] = (),
    line_support: tuple[EvidenceRef, ...] = (),
    qualifying_match: bool = False,
) -> tuple[bool, str]:
    """Check that a declared mode is backed by what it claims.

    `exact_product` requires a *selected* external match, not merely that the item
    carries identifiers. A check digit proves a transcription is well-formed; it
    says nothing about which product the number denotes. Resolution is a separate
    step and only something external supplies it -- which is the same reasoning the
    matcher used when it rejected a near-identical Explorer jacket because its item
    number did not match the swing tag. Unmatched identifiers cannot be grounds to
    reject a candidate and grounds to claim identity at the same time.

    The cost is real: an item whose manufacturer page no longer exists stays
    product_family however legible its part number. That is preferable to a wrong
    exact_product, which propagates into comps as a search for a SKU this item does
    not have.
    """
    if mode is IdentificationMode.UNRESOLVED:
        return True, "unresolved makes no claim"

    if mode is IdentificationMode.EXACT_PRODUCT:
        if not qualifying_match:
            return False, (
                "exact_product requires a match to a specific catalogue product, of "
                "identifier strength and from a source good enough to donate. "
                "Identifiers on the item establish a family; they do not say which "
                "product they denote."
            )
        return True, "a qualifying product match resolves this to a specific product"

    if mode is IdentificationMode.PRODUCT_FAMILY:
        if not brand_support:
            return False, "product_family needs a cited brand"
        if not line_support:
            return False, (
                "product_family needs a cited product line or manufacturer code; with "
                "a brand alone this is branded_generic"
            )
        return True, "brand and product line are cited"

    if mode is IdentificationMode.BRANDED_GENERIC:
        if not brand_support:
            return False, "branded_generic needs a cited brand"
        ok, why = negative_finding_sufficient(effort, negative_finding)
        if not ok:
            return False, (
                f"branded_generic asserts no line or model is discoverable: {why}"
            )
        return True, f"brand cited, and {why}"

    ok, why = negative_finding_sufficient(effort, negative_finding)
    if not ok:
        return False, f"{mode}: {why}"
    return True, f"{mode}: {why}"


def supported_modes(**kwargs) -> list[IdentificationMode]:
    """Every mode the evidence earns, in preference order."""
    return [
        mode for mode in _MODE_PREFERENCE
        if mode_is_supported(mode, **kwargs)[0]
    ]


# --- effort escalation -------------------------------------------------------


class EscalationDecision(StrEnum):
    """Who may grant more identification budget."""

    AUTO_GRANT = "auto_grant"
    REQUIRES_OPERATOR = "requires_operator"
    REFUSE = "refuse"


_EFFORT_ORDER = (
    IdentificationEffort.MINIMAL,
    IdentificationEffort.STANDARD,
    IdentificationEffort.THOROUGH,
)


def escalation_policy(
    current: IdentificationEffort,
    requested: IdentificationEffort,
    *,
    cited_evidence: tuple[int, ...] = (),
) -> tuple[EscalationDecision, str]:
    """Decide whether more identification budget can be granted, and by whom.

    The model may ask; it may not grant itself anything. A single step up from
    minimal is cheap enough to allow automatically when the request is backed by an
    observation -- refusing it would just route trivial decisions through a human.
    Anything larger, and anything reaching thorough, is the operator's call.

    Deliberately scoped to identity resolution. Pricing and comp research get their
    own budget policy rather than inheriting this one.
    """
    if requested == current:
        return EscalationDecision.REFUSE, f"already at {current}"

    current_index = _EFFORT_ORDER.index(current)
    requested_index = _EFFORT_ORDER.index(requested)
    if requested_index < current_index:
        return EscalationDecision.REFUSE, (
            f"escalation only moves upward; {current} -> {requested} is a reduction"
        )

    if not cited_evidence:
        return EscalationDecision.REFUSE, (
            "an escalation request must cite the observations that justify it, "
            "otherwise 'look harder' is unfalsifiable"
        )

    if requested is IdentificationEffort.THOROUGH:
        return EscalationDecision.REQUIRES_OPERATOR, (
            "thorough identification costs the operator time and photographs; "
            "that is their call to make"
        )
    if requested_index - current_index > 1:
        return EscalationDecision.REQUIRES_OPERATOR, (
            f"{current} -> {requested} skips a level"
        )
    return EscalationDecision.AUTO_GRANT, (
        f"{current} -> {requested} is one step and is justified by evidence "
        f"{sorted(cited_evidence)}"
    )


# --- category fit ------------------------------------------------------------


@dataclass(frozen=True)
class CategoryFitSignals:
    """What the mapping outcome says about the category, as counts rather than a score.

    Deliberately NOT a single number. "Most aspects filled" is easy to compute and
    actively misleading: a broad, wrong category can have fewer missing required
    fields than the correct narrow one, because it demands less. Completeness ranks
    categories by how little they ask, which is the opposite of what matters.

    These counts are inputs to a judgment, not a substitute for one.
    """

    category_id: str
    required_total: int
    required_resolved: int
    required_none_apply: tuple[str, ...]
    required_not_applicable: tuple[str, ...]
    required_not_observed: tuple[str, ...]
    optional_not_applicable: tuple[str, ...]

    @property
    def has_untruthful_requirement(self) -> bool:
        """A required field with no truthful option. The strongest signal available."""
        return bool(self.required_none_apply)

    def summary(self) -> str:
        parts = [f"{self.required_resolved}/{self.required_total} required aspects resolved"]
        if self.required_none_apply:
            parts.append(
                f"{len(self.required_none_apply)} required with NO truthful option "
                f"({', '.join(self.required_none_apply)})"
            )
        if self.required_not_applicable:
            parts.append(
                f"{len(self.required_not_applicable)} required but inapplicable "
                f"({', '.join(self.required_not_applicable)})"
            )
        if self.optional_not_applicable:
            parts.append(
                f"{len(self.optional_not_applicable)} optional aspects belong to a "
                "different kind of object"
            )
        return "; ".join(parts)


def category_fit_signals(
    category_id: str, outcomes: list[AspectOutcome], required_names: set[str]
) -> CategoryFitSignals:
    def named(reason: UnsupportedReason, required: bool) -> tuple[str, ...]:
        return tuple(
            o.aspect_name for o in outcomes
            if o.unsupported_reason is reason
            and ((o.aspect_name in required_names) is required)
        )

    return CategoryFitSignals(
        category_id=category_id,
        required_total=len(required_names),
        required_resolved=sum(
            1 for o in outcomes
            if o.aspect_name in required_names and not o.blocking
        ),
        required_none_apply=named(UnsupportedReason.NONE_APPLY, True),
        required_not_applicable=named(UnsupportedReason.NOT_APPLICABLE, True),
        required_not_observed=named(UnsupportedReason.NOT_OBSERVED, True),
        optional_not_applicable=named(UnsupportedReason.NOT_APPLICABLE, False),
    )


def category_review_advice(signals: CategoryFitSignals) -> str:
    """What the signals warrant saying. Never "use category X instead"."""
    if signals.has_untruthful_requirement:
        return (
            f"Category {signals.category_id} requires "
            f"{', '.join(signals.required_none_apply)}, and the evidence supports no "
            "truthful value among the allowed options. That is a category problem. "
            "Review alternatives before supplying a value; the aspects mapped so far "
            "belong to this category's form and will need re-mapping if it changes."
        )
    if len(signals.optional_not_applicable) >= 3:
        return (
            f"Category {signals.category_id} carries "
            f"{len(signals.optional_not_applicable)} aspects that do not apply to this "
            "object, which suggests it covers a broader class. A narrower category may "
            "fit better, though a broader one is not necessarily wrong."
        )
    return f"Nothing in the mapping outcome argues against category {signals.category_id}."


# --- value substitution ------------------------------------------------------


# The same thing under another name. Marketplaces standardise on one term and tags
# print another, so a value can be absent from the allowed list while the fibre is
# very much present. Restricted to pairs that denote an identical material -- this
# is a translation table, not a similarity one. Elastane and elastodiene are NOT
# here: one is polyurethane-based and the other rubber-based.
VALUE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "spandex": ("elastane", "lycra"),
    "viscose": ("rayon",),
    "nylon": ("polyamide",),
    "acrylic": ("polyacrylic",),
    "lyocell": ("tencel",),
    "faux leather": ("pu leather", "synthetic leather", "pleather"),
    "flax": ("linen",),
    # Brands, where eBay records the full form and the object says the short one.
    # Same rule as the fibres: an identical referent, not a similar one. Without
    # the alias, citing the observation that actually reads "Beats" fails to
    # support eBay's own value and the correct mapping is rejected.
    "beats by dr. dre": ("beats",),
}


# Two words for one word, where the difference is how it is written rather than
# what it means. Kept apart from VALUE_SYNONYMS above, which pairs *different*
# words for the same referent -- elastane and spandex are two names, grey and
# gray are two spellings. Applied in both directions to both sides.
SPELLING_VARIANTS: dict[str, str] = {
    "grey": "gray",
    "colour": "color",
    "aluminium": "aluminum",
    "jewellery": "jewelry",
    "fibre": "fiber",
}

_SPELLING = re.compile(
    r"\b(" + "|".join(sorted(SPELLING_VARIANTS, key=len, reverse=True)) + r")\b"
)
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def _same_spelling(text: str) -> str:
    """One spelling of each word, so grey and gray compare equal."""
    return _SPELLING.sub(lambda m: SPELLING_VARIANTS[m.group(1)], text.casefold())


def _letters_only(text: str) -> str:
    return _NOT_ALNUM.sub("", text.casefold())


def value_appears_in(value: str, text: str) -> str | None:
    """Whether a value is present in some text, directly or under another name.

    Four ways, in order of how much they assume. The first two are the original
    ones. The last two exist because MP-000041 asked its owner for a brand and a
    colour that were printed on the item and already written down:

      Brand `XD Design` cited from an observation reading "XDDESIGN"
      Color `Gray` cited from "the overall colour scheme is grey and black"

    Both were refused, both are the same value written differently, and both cost
    a person a question. What is *not* accepted is anything that changes the
    referent: elastane and elastodiene are still different fibres, Beats and
    Apple are still different brands, and Gray is still not Black.
    """
    folded = text.casefold()
    if value.casefold() in folded:
        return value
    for alias in VALUE_SYNONYMS.get(value.casefold(), ()):
        if alias in folded:
            return alias
    # The same word, spelled the other way.
    if _same_spelling(value) in _same_spelling(text):
        return value
    # The same words, run together or split apart. Only for a value that has a
    # space in it, so this asks one question -- "is this multi-word value written
    # as one word?" -- and cannot start matching single tokens inside longer
    # ones, where "Gap" would find itself in "flagship".
    if " " in value.strip():
        squeezed = _letters_only(value)
        if squeezed and squeezed in _letters_only(text):
            return value
    return None


def synonym_for(read_term: str, allowed_values: tuple[str, ...]) -> str | None:
    """The allowed value denoting the same thing as a term read off the item."""
    folded = read_term.casefold()
    for allowed in allowed_values:
        if folded in VALUE_SYNONYMS.get(allowed.casefold(), ()):
            return allowed
    return None


def detect_value_substitution(
    aspect_name: str,
    value: str,
    cited_text: str,
    allowed_values: tuple[str, ...],
) -> str | None:
    """Catch an allowed value quietly replacing the one actually read.

    The case this exists for: an observation transcribed "4% Elastane" from a swing
    tag, and the mapping returned Material = Elastodiene. Both are legal eBay
    values, the citation was real, and elastodiene is a chemically different fibre --
    rubber-based rather than polyurethane. Nothing else in the pipeline could catch
    it: the value was permitted, the evidence existed, and only someone who knows
    textiles would notice.

    The rule is narrow on purpose. It fires only when the proposed value is absent
    from the cited evidence AND a *different* allowed value is present in it, which
    is the signature of a swap rather than of paraphrase or inference. "Navy" cited
    from "navy blue" is fine; "Men" from "men's jacket" is fine; anything the
    evidence does not name at all is left to the other checks.
    """
    if not allowed_values:
        return None
    haystack = cited_text.casefold()
    # A value the evidence names under a different word is not a substitution. The
    # tag reads "Elastane" and eBay calls it "Spandex"; the fibre is the same one.
    if value_appears_in(value, cited_text):
        return None

    alternatives = [
        allowed for allowed in allowed_values
        if allowed.casefold() != value.casefold()
        and len(allowed) > 3
        and value_appears_in(allowed, cited_text)
    ]
    if not alternatives:
        return None
    # Name every allowed value the evidence does contain, rather than the first one
    # found: on a multi-valued aspect the others are usually the sibling values, and
    # picking one arbitrarily would point at the wrong thing.
    named = ", ".join(repr(a) for a in alternatives)
    return (
        f"{aspect_name}: {value!r} does not appear in the cited evidence, which names "
        f"{named}. All are allowed values, so nothing else would catch this -- where "
        f"they mean different things, the evidence wins."
    )


def detect_uncited_value(
    aspect_name: str,
    value: str,
    cited_ids: tuple[int, ...],
    text_by_id: dict[int, str],
) -> str | None:
    """Catch a value whose support is in evidence other than the evidence cited.

    The case this exists for: MP-000005 is a Beats speaker. Three observations say
    so outright -- two read the brand off the front, one names it as an inference.
    The mapping proposed Brand correctly and cited the regulatory panel on the
    bottom, which reads "Apple Inc.". Apple does own Beats, so the citation is not
    a random one; it is a fact about a related entity that only supports the value
    if you already know how the two are connected. Knowledge from outside these
    observations is exactly what a citation is supposed to make unnecessary.

    Narrow on purpose, and narrower than it looks: it fires only when *no* cited
    observation names the value and *some* uncited one does. That pairing is what
    makes it a citation error rather than a judgement call -- a better citation was
    demonstrably available and sitting in the same set.

    A genuinely inferred value that nothing names literally is left alone; so is a
    candidate citing evidence whose text we cannot read, which fails open rather
    than inventing a rejection out of a missing lookup. Unlike
    `detect_value_substitution` this needs no allowed-value list, so it is the only
    citation check that covers a free-text aspect -- which is most of them.

    Asked before `detect_value_substitution`, because a case that trips both is a
    miscitation and not a swap: the substitution message would name the related
    entity as the value the evidence supports, which is the opposite of true.

    The caller asks this only about an aspect with a single candidate, and that
    restriction is not incidental -- see `map_aspects`. Two readings offered from
    one hedged observation is the model declining to choose, and dropping whichever
    one some other observation happens to name would convert an ambiguity into a
    confident answer.
    """
    if not cited_ids or any(cited_id not in text_by_id for cited_id in cited_ids):
        return None
    cited_text = " ".join(text_by_id[cited_id] for cited_id in cited_ids)
    if value_appears_in(value, cited_text):
        return None

    supporting = [
        evidence_id for evidence_id, text in sorted(text_by_id.items())
        if evidence_id not in cited_ids and value_appears_in(value, text)
    ]
    if not supporting:
        return None
    named = ", ".join(str(evidence_id) for evidence_id in supporting)
    return (
        f"{aspect_name}: {value!r} is not named by the cited evidence "
        f"{list(cited_ids)}, but is named by {named}. Cite the observation that "
        f"states the value; a fact about a related thing only supports it if you "
        f"already know how the two connect, and that knowledge is not in evidence."
    )


def supported_generalisation(
    value: str, cited_text: str, allowed_values: tuple[str, ...]
) -> str | None:
    """The same value with the unsupported part removed, if the evidence has it.

    MP-000041 proposed Material = `100% Polyester` citing a tag that reads
    "Polyester". Refusing that is right -- "100%" is a composition claim and
    nothing observed it -- but the aspect was answerable all along, and instead a
    person was asked and typed "recycled origin", which is worse than the value
    the machine already had.

    Strictly narrower, never a different value: the fallback has to be an allowed
    value that the proposal *contains* and that the cited evidence names. So
    `100% Polyester` may fall back to `Polyester`, and `Gray` may not fall back
    to `Black` -- that is a swap, and `detect_value_substitution` is right to
    refuse it rather than quietly answer a different question.

    The longest such value wins, so `Full Grain Leather` prefers `Grain Leather`
    over `Leather` when the evidence supports both.
    """
    if not allowed_values:
        return None
    proposed = value.casefold()
    weaker = [
        allowed for allowed in allowed_values
        if allowed.casefold() != proposed
        and allowed.casefold() in proposed
        and value_appears_in(allowed, cited_text)
    ]
    if not weaker:
        return None
    return max(weaker, key=len)


def missing_synonyms(cited_text: str, allowed_values: tuple[str, ...],
                     proposed: set[str]) -> list[str]:
    """Allowed values the evidence names under another word but nobody proposed.

    The gap this closes: the tag says Elastane, eBay offers Spandex, and the model
    proposed neither -- so a fibre that is genuinely present and genuinely listable
    simply vanished. Reported rather than added, because deciding an item is made of
    something is not a thing to do silently.
    """
    folded = {p.casefold() for p in proposed}
    found = []
    for allowed in allowed_values:
        if allowed.casefold() in folded:
            continue
        alias = value_appears_in(allowed, cited_text)
        if alias and alias.casefold() != allowed.casefold():
            found.append(f"{allowed} (the evidence says {alias!r})")
    return found
