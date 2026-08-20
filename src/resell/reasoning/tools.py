"""Tool contracts for the reasoning stages.

One tool per stage, each with a JSON schema the model fills. Parsing produces
*typed proposals* and nothing else -- no database, no side effects. The gateway
decides what becomes evidence.

That separation is the point. A tool call is the model speaking in a constrained
vocabulary; it is not the model writing to storage. Everything it says still faces
the same validation an operator's input would.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from resell.reasoning.schema import (
    Basis,
    IdentifierObservation,
    IdentifierScheme,
    MeasurementMethod,
    NegativeFinding,
    Observation,
    Subject,
)

# Bases the observation stage may use. Excludes operator (not the model's to claim)
# and external_source (research is a later stage with its own tool).
OBSERVATION_BASES = (
    str(Basis.TEXT_READ),
    str(Basis.VISUAL_OBSERVATION),
    str(Basis.MEASUREMENT),
    str(Basis.INFERENCE),
)

OBSERVE_TOOL_NAME = "record_observations"

OBSERVE_TOOL_SCHEMA: dict[str, Any] = {
    "name": OBSERVE_TOOL_NAME,
    "description": "Record observations of one second-hand item from its photographs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "observations": {
                "type": "array",
                "description": (
                    "Array of discrete claims. One claim per entry; split compound "
                    "statements."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {
                            "type": "string",
                            "description": "A single factual statement about the item.",
                        },
                        "basis": {
                            "type": "string",
                            "enum": list(OBSERVATION_BASES),
                            "description": "Required. See the system prompt for each meaning.",
                        },
                        "photo_positions": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Photo positions supporting this claim.",
                        },
                        "surface": {
                            "type": "string",
                            "description": "Where on the object, e.g. 'interior label'.",
                        },
                        "measurement_method": {
                            "type": "string",
                            "enum": [str(m) for m in MeasurementMethod],
                            "description": "Required when basis is measurement.",
                        },
                        "confidence": {"type": "number", "description": "0-1, diagnostic only."},
                    },
                    "required": ["claim", "basis"],
                },
            },
            "identifiers": {
                "type": "array",
                # Kept short deliberately. A long description here coincided with
                # the model serialising `observations` into a JSON string rather
                # than emitting an array; the detail lives in the system prompt,
                # where it cannot affect how tool arguments are encoded.
                "description": (
                    "Required. Every code legible in the photos: style, product, "
                    "article, factory, barcode, model, RN/CA, serial. Transcribe "
                    "exactly; use `other` if unsure of the scheme. An empty array "
                    "asserts that no code appears anywhere in the photographs."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "scheme": {
                            "type": "string",
                            "enum": [str(s) for s in IdentifierScheme],
                        },
                        "raw_transcription": {"type": "string"},
                        "photo_position": {"type": "integer"},
                        "surface": {"type": "string"},
                    },
                    "required": ["scheme", "raw_transcription", "photo_position"],
                },
            },
            "identity_search": {
                "type": "object",
                "description": "What you examined while looking for marks and labels.",
                "properties": {
                    "surfaces_examined": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Named surfaces you were able to inspect.",
                    },
                    "photos_reviewed": {"type": "integer"},
                    "note": {"type": "string", "description": "What was or was not present."},
                },
                "required": ["photos_reviewed"],
            },
        },
        # identifiers is required. It was optional for two runs, and the model
        # satisfied the contract by omitting it entirely while transcribing four
        # product codes into prose. No amount of prompt emphasis fixes a field the
        # schema says is optional; an empty array is now an explicit assertion
        # rather than an absence.
        "required": ["observations", "identifiers", "identity_search"],
    },
}


@dataclass
class ObservationProposal:
    """What the model proposed. Persisted only after the gateway accepts it."""

    observations: list[Observation] = field(default_factory=list)
    identifiers: list[IdentifierObservation] = field(default_factory=list)
    negative_finding: NegativeFinding | None = None
    malformed: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.observations) + len(self.identifiers)


# Fields that mark a dict as one entry rather than a container of entries.
_ENTRY_MARKERS = frozenset({"claim", "basis", "scheme", "raw_transcription"})


def _looks_like_entry(value: dict) -> bool:
    return bool(_ENTRY_MARKERS & set(value))


def _as_list(value: object, label: str, malformed: list[str]) -> list:
    """Coerce a container to a list, recording anything unusable.

    Providers vary in how faithfully they honour a schema, and a model can return a
    stringified array or a bare string where an array was specified. None of that
    should crash a parse: the point of this layer is that a badly-shaped response
    costs the entries it broke, not the whole pass and not the trace.
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            malformed.append(f"{label}: expected a list, got an unparseable string")
            return []
    if isinstance(value, dict):
        # A single entry sent unwrapped is recoverable -- but only if it looks like
        # an entry. A dict of entries keyed by index or name would otherwise be
        # wrapped into one unusable element, turning a diagnosable structural
        # problem into a misleading "unusable basis None".
        if _looks_like_entry(value):
            return [value]
        malformed.append(
            f"{label}: expected a list, got an object with keys "
            f"{sorted(value)[:8]} -- entries may have been keyed rather than listed"
        )
        return [entry for entry in value.values() if isinstance(entry, dict)]
    if not isinstance(value, list):
        malformed.append(f"{label}: expected a list, got {type(value).__name__}")
        return []
    return value


