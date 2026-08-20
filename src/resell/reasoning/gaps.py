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

    def __post_init__(self) -> None:
        if not self.values and self.value:
            object.__setattr__(self, "values", (self.value,))

    @property
    def blocking(self) -> bool:
        return self.resolution in BLOCKING_RESOLUTIONS


def resolve_aspect(
    aspect_name: str, candidates: list[Candidate], *, cardinality: str = "SINGLE"
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
        return AspectOutcome(
            aspect_name, Resolution.UNSUPPORTED, None, tuple(candidates),
            f"no evidence supports any value for {aspect_name!r}{dropped_note}",
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


class GapAction(StrEnum):
    """What would close the gap. Determines how the question is worded."""

    ASK_OPERATOR = "ask_operator"
    REQUEST_PHOTO = "request_photo"
    REQUEST_MEASUREMENT = "request_measurement"
    RESEARCH = "research"


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

    return Gap(
        outcome.aspect_name, outcome.resolution, GapAction.ASK_OPERATOR,
        f"Nothing observed supports a value for {outcome.aspect_name}. "
        f"Can you supply it, or point to where on the item it appears?",
    )


def analyse(
    required_aspects: list[str],
    candidates_by_aspect: dict[str, list[Candidate]],
    cardinality_by_aspect: dict[str, str] | None = None,
) -> tuple[list[AspectOutcome], list[Gap]]:
    """Resolve every required aspect and collect the gaps."""
    cardinality_by_aspect = cardinality_by_aspect or {}
    outcomes = [
        resolve_aspect(
            name, candidates_by_aspect.get(name, []),
            cardinality=cardinality_by_aspect.get(name, "SINGLE"),
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


def mode_is_supported(
    mode: IdentificationMode,
    *,
    effort: IdentificationEffort,
    negative_finding: NegativeFinding | None,
    brand_support: tuple[EvidenceRef, ...] = (),
    identifier_support: tuple[EvidenceRef, ...] = (),
) -> tuple[bool, str]:
    """Check that a declared mode is backed by what it claims.

    Modes are conclusions and must be earned in both directions: exact_product
    needs a positive identifier, and described_object needs a negative finding.
    """
    if mode is IdentificationMode.UNRESOLVED:
        return True, "unresolved makes no claim"

    if mode is IdentificationMode.EXACT_PRODUCT and not identifier_support:
        return False, (
            "exact_product asserts a specific manufacturer product; cite an "
            "identifier or a catalogue match, or use product_family"
        )
    if mode is IdentificationMode.PRODUCT_FAMILY and not brand_support:
        return False, "product_family needs a cited brand"
    if mode is IdentificationMode.BRANDED_GENERIC and not brand_support:
        return False, "branded_generic needs a cited brand"

    if mode in MODES_REQUIRING_NEGATIVE_FINDING:
        ok, why = negative_finding_sufficient(effort, negative_finding)
        if not ok:
            return False, f"{mode}: {why}"
        return True, f"{mode}: {why}"

    return True, f"{mode} is supported"


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