def parse_observe_tool_input(payload: object) -> ObservationProposal:
    """Turn a tool call into typed proposals. Never raises.

    Tolerant of individual malformed entries: one unusable observation should not
    discard a whole pass. Each is collected in `malformed` so the failure is visible
    rather than silently dropped.

    Total by design. An earlier version guarded KeyError and ValueError but not
    TypeError, so a response whose `observations` held strings rather than objects
    crashed the parse -- which also meant the call was never recorded and its cost
    never counted, despite having been paid for.
    """
    proposal = ObservationProposal()

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            proposal.malformed.append("tool input was an unparseable string")
            return proposal
    if not isinstance(payload, dict):
        proposal.malformed.append(
            f"tool input was {type(payload).__name__}, expected an object"
        )
        return proposal

    for index, raw in enumerate(_as_list(payload.get("observations"), "observations", proposal.malformed)):
        if not isinstance(raw, dict):
            proposal.malformed.append(
                f"observation {index}: expected an object, got "
                f"{type(raw).__name__} ({str(raw)[:60]!r})"
            )
            continue
        try:
            basis = Basis(raw["basis"])
        except (KeyError, ValueError, TypeError):
            # Report the keys present, not just the missing one. "unusable basis
            # None" is true but says nothing about what actually arrived, which is
            # the question worth answering when a contract drifts.
            proposal.malformed.append(
                f"observation {index}: unusable basis {raw.get('basis')!r} "
                f"(keys present: {sorted(raw)[:8]})"
            )
            continue
        method = raw.get("measurement_method")
        try:
            measurement_method = MeasurementMethod(method) if method else None
        except (ValueError, TypeError):
            proposal.malformed.append(
                f"observation {index}: unknown measurement_method {method!r}"
            )
            continue

        positions = raw.get("photo_positions")
        try:
            photo_positions = tuple(int(p) for p in (positions or ()))
        except (TypeError, ValueError):
            proposal.malformed.append(
                f"observation {index}: unusable photo_positions {positions!r}"
            )
            continue

        confidence = raw.get("confidence")
        if confidence is not None and not isinstance(confidence, (int, float)):
            confidence = None

        proposal.observations.append(
            Observation(
                claim=str(raw.get("claim", "")),
                basis=basis,
                subject=Subject.THIS_ITEM,
                photo_positions=photo_positions,
                confidence=confidence,
                measurement_method=measurement_method,
                surface=raw.get("surface") if isinstance(raw.get("surface"), str) else None,
            )
        )

    for index, raw in enumerate(_as_list(payload.get("identifiers"), "identifiers", proposal.malformed)):
        if not isinstance(raw, dict):
            proposal.malformed.append(
                f"identifier {index}: expected an object, got {type(raw).__name__}"
            )
            continue
        try:
            scheme = IdentifierScheme(raw["scheme"])
        except (KeyError, ValueError, TypeError):
            proposal.malformed.append(
                f"identifier {index}: unknown scheme {raw.get('scheme')!r}"
            )
            continue
        transcription = str(raw.get("raw_transcription", "")).strip()
        if not transcription:
            proposal.malformed.append(f"identifier {index}: empty transcription")
            continue
        try:
            position = int(raw.get("photo_position", 0))
        except (TypeError, ValueError):
            position = 0
        proposal.identifiers.append(
            IdentifierObservation(
                scheme=scheme,
                raw_transcription=transcription,
                photo_position=position,
                surface=raw.get("surface") if isinstance(raw.get("surface"), str) else None,
            )
        )

    search = payload.get("identity_search")
    if isinstance(search, str):
        try:
            search = json.loads(search)
        except ValueError:
            # Setting it aside silently would lose the fact that the model reported
            # *something* about its identity search, which is exactly what
            # distinguishes "nothing there" from "did not look".
            proposal.malformed.append(
                f"identity_search: unparseable string ({search[:60]!r})"
            )
            search = None
    if isinstance(search, dict):
        surfaces = search.get("surfaces_examined")
        try:
            reviewed = int(search.get("photos_reviewed", 0))
        except (TypeError, ValueError):
            reviewed = 0
        proposal.negative_finding = NegativeFinding(
            surfaces_examined=tuple(
                str(s) for s in (surfaces if isinstance(surfaces, list) else ())
            ),
            photos_reviewed=reviewed,
            operator_confirmed=False,  # never the model's to assert
            note=str(search.get("note", "")),
        )
    elif search is not None:
        proposal.malformed.append(
            f"identity_search: expected an object, got {type(search).__name__}"
        )

    return proposal


# --- deterministic backstop --------------------------------------------------

# A run of characters long enough and mixed enough to be a code rather than a word.
_CODE_LIKE = __import__("re").compile(r"\b(?=[A-Z0-9][A-Z0-9\-/]{4,})(?=[^\s]*\d)[A-Z0-9][A-Z0-9\-/]{4,}\b")

# Words that match the shape but are not identifiers.
_CODE_NOISE = frozenset({
    "MADE", "EGYPT", "COTTON", "WOOL", "SPRING", "SUMMER", "WINTER", "AUTUMN",
    "SMALL", "LARGE", "MEDIUM", "XLARGE", "SLIM", "CLEAN", "WATER",
})


def unstructured_identifier_candidates(proposal: ObservationProposal) -> list[tuple[str, str]]:
    """Code-like strings transcribed as prose but not recorded as identifiers.

    A model can satisfy the observation contract perfectly while leaving every
    product code inside a `text_read` claim, where it is never check-digit verified
    and never reaches eBay's dedicated identifier fields. That happened on the first
    real run: a style code, a product code and a barcode number all arrived as prose.

    This is a reporting signal, not a gate -- the regex will have false positives,
    and refusing on a heuristic would be worse than the problem. It exists so the
    omission is visible rather than silent.
    """
    recorded = " ".join(
        f"{i.raw_transcription} {i.normalized}" for i in proposal.identifiers
    ).upper()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for observation in proposal.observations:
        if observation.basis is not Basis.TEXT_READ:
            continue
        for match in _CODE_LIKE.findall(observation.claim.upper()):
            token = match.strip("-/")
            if len(token) < 5 or token in _CODE_NOISE or token in seen:
                continue
            if token in recorded:
                continue
            seen.add(token)
            found.append((token, observation.claim))
    return found


# --- stage 2: aspect mapping -------------------------------------------------

MAP_TOOL_NAME = "map_aspects"

# Strings that express absence rather than a value. A model asked for a value it
# cannot supply will sometimes invent a placeholder and cite an observation saying
# the thing is not visible -- which is a negative observation, evidence of absence
# rather than support for a value. `<UNKNOWN>` reached a resolved aspect that way
# and would have gone into a listing verbatim.
#
# Note what is NOT here: "Does not apply" and "Unbranded" are real eBay aspect
# values with real meaning, and must not be filtered.
PLACEHOLDER_VALUES = frozenset({
    "unknown", "n/a", "na", "none", "null", "nil", "-", "--", "?", "tbd",
    "not specified", "unspecified", "not visible", "not legible", "not applicable",
    "not stated", "unavailable", "no value", "empty", "blank",
})


def is_placeholder(value: str) -> bool:
    stripped = value.strip()
    # Angle brackets are how models signal a slot they could not fill.
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    return stripped.casefold() in PLACEHOLDER_VALUES

# Kept deliberately lean. A 3,237-byte schema coincided with the model serialising
# its arguments into a string; the reasoning belongs in the system prompt.
MAP_TOOL_SCHEMA: dict[str, Any] = {
    "name": MAP_TOOL_NAME,
    "description": "Map recorded observations onto a marketplace's aspect form.",
    "input_schema": {
        "type": "object",
        "properties": {
            "aspects": {
                "type": "array",
                "description": (
                    "One entry per aspect in the form. Include every aspect, even "
                    "those nothing supports."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "aspect_name": {
                            "type": "string",
                            "description": "Exactly as given in the form.",
                        },
                        "unsupported_reason": {
                            "type": "string",
                            "enum": [
                                "not_observed", "not_applicable", "none_apply",
                                "insufficient_evidence",
                            ],
                            "description": (
                                "Required when candidates is empty. not_observed: the "
                                "photos say nothing about it. not_applicable: it does "
                                "not apply to this kind of object. none_apply: the "
                                "evidence describes it but no allowed value is "
                                "truthful. insufficient_evidence: partly observed, not "
                                "enough to name a value."
                            ),
                        },
                        "candidates": {
                            "type": "array",
                            "description": (
                                "Values the observations support. Empty when nothing "
                                "does. Include every materially supported reading, "
                                "not just your preferred one."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "value": {"type": "string"},
                                    "evidence_ids": {
                                        "type": "array",
                                        "items": {"type": "integer"},
                                        "description": (
                                            "Observation ids supporting this value. "
                                            "Required and non-empty."
                                        ),
                                    },
                                    "reasoning": {"type": "string"},
                                },
                                "required": ["value", "evidence_ids"],
                            },
                        },
                    },
                    "required": ["aspect_name", "candidates"],
                },
            }
        },
        "required": ["aspects"],
    },
}


@dataclass
class MappingProposal:
    """Candidate sets per aspect. Persisted only after the gateway accepts them."""

    candidates_by_aspect: dict[str, list] = field(default_factory=dict)
    reasons_by_aspect: dict[str, object] = field(default_factory=dict)
    reasoning_by_value: dict[tuple[str, str], str] = field(default_factory=dict)
    # Values resting on external evidence, so they can be marked at approval rather
    # than blending in with what was observed on the item itself.
    donated_by_value: dict[tuple[str, str], tuple[int, ...]] = field(default_factory=dict)
    malformed: list[str] = field(default_factory=list)


def parse_map_tool_input(
    payload: object,
    *,
    valid_evidence_ids: set[int],
    citable_candidates: dict[int, str] | None = None,
) -> MappingProposal:
    """Turn a mapping tool call into candidate sets. Never raises.

    Citations are checked against the evidence actually in scope. A value citing an
    id that does not exist, or that belongs to another item or an earlier run, is
    dropped rather than trusted -- an invented citation is worse than an absent one
    because it looks like support.

    `citable_candidates` maps candidate-product evidence ids to the donation scope a
    match permits. Candidate evidence absent from it is refused exactly as an
    invented citation is: the donation gate is the same mechanism as every other
    citation rule, not a parallel one that could disagree with it.
    """
    from resell.reasoning.research import DonationScope, aspect_is_specific, may_cite_candidate

    citable_candidates = citable_candidates or {}
    from resell.reasoning.gaps import Candidate
    from resell.reasoning.schema import Basis, EvidenceRef

    proposal = MappingProposal()

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            proposal.malformed.append("tool input was an unparseable string")
            return proposal
    if not isinstance(payload, dict):
        proposal.malformed.append(
            f"tool input was {type(payload).__name__}, expected an object"
        )
        return proposal

    for index, raw in enumerate(_as_list(payload.get("aspects"), "aspects", proposal.malformed)):
        if not isinstance(raw, dict):
            proposal.malformed.append(f"aspect {index}: expected an object")
            continue
        name = raw.get("aspect_name")
        if not isinstance(name, str) or not name.strip():
            proposal.malformed.append(f"aspect {index}: missing aspect_name")
            continue
        name = name.strip()

        candidates = []
        for position, entry in enumerate(
            _as_list(raw.get("candidates"), f"{name} candidates", proposal.malformed)
        ):
            if not isinstance(entry, dict):
                proposal.malformed.append(f"{name} candidate {position}: expected an object")
                continue
            value = entry.get("value")
            if not isinstance(value, str) or not value.strip():
                proposal.malformed.append(f"{name} candidate {position}: empty value")
                continue
            if is_placeholder(value):
                proposal.malformed.append(
                    f"{name} candidate {value.strip()!r}: a placeholder is not a value; "
                    f"an aspect nothing supports should have no candidates"
                )
                continue

            cited = entry.get("evidence_ids")
            ids: list[int] = []
            for item in cited if isinstance(cited, list) else []:
                try:
                    ids.append(int(item))
                except (TypeError, ValueError):
                    continue

            unknown = [
                i for i in ids
                if i not in valid_evidence_ids and i not in citable_candidates
            ]
            if unknown:
                proposal.malformed.append(
                    f"{name} candidate {value!r}: cites evidence {unknown} which is not "
                    f"in scope for this item"
                )

            # Candidate-product evidence is admissible only as far as a match permits.
            refused = []
            for evidence_id in list(ids):
                if evidence_id not in citable_candidates:
                    continue
                scope = DonationScope(citable_candidates[evidence_id])
                allowed, why = may_cite_candidate(
                    scope, aspect_is_specific=aspect_is_specific(name)
                )
                if not allowed:
                    refused.append((evidence_id, why))
            if refused:
                proposal.malformed.append(
                    f"{name} candidate {value!r}: candidate evidence "
                    f"{[i for i, _ in refused]} may not be cited here -- {refused[0][1]}"
                )
                ids = [i for i in ids if i not in {i for i, _ in refused}]

            ids = [i for i in ids if i in valid_evidence_ids or i in citable_candidates]
            if not ids:
                proposal.malformed.append(
                    f"{name} candidate {value!r}: no usable citation, discarded"
                )
                continue

            # Basis is read back from storage during resolution; the placeholder here
            # only carries the id.
            candidates.append(
                Candidate(
                    value=value.strip(),
                    support=tuple(EvidenceRef(i, Basis.INFERENCE) for i in ids),
                )
            )
            donated = [i for i in ids if i in citable_candidates]
            if donated:
                proposal.donated_by_value[(name, value.strip())] = tuple(donated)
            if isinstance(entry.get("reasoning"), str):
                proposal.reasoning_by_value[(name, value.strip())] = entry["reasoning"]

        proposal.candidates_by_aspect[name] = candidates

        if not candidates:
            from resell.reasoning.gaps import UnsupportedReason

            raw_reason = raw.get("unsupported_reason")
            try:
                proposal.reasons_by_aspect[name] = UnsupportedReason(raw_reason)
            except (ValueError, TypeError):
                # Not fatal -- the aspect is still correctly unsupported. But without
                # the reason we cannot tell "not photographed" from "no truthful value
                # exists in this category", and those need opposite responses.
                proposal.malformed.append(
                    f"{name}: no candidates and no usable unsupported_reason "
                    f"({raw_reason!r}); the gap cannot be classified"
                )

    return proposal


# --- stage 3: research planning ----------------------------------------------

PLAN_TOOL_NAME = "plan_research"

# The planner's most valuable answer is often an empty plan. `sufficient` exists so
# "I have enough to call this branded_generic, and more searching will not change
# that" is a first-class output rather than something inferred from silence.
PLAN_TOOL_SCHEMA: dict[str, Any] = {
    "name": PLAN_TOOL_NAME,
    "description": "Decide what external lookups, if any, would improve identification.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assessment": {
                "type": "object",
                "properties": {
                    "sufficient": {
                        "type": "boolean",
                        "description": (
                            "True when existing evidence already supports the best "
                            "identification available and further searching is "
                            "unlikely to improve it. Say so plainly; an empty plan "
                            "with a reason is a good answer."
                        ),
                    },
                    "proposed_mode": {
                        "type": "string",
                        "enum": ["exact_product", "product_family", "branded_generic",
                                 "described_object", "unresolved"],
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["sufficient", "proposed_mode", "rationale"],
            },
            "lookups": {
                "type": "array",
                "description": (
                    "Targeted lookups, most valuable first -- the list may be trimmed "
                    "to fit the budget, so order matters. Empty when sufficient."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "source_kind": {
                            "type": "string",
                            "enum": ["manufacturer", "reference", "general_web"],
                            "description": "Where this should be looked up.",
                        },
                        "motivation": {
                            "type": "string",
                            "description": "What this would settle, and why it is worth a lookup.",
                        },
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Observations that motivate this lookup. Required: a "
                                "search with nothing behind it is browsing."
                            ),
                        },
                        "expected_to_resolve": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Aspect names or questions this may settle.",
                        },
                    },
                    "required": ["query", "source_kind", "motivation", "evidence_ids"],
                },
            },
        },
        "required": ["assessment", "lookups"],
    },
}


@dataclass
class PlannedLookup:
    query: str
    source_kind: str
    motivation: str
    evidence_ids: tuple[int, ...]
    expected_to_resolve: tuple[str, ...] = ()


@dataclass
class ResearchPlan:
    sufficient: bool = False
    proposed_mode: str = "unresolved"
    rationale: str = ""
    lookups: list[PlannedLookup] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)


def parse_plan_tool_input(
    payload: object, *, valid_evidence_ids: set[int], already_searched: set[str] | None = None
) -> ResearchPlan:
    """Turn a planning tool call into a validated plan. Never raises.

    Two rules enforced here rather than hoped for: a lookup must cite the
    observations that motivate it, and a query already performed for this item is
    dropped. The first is what makes this planning rather than browsing; the second
    stops a loop paying twice for the same answer.
    """
    already_searched = {q.casefold() for q in (already_searched or set())}
    plan = ResearchPlan()

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            plan.malformed.append("tool input was an unparseable string")
            return plan
    if not isinstance(payload, dict):
        plan.malformed.append(f"tool input was {type(payload).__name__}, expected an object")
        return plan

    assessment = payload.get("assessment")
    if isinstance(assessment, dict):
        plan.sufficient = bool(assessment.get("sufficient"))
        plan.proposed_mode = str(assessment.get("proposed_mode", "unresolved"))
        plan.rationale = str(assessment.get("rationale", ""))
        if not plan.rationale.strip():
            plan.malformed.append("assessment: no rationale given")
    else:
        plan.malformed.append("assessment missing; cannot tell whether searching is warranted")

    for index, raw in enumerate(_as_list(payload.get("lookups"), "lookups", plan.malformed)):
        if not isinstance(raw, dict):
            plan.malformed.append(f"lookup {index}: expected an object")
            continue
        query = str(raw.get("query", "")).strip()
        if not query:
            plan.malformed.append(f"lookup {index}: empty query")
            continue
        if query.casefold() in already_searched:
            plan.malformed.append(
                f"lookup {index}: {query!r} was already performed for this item; skipped"
            )
            continue

        cited = raw.get("evidence_ids")
        ids = []
        for item in cited if isinstance(cited, list) else []:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        unknown = [i for i in ids if i not in valid_evidence_ids]
        if unknown:
            plan.malformed.append(
                f"lookup {index} ({query!r}): cites evidence {unknown} not in scope"
            )
        ids = [i for i in ids if i in valid_evidence_ids]
        if not ids:
            plan.malformed.append(
                f"lookup {index} ({query!r}): no observation motivates this search; "
                f"a lookup with nothing behind it is browsing, not planning"
            )
            continue

        expected = raw.get("expected_to_resolve")
        plan.lookups.append(
            PlannedLookup(
                query=query,
                source_kind=str(raw.get("source_kind", "general_web")),
                motivation=str(raw.get("motivation", "")),
                evidence_ids=tuple(ids),
                expected_to_resolve=tuple(
                    str(x) for x in (expected if isinstance(expected, list) else ())
                ),
            )
        )

    if plan.sufficient and plan.lookups:
        plan.malformed.append(
            "assessment says the evidence is sufficient but lookups were still "
            "proposed; treating the lookups as the intent"
        )
        plan.sufficient = False

    return plan


# --- stage 4: candidate matching ---------------------------------------------

MATCH_TOOL_NAME = "judge_candidates"

# `any_match: false` is a result, not a failure. A loop that always selects
# something will always find something, and the thing it finds will increasingly be
# whatever it was hoping for.
MATCH_TOOL_SCHEMA: dict[str, Any] = {
    "name": MATCH_TOOL_NAME,
    "description": "Judge whether retrieved candidate products are this item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assessment": {
                "type": "object",
                "properties": {
                    "any_match": {
                        "type": "boolean",
                        "description": (
                            "False when no candidate is this item. That is a correct "
                            "and useful answer."
                        ),
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["any_match", "rationale"],
            },
            "claims": {
                "type": "array",
                "description": (
                    "One entry per candidate examined, whether or not it matches. "
                    "Record the non-matches: knowing a candidate was considered and "
                    "ruled out is worth keeping."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_ref": {"type": "string"},
                        "is_match": {"type": "boolean"},
                        "strength": {
                            "type": "string",
                            "enum": ["identifier_verified", "identifier_asserted",
                                     "attribute_convergence", "similarity"],
                            "description": "Required when is_match is true.",
                        },
                        "rationale": {"type": "string"},
                        "item_evidence": {
                            "type": "array", "items": {"type": "integer"},
                            "description": "Observations of the physical item. Required.",
                        },
                        "candidate_evidence": {
                            "type": "array", "items": {"type": "integer"},
                            "description": "Facts about the candidate product. Required.",
                        },
                        "ruled_out_by": {
                            "type": "array", "items": {"type": "string"},
                            "description": (
                                "For a non-match: the specific conflicts. "
                                "'candidate is a 3-button, the item is 2-button'."
                            ),
                        },
                    },
                    "required": ["candidate_ref", "is_match", "rationale",
                                 "item_evidence", "candidate_evidence"],
                },
            },
        },
        "required": ["assessment", "claims"],
    },
}


@dataclass
class MatchProposal:
    any_match: bool = False
    rationale: str = ""
    claims: list = field(default_factory=list)      # list[MatchClaim]
    malformed: list[str] = field(default_factory=list)

    @property
    def matches(self) -> list:
        return [claim for claim in self.claims if claim.is_match]

    @property
    def non_matches(self) -> list:
        return [claim for claim in self.claims if not claim.is_match]


def parse_match_tool_input(
    payload: object,
    *,
    valid_item_evidence: set[int],
    valid_candidate_evidence: set[int],
) -> MatchProposal:
    """Turn a matching tool call into validated claims. Never raises.

    Both sides of every claim are checked against the evidence that actually exists,
    matches and non-matches alike. A non-match citing nothing is as empty as a match
    citing nothing -- "this isn't it" is only useful if it says what conflicts.
    """
    from resell.reasoning.research import MatchClaim, MatchStrength, SourceAuthority

    proposal = MatchProposal()

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            proposal.malformed.append("tool input was an unparseable string")
            return proposal
    if not isinstance(payload, dict):
        proposal.malformed.append(f"tool input was {type(payload).__name__}, expected an object")
        return proposal

    assessment = payload.get("assessment")
    if isinstance(assessment, dict):
        proposal.any_match = bool(assessment.get("any_match"))
        proposal.rationale = str(assessment.get("rationale", ""))
        if not proposal.rationale.strip():
            proposal.malformed.append("assessment: no rationale given")
    else:
        proposal.malformed.append("assessment missing")

    for index, raw in enumerate(_as_list(payload.get("claims"), "claims", proposal.malformed)):
        if not isinstance(raw, dict):
            proposal.malformed.append(f"claim {index}: expected an object")
            continue
        candidate_ref = str(raw.get("candidate_ref", "")).strip()
        if not candidate_ref:
            proposal.malformed.append(f"claim {index}: no candidate_ref")
            continue

        is_match = bool(raw.get("is_match"))
        strength = MatchStrength.SIMILARITY
        if is_match:
            try:
                strength = MatchStrength(raw.get("strength"))
            except (ValueError, TypeError):
                proposal.malformed.append(
                    f"claim {index} ({candidate_ref}): a match needs a strength, got "
                    f"{raw.get('strength')!r}"
                )
                continue

        def cited(key: str, valid: set[int]) -> tuple[list[int], list[int]]:
            raw_ids = raw.get(key)
            ids = []
            for item in raw_ids if isinstance(raw_ids, list) else []:
                try:
                    ids.append(int(item))
                except (TypeError, ValueError):
                    continue
            return [i for i in ids if i in valid], [i for i in ids if i not in valid]

        item_ids, bad_item = cited("item_evidence", valid_item_evidence)
        candidate_ids, bad_candidate = cited("candidate_evidence", valid_candidate_evidence)
        if bad_item or bad_candidate:
            proposal.malformed.append(
                f"claim {index} ({candidate_ref}): cites evidence not in scope "
                f"(item {bad_item}, candidate {bad_candidate})"
            )

        claim = MatchClaim(
            candidate_ref=candidate_ref,
            strength=strength,
            # Authority comes from the retrieved document, never from the model:
            # it is a property of where the page came from, not a judgment.
            authority=SourceAuthority.UNKNOWN,
            rationale=str(raw.get("rationale", "")),
            item_evidence=tuple(item_ids),
            candidate_evidence=tuple(candidate_ids),
            is_match=is_match,
        )
        problems = claim.problems()
        if problems:
            proposal.malformed.append(
                f"claim {index} ({candidate_ref}): " + "; ".join(problems)
            )
            continue
        proposal.claims.append(claim)

    if proposal.any_match and not proposal.matches:
        proposal.malformed.append(
            "assessment says a candidate matches but no usable match claim survived "
            "validation; treating as no match"
        )
        proposal.any_match = False
    if not proposal.any_match and proposal.matches:
        proposal.malformed.append(
            "assessment says nothing matches but match claims were made; "
            "treating the claims as the intent"
        )
        proposal.any_match = True

    return proposal


# --- stage 5: listing draft --------------------------------------------------

DRAFT_TOOL_NAME = "draft_listing"

DRAFT_TOOL_SCHEMA: dict[str, Any] = {
    "name": DRAFT_TOOL_NAME,
    "description": "Write a marketplace title and description from recorded evidence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "80 characters maximum. Front-load what a buyer searches for.",
            },
            "description": {
                "type": "string",
                "description": (
                    "The listing as a buyer reads it: the factual claims and the "
                    "marketing copy woven together."
                ),
            },
            "marketing_copy": {
                "type": "string",
                "description": (
                    "Positioning, tone, who this suits and why. No citations needed "
                    "-- opinion asserts nothing checkable. Write it well."
                ),
            },
            "claims": {
                "type": "array",
                "description": (
                    "Every factual assertion, each citing the evidence behind it. "
                    "Marketing language does not belong here."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "evidence_ids": {
                            "type": "array", "items": {"type": "integer"},
                            "description": "Required and non-empty.",
                        },
                    },
                    "required": ["text", "evidence_ids"],
                },
            },
        },
        "required": ["title", "description", "marketing_copy", "claims"],
    },
}


def parse_draft_tool_input(payload: object, *, valid_evidence_ids: set[int]):
    """Turn a drafting tool call into a draft. Never raises."""
    from resell.reasoning.listing import DraftClaim, ListingDraft

    draft = ListingDraft()
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            draft.malformed.append("tool input was an unparseable string")
            return draft
    if not isinstance(payload, dict):
        draft.malformed.append(f"tool input was {type(payload).__name__}")
        return draft

    draft.title = str(payload.get("title", "")).strip()
    draft.description = str(payload.get("description", "")).strip()
    draft.marketing_copy = str(payload.get("marketing_copy", "")).strip()

    claims = []
    for index, raw in enumerate(_as_list(payload.get("claims"), "claims", draft.malformed)):
        if not isinstance(raw, dict):
            draft.malformed.append(f"claim {index}: expected an object")
            continue
        text = str(raw.get("text", "")).strip()
        if not text:
            draft.malformed.append(f"claim {index}: empty text")
            continue
        ids = []
        for item in raw.get("evidence_ids") if isinstance(raw.get("evidence_ids"), list) else []:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        claims.append(DraftClaim(text=text, evidence_ids=tuple(ids)))
    draft.claims = tuple(claims)
    return draft
