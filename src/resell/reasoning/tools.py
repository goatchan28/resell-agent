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
import re
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

# The second shape of the same fault. Where the planner split a JSON document at
# its first key, drafting split an *XML-formatted* tool call at its second: `title`
# arrived correctly and `description` swallowed the entire remainder --
#
#     ...ready for your next set.</parameter>
#     <parameter name="marketing_copy">Whether you're building...</parameter>
#     <parameter name="claims">[{...}]
#
# Nothing raised. The draft parsed, passed review, and was stored with the markup
# and the marketing copy inside the description a buyer would read, and with no
# claims at all -- so every citation silently vanished from an architecture whose
# entire premise is that claims carry citations. Closing tags come back as either
# `</parameter>` or `</description>`, so both are accepted.
_XML_PARAM = re.compile(
    r"</(?:parameter|[A-Za-z_][\w.\-]*)>\s*<parameter\s+name=\"([^\"]+)\"\s*>",
    re.DOTALL,
)
_XML_TAIL = re.compile(r"</(?:parameter|[A-Za-z_][\w.\-]*)>\s*\Z", re.DOTALL)


def _split_xml_parameters(value: str) -> dict[str, str] | None:
    """Split one over-long string back into the parameters it ran together.

    The head keeps the key it arrived under; each `<parameter name="x">` opens the
    next. Returns None unless at least one boundary is present, so ordinary prose
    is never touched.
    """
    parts = _XML_PARAM.split(value)
    if len(parts) < 3:
        return None
    recovered = {"": _XML_TAIL.sub("", parts[0]).rstrip()}
    for name, text in zip(parts[1::2], parts[2::2], strict=False):
        recovered[name] = _XML_TAIL.sub("", text).rstrip()
    return recovered


def _maybe_json(text: str) -> object:
    """A recovered array or object is structure; anything else is prose."""
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _looks_like_entry(value: dict) -> bool:
    return bool(_ENTRY_MARKERS & set(value))


def unwrap_tool_input(payload: object, expected: set[str], malformed: list[str]) -> object:
    """Recover a whole argument object that arrived as a string inside one property.

    The observed shape, from the planning stage, on every call it ever made:

        {"assessment": "{\\"proposed_mode\\": ...}, \\"lookups\\": [...]}"}

    Note where the quote closes. The first key was parsed as structure and the whole
    remainder of the document became its string value -- so the string is not a
    serialised `assessment`, it is `{assessment}, "lookups": [...]}` and does not
    parse on its own. Re-prefixing the key it was split on reconstructs the intended
    document exactly.

    The plan inside was good: two well-cited lookups, one at a manufacturer and one
    at a reference source. It was discarded because `assessment` was a `str` where a
    dict belonged, and the resulting emptiness was then read as the planner deciding
    no research was warranted.

    Two strategies, both narrow, and neither runs while the expected keys are usable
    as given:

      1. a property whose string parses to a dict carrying an expected key -- the
         plain "serialised the arguments" case
      2. a single-property dict whose string, with `{"key": ` put back in front of
         it, parses to a dict carrying an expected key -- the case above

    Same posture as `_as_list` one level up: a badly-shaped response costs the
    entries it broke, not the whole pass.

    Not schema size, before that theory gets retried: this schema is 1462 bytes and
    the observation schema, which has never done this, is 2109.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            malformed.append("tool input was an unparseable string")
            return None
    if not isinstance(payload, dict):
        return payload

    # Before the structural check below, because this fault leaves the expected keys
    # looking perfectly usable -- `title` a string, `description` a string -- while
    # one of them holds three parameters' worth of text.
    for key, value in payload.items():
        if not isinstance(value, str) or "<parameter name=" not in value:
            continue
        recovered = _split_xml_parameters(value)
        if not recovered:
            continue
        merged = dict(payload)
        merged[key] = recovered.pop("")
        for name, text in recovered.items():
            merged[name] = _maybe_json(text)
        malformed.append(
            f"the arguments arrived as XML run together inside {key!r}; split them "
            f"back out ({', '.join(sorted(recovered))}) rather than publishing markup"
        )
        return merged

    # Nothing to do when any expected key already holds a usable structure.
    if any(not isinstance(payload.get(key), (str, type(None))) for key in expected):
        return payload

    for key, value in payload.items():
        if not isinstance(value, str):
            continue
        candidates = (
            value,
            # The key was consumed as structure; put it back.
            f"{{{json.dumps(key)}: {value}",
        )
        for attempt, text in enumerate(candidates):
            try:
                inner = json.loads(text)
            except ValueError:
                continue
            if not isinstance(inner, dict) or not expected & set(inner):
                continue
            malformed.append(
                f"the argument object arrived as a JSON string"
                + (f" split at {key!r}" if attempt else f" inside {key!r}")
                + "; reconstructed it rather than discarding the call"
            )
            return inner
    return payload


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

    payload = unwrap_tool_input(
        payload, {"observations", "identifiers", "identity_search"}, proposal.malformed
    )
    if payload is None:
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

    payload = unwrap_tool_input(payload, {"aspects"}, proposal.malformed)
    if payload is None:
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
    # Whether an assessment was actually read, as opposed to defaulted. Without
    # this, a plan that could not be parsed at all is indistinguishable from one
    # whose author decided no research was warranted -- and the caller recorded the
    # second as a negative finding when it was really the first.
    assessment_read: bool = False
    # Lookups the planner proposed and this parser refused. Distinct from
    # proposing none: MP-000013's planner proposed two well-motivated searches,
    # both were dropped for omitting `evidence_ids`, and the empty list that left
    # behind was reported as "the evidence is already sufficient". The item
    # recorded a deliberate decision not to search that nobody had made.
    dropped_lookups: int = 0

    @property
    def proposed_but_unusable(self) -> bool:
        """The planner wanted to search and nothing it asked for survived."""
        return self.dropped_lookups > 0 and not self.lookups

    @property
    def usable(self) -> bool:
        """Whether there is anything here to act on.

        No assessment and no lookups means the response yielded nothing, whatever
        else went wrong. A plan with either is workable.
        """
        return self.assessment_read or bool(self.lookups)


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

    payload = unwrap_tool_input(payload, {"assessment", "lookups"}, plan.malformed)
    if payload is None:
        return plan
    if not isinstance(payload, dict):
        plan.malformed.append(f"tool input was {type(payload).__name__}, expected an object")
        return plan

    assessment = payload.get("assessment")
    if isinstance(assessment, str):
        # The assessment alone serialised, with the rest of the object intact.
        try:
            assessment = json.loads(assessment)
        except ValueError:
            pass
    if isinstance(assessment, dict):
        plan.assessment_read = True
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
            plan.dropped_lookups += 1
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

    payload = unwrap_tool_input(payload, {"assessment", "claims"}, proposal.malformed)
    if payload is None:
        return proposal
    if not isinstance(payload, dict):
        proposal.malformed.append(f"tool input was {type(payload).__name__}, expected an object")
        return proposal

    assessment = payload.get("assessment")
    if isinstance(assessment, str):
        try:
            assessment = json.loads(assessment)
        except ValueError:
            pass
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
    payload = unwrap_tool_input(
        payload, {"title", "description", "claims"}, draft.malformed
    )
    if payload is None:
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


# --- fact extraction ---------------------------------------------------------

EXTRACT_TOOL_NAME = "extract_candidate_facts"

# Deliberately the narrowest tool in the system. Extraction is mechanical work at
# the highest volume of any stage -- one call per fetched page -- which makes it the
# first thing worth running on a cheaper or local model. Keeping the schema tiny is
# what makes that swap a configuration change rather than a prompt rewrite.
EXTRACT_TOOL_SCHEMA: dict[str, Any] = {
    "name": EXTRACT_TOOL_NAME,
    "description": (
        "Extract facts about the product a page describes, each quoting the page "
        "text it came from."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "product_title": {
                "type": "string",
                "description": "The product this page is about, as the page names it.",
            },
            "facts": {
                "type": "array",
                "description": (
                    "One entry per fact the page states about the product. Empty when "
                    "the page describes no product, which is a correct answer."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {
                            "type": "string",
                            "description": (
                                "One fact about the product, e.g. 'Colourway: Navy "
                                "Mini Houndstooth' or 'Model number A3211'."
                            ),
                        },
                        "domain": {
                            "type": "string",
                            "enum": ["identity", "retail"],
                            "description": (
                                "identity: what the product is. retail: what someone "
                                "charges for it. Prices, discounts, shipping and "
                                "availability are retail."
                            ),
                        },
                        "excerpt": {
                            "type": "string",
                            "description": (
                                "The page text this claim came from, quoted verbatim "
                                "and not paraphrased. Required: it is the only support "
                                "the claim has."
                            ),
                        },
                    },
                    "required": ["claim", "domain", "excerpt"],
                },
            },
        },
        "required": ["product_title", "facts"],
    },
}


@dataclass
class ExtractedFacts:
    """Facts pulled from one page, with what could not be used."""

    product_title: str = ""
    # (claim, domain, excerpt)
    facts: tuple[tuple[str, str, str], ...] = ()
    malformed: list[str] = field(default_factory=list)


def parse_extract_tool_input(payload: object, *, page_text: str) -> ExtractedFacts:
    """Validate an extraction. Never raises.

    Two rules, both checked here rather than trusted:

    A claim must carry an excerpt, and the excerpt must actually occur in the page
    that was fetched. That second check is the whole reason the excerpt is worth
    storing -- without it the field is just more model output, and a fabricated
    quotation would look exactly like a real one. Whitespace is normalised before
    comparing, because HTML-to-text collapses line breaks unpredictably and a
    quotation that differs only in spacing is still the page's own words.

    A `retail` domain is accepted as given but never widened: `citable_candidate_
    evidence` admits identity facts only, so a retail fact misfiled as identity is
    the error that matters and a mislabelled identity fact merely goes unused.
    """
    extracted = ExtractedFacts()

    payload = unwrap_tool_input(
        payload, {"product_title", "facts"}, extracted.malformed
    )
    if payload is None:
        return extracted
    if not isinstance(payload, dict):
        extracted.malformed.append(
            f"tool input was {type(payload).__name__}, expected an object"
        )
        return extracted

    extracted.product_title = str(payload.get("product_title", "")).strip()
    haystack = " ".join(page_text.split()).casefold()

    kept: list[tuple[str, str, str]] = []
    for index, raw in enumerate(
        _as_list(payload.get("facts"), "facts", extracted.malformed)
    ):
        if not isinstance(raw, dict):
            extracted.malformed.append(f"fact {index}: expected an object")
            continue
        claim = str(raw.get("claim", "")).strip()
        excerpt = str(raw.get("excerpt", "")).strip()
        domain = str(raw.get("domain", "")).strip().lower()

        if not claim:
            extracted.malformed.append(f"fact {index}: empty claim")
            continue
        if not excerpt:
            extracted.malformed.append(
                f"fact {index}: {claim[:50]!r} quotes nothing from the page; a fact "
                f"with no excerpt has no support"
            )
            continue
        if " ".join(excerpt.split()).casefold() not in haystack:
            extracted.malformed.append(
                f"fact {index}: the excerpt for {claim[:50]!r} does not appear in the "
                f"fetched page, so the quotation is not the page's own words"
            )
            continue
        if domain not in ("identity", "retail"):
            extracted.malformed.append(
                f"fact {index}: unknown domain {domain!r} for {claim[:40]!r}; "
                f"recorded as retail, which cannot be cited by an aspect"
            )
            domain = "retail"
        kept.append((claim, domain, excerpt))

    extracted.facts = tuple(kept)
    return extracted


# --- comp research -----------------------------------------------------------

COMP_PLAN_TOOL_NAME = "plan_comp_research"

COMP_PLAN_TOOL_SCHEMA: dict[str, Any] = {
    "name": COMP_PLAN_TOOL_NAME,
    "description": "Decide which marketplace searches would establish what this sells for.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assessment": {
                "type": "object",
                "properties": {
                    "sufficient": {
                        "type": "boolean",
                        "description": (
                            "True when the comps already recorded are enough. An "
                            "empty plan with a reason is a good answer."
                        ),
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["sufficient", "rationale"],
            },
            "lookups": {
                "type": "array",
                "description": "Searches, most valuable first; the list may be trimmed.",
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "seeking": {
                            "type": "string",
                            "enum": ["sold", "asking"],
                            "description": (
                                "Which kind of price this search is meant to find. "
                                "Realised sales are worth more than open offers."
                            ),
                        },
                        "motivation": {"type": "string"},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Observations of the item that justify this search. "
                                "Required: a query with nothing behind it is browsing."
                            ),
                        },
                    },
                    "required": ["query", "seeking", "motivation", "evidence_ids"],
                },
            },
        },
        "required": ["assessment", "lookups"],
    },
}


RETAIL_EXTRACT_TOOL_NAME = "extract_retail_prices"

RETAIL_EXTRACT_TOOL_SCHEMA: dict[str, Any] = {
    "name": RETAIL_EXTRACT_TOOL_NAME,
    "description": (
        "List the products a shop's page offers and what the shop charges for "
        "each, quoting the page."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "products": {
                "type": "array",
                "description": (
                    "One entry per product the page offers. A product page has "
                    "one; a collection or grid page has as many as it shows. "
                    "Empty when the page sells nothing -- a brand's homepage "
                    "usually sells nothing -- which is a correct answer."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "product_title": {
                            "type": "string",
                            "description": "The product's name as the page gives it.",
                        },
                        "price_cents": {
                            "type": "integer",
                            "description": (
                                "What the shop charges today, in cents. Where a "
                                "page shows a struck-through price beside a "
                                "current one, this is the current one."
                            ),
                        },
                        "was_price_cents": {
                            "type": "integer",
                            "description": (
                                "The struck-through or compare-at price, where the "
                                "page shows one. Omit entirely when it does not."
                            ),
                        },
                        "currency": {"type": "string"},
                        "in_stock": {
                            "type": "boolean",
                            "description": (
                                "Only where the page says. Omit when it does not."
                            ),
                        },
                        "excerpt": {
                            "type": "string",
                            "description": (
                                "The page's own words for this product, verbatim, "
                                "and the quotation must contain the price. An "
                                "entry whose price is not in its quotation is "
                                "discarded."
                            ),
                        },
                    },
                    "required": ["product_title", "price_cents", "excerpt"],
                },
            },
        },
        "required": ["products"],
    },
}


COMP_EXTRACT_TOOL_NAME = "extract_comps"

COMP_EXTRACT_TOOL_SCHEMA: dict[str, Any] = {
    "name": COMP_EXTRACT_TOOL_NAME,
    "description": "List the marketplace listings a page shows, quoting each one.",
    "input_schema": {
        "type": "object",
        "properties": {
            "listings": {
                "type": "array",
                "description": (
                    "One entry per listing on the page. Empty when the page shows "
                    "none, which is a correct answer."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "price_cents": {
                            "type": "integer",
                            "description": "The listing price in cents, excluding postage.",
                        },
                        "price_state": {
                            "type": "string",
                            "enum": ["sold", "asking"],
                            "description": (
                                "sold only where the page says the item sold. If it "
                                "is an open offer, say asking."
                            ),
                        },
                        "shipping_cents": {
                            "type": "integer",
                            "description": (
                                "Postage in cents. Omit entirely when the page does "
                                "not state it; omission is not zero."
                            ),
                        },
                        "condition_text": {
                            "type": "string",
                            "description": "The seller's own condition wording, verbatim.",
                        },
                        "sale_date": {
                            "type": "string",
                            "description": "ISO date the sale completed, where shown.",
                        },
                        "external_id": {
                            "type": "string",
                            "description": "The listing's id on the marketplace, where shown.",
                        },
                        "url": {"type": "string"},
                        "excerpt": {
                            "type": "string",
                            "description": (
                                "The page text this listing's price came from, quoted "
                                "verbatim. Must contain the price."
                            ),
                        },
                    },
                    "required": ["title", "price_cents", "price_state", "excerpt"],
                },
            }
        },
        "required": ["listings"],
    },
}


COMP_JUDGE_TOOL_NAME = "judge_comps"

COMP_JUDGE_TOOL_SCHEMA: dict[str, Any] = {
    "name": COMP_JUDGE_TOOL_NAME,
    "description": "Judge how comparable each retrieved listing is to this item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "judgements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "comp_id": {"type": "string"},
                        "comparability": {
                            "type": "string",
                            "enum": ["same_product", "same_family_variant",
                                     "category_attribute", "superficial", "excluded"],
                            "description": (
                                "same_product: the identical product. "
                                "same_family_variant: same line, different variant. "
                                "category_attribute: same kind of thing, including a "
                                "sibling -- same brand and material, different fit or "
                                "sub-line. "
                                "superficial: merely resembles it. "
                                "excluded: a different object entirely -- other "
                                "material, bundle, accessory, part, lot. Say why."
                            ),
                        },
                        "item_evidence_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Observations of your item this was matched on. "
                                "Required."
                            ),
                        },
                        "comp_fields": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Fields of the listing matched against, e.g. title, "
                                "condition_text. Required."
                            ),
                        },
                        "rationale": {"type": "string"},
                        "excluded_reason": {
                            "type": "string",
                            "description": (
                                "Required when comparability is excluded, and the "
                                "judgement is discarded without it -- so the listing "
                                "ends up neither counted nor explained. One short "
                                "phrase: what the extra item was, which cloth it is, "
                                "what kind of thing it turned out to be."
                            ),
                        },
                    },
                    "required": ["comp_id", "comparability", "item_evidence_ids",
                                 "comp_fields", "rationale"],
                },
            }
        },
        "required": ["judgements"],
    },
}


@dataclass
class CompLookup:
    query: str
    seeking: str
    motivation: str
    evidence_ids: tuple[int, ...]


@dataclass
class CompPlan:
    sufficient: bool = False
    rationale: str = ""
    lookups: list[CompLookup] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)
    assessment_read: bool = False
    # Lookups the planner proposed and this parser refused. Distinct from
    # proposing none: MP-000013's planner proposed two well-motivated searches,
    # both were dropped for omitting `evidence_ids`, and the empty list that left
    # behind was reported as "the evidence is already sufficient". The item
    # recorded a deliberate decision not to search that nobody had made.
    dropped_lookups: int = 0

    @property
    def proposed_but_unusable(self) -> bool:
        """The planner wanted to search and nothing it asked for survived."""
        return self.dropped_lookups > 0 and not self.lookups

    @property
    def usable(self) -> bool:
        return self.assessment_read or bool(self.lookups)


def parse_comp_plan_tool_input(
    payload: object, *, valid_evidence_ids: set[int], already_searched: set[str] | None = None
) -> CompPlan:
    """Validate a comp search plan. Never raises.

    Same two rules as identity planning: a query cites the observations that
    motivate it, and a query already run for this item is dropped.
    """
    already_searched = {q.casefold() for q in (already_searched or set())}
    plan = CompPlan()

    payload = unwrap_tool_input(payload, {"assessment", "lookups"}, plan.malformed)
    if payload is None:
        return plan
    if not isinstance(payload, dict):
        plan.malformed.append(f"tool input was {type(payload).__name__}, expected an object")
        return plan

    assessment = payload.get("assessment")
    if isinstance(assessment, str):
        try:
            assessment = json.loads(assessment)
        except ValueError:
            pass
    if isinstance(assessment, dict):
        plan.assessment_read = True
        plan.sufficient = bool(assessment.get("sufficient"))
        plan.rationale = str(assessment.get("rationale", ""))
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
        ids = []
        for item in raw.get("evidence_ids") if isinstance(raw.get("evidence_ids"), list) else []:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        unknown = [i for i in ids if i not in valid_evidence_ids]
        if unknown:
            plan.malformed.append(f"lookup {index}: cites unknown evidence {unknown}")
        ids = [i for i in ids if i in valid_evidence_ids]
        if not ids:
            plan.malformed.append(
                f"lookup {index}: {query!r} cites no observation of the item; that is "
                f"browsing, not research"
            )
            continue
        seeking = str(raw.get("seeking", "asking")).strip().lower()
        if seeking not in ("sold", "asking"):
            seeking = "asking"
        plan.lookups.append(
            CompLookup(query=query, seeking=seeking,
                       motivation=str(raw.get("motivation", "")), evidence_ids=tuple(ids))
        )
    return plan


@dataclass
class ExtractedComp:
    title: str
    price_cents: int
    price_state: str
    excerpt: str
    shipping_cents: int | None = None
    condition_text: str = ""
    sale_date: str = ""
    external_id: str = ""
    url: str = ""


@dataclass
class ExtractedComps:
    listings: tuple[ExtractedComp, ...] = ()
    malformed: list[str] = field(default_factory=list)


def parse_comp_extract_tool_input(payload: object, *, page_text: str) -> ExtractedComps:
    """Validate extracted listings. Never raises.

    Every listing must quote the page, and the quotation must contain the price.
    The text-only check that suffices for an identity fact is not enough here: the
    fact *is* a number, so a real quotation beside an invented figure would pass it
    and go straight into a distribution.
    """
    from resell.reasoning.comp_reading import price_supported_by

    out = ExtractedComps()
    payload = unwrap_tool_input(payload, {"listings"}, out.malformed)
    if payload is None:
        return out
    if not isinstance(payload, dict):
        out.malformed.append(f"tool input was {type(payload).__name__}, expected an object")
        return out

    haystack = " ".join(page_text.split()).casefold()
    kept: list[ExtractedComp] = []
    for index, raw in enumerate(_as_list(payload.get("listings"), "listings", out.malformed)):
        if not isinstance(raw, dict):
            out.malformed.append(f"listing {index}: expected an object")
            continue
        title = str(raw.get("title", "")).strip()
        excerpt = str(raw.get("excerpt", "")).strip()
        try:
            price_cents = int(raw.get("price_cents"))
        except (TypeError, ValueError):
            out.malformed.append(f"listing {index}: {title[:40]!r} has no usable price")
            continue
        if price_cents < 0:
            out.malformed.append(f"listing {index}: negative price")
            continue
        if not excerpt:
            out.malformed.append(
                f"listing {index}: {title[:40]!r} quotes nothing from the page"
            )
            continue
        if " ".join(excerpt.split()).casefold() not in haystack:
            out.malformed.append(
                f"listing {index}: the excerpt for {title[:40]!r} is not in the "
                f"fetched page"
            )
            continue
        if not price_supported_by(price_cents, excerpt):
            out.malformed.append(
                f"listing {index}: the quoted text for {title[:40]!r} does not contain "
                f"{price_cents / 100:.2f}; the price is not supported by what was read"
            )
            continue

        shipping = raw.get("shipping_cents")
        try:
            shipping_cents = None if shipping is None else int(shipping)
        except (TypeError, ValueError):
            shipping_cents = None
        if shipping_cents is not None and shipping_cents < 0:
            shipping_cents = None

        kept.append(ExtractedComp(
            title=title or "(untitled listing)",
            price_cents=price_cents,
            price_state=str(raw.get("price_state", "asking")).strip().lower(),
            excerpt=excerpt,
            shipping_cents=shipping_cents,
            condition_text=str(raw.get("condition_text", "")).strip(),
            sale_date=str(raw.get("sale_date", "")).strip(),
            external_id=str(raw.get("external_id", "")).strip(),
            url=str(raw.get("url", "")).strip(),
        ))
    out.listings = tuple(kept)
    return out


@dataclass
class CompJudgement:
    comp_id: str
    comparability: str
    item_evidence_ids: tuple[int, ...]
    comp_fields: tuple[str, ...]
    rationale: str
    excluded_reason: str | None = None


@dataclass
class CompJudgements:
    judgements: tuple[CompJudgement, ...] = ()
    malformed: list[str] = field(default_factory=list)


def parse_comp_judge_tool_input(
    payload: object, *, valid_item_evidence: set[int], valid_comp_ids: set[str]
) -> CompJudgements:
    """Validate comparability judgements. Never raises.

    Citations on both sides are required here as they are in `validate_claim`, and
    for the same reason: a judgement citing only the listing is a description of a
    web page. The ladder ceiling is *not* checked here -- `record_comp_claim`
    enforces it against the item's identity resolution, which is a stored fact
    rather than anything this parser can see.
    """
    out = CompJudgements()
    payload = unwrap_tool_input(payload, {"judgements"}, out.malformed)
    if payload is None:
        return out
    if not isinstance(payload, dict):
        out.malformed.append(f"tool input was {type(payload).__name__}, expected an object")
        return out

    valid_ladder = {"same_product", "same_family_variant", "category_attribute",
                    "superficial", "excluded"}
    kept: list[CompJudgement] = []
    for index, raw in enumerate(
        _as_list(payload.get("judgements"), "judgements", out.malformed)
    ):
        if not isinstance(raw, dict):
            out.malformed.append(f"judgement {index}: expected an object")
            continue
        comp_id = str(raw.get("comp_id", "")).strip()
        if comp_id not in valid_comp_ids:
            out.malformed.append(
                f"judgement {index}: {comp_id!r} is not a comp retrieved for this item"
            )
            continue
        rung = str(raw.get("comparability", "")).strip().lower()
        if rung not in valid_ladder:
            out.malformed.append(f"judgement {index}: unknown comparability {rung!r}")
            continue

        ids = []
        for item in (raw.get("item_evidence_ids") or []):
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        unknown = [i for i in ids if i not in valid_item_evidence]
        if unknown:
            out.malformed.append(f"judgement {index}: cites unknown item evidence {unknown}")
        ids = [i for i in ids if i in valid_item_evidence]
        fields = tuple(
            str(f).strip() for f in (raw.get("comp_fields") or []) if str(f).strip()
        )
        excluded_reason = str(raw.get("excluded_reason", "")).strip() or None

        if rung == "excluded":
            if not excluded_reason:
                out.malformed.append(
                    f"judgement {index}: an excluded comp must record why"
                )
                continue
        else:
            if not ids:
                out.malformed.append(
                    f"judgement {index}: cites no observation of the item, so it is a "
                    f"description of a listing rather than a comparison"
                )
                continue
            if not fields:
                out.malformed.append(
                    f"judgement {index}: names no field of the listing it matched against"
                )
                continue

        kept.append(CompJudgement(
            comp_id=comp_id, comparability=rung, item_evidence_ids=tuple(ids),
            comp_fields=fields, rationale=str(raw.get("rationale", "")),
            excluded_reason=excluded_reason,
        ))
    out.judgements = tuple(kept)
    return out


# --- condition -----------------------------------------------------------------

CONDITION_TOOL_NAME = "choose_condition"

CONDITION_TOOL_SCHEMA: dict[str, Any] = {
    "name": CONDITION_TOOL_NAME,
    "description": "Choose the marketplace condition grade that describes this item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "condition": {
                "type": "string",
                "description": "Exactly one of the grades you were given.",
            },
            "evidence_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Observations supporting the grade. Required.",
            },
            "rationale": {"type": "string"},
            "uncertain_because": {
                "type": "string",
                "description": (
                    "What the photographs did not settle, where anything. Empty when "
                    "the grade is clear."
                ),
            },
        },
        "required": ["condition", "evidence_ids", "rationale"],
    },
}


@dataclass
class ConditionChoice:
    condition: str = ""
    evidence_ids: tuple[int, ...] = ()
    rationale: str = ""
    uncertain_because: str = ""
    malformed: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.condition)


def parse_condition_tool_input(
    payload: object, *, allowed: set[str], valid_evidence_ids: set[int]
) -> ConditionChoice:
    """Validate a condition choice. Never raises.

    The allowed set is eBay's, for this category, and is enforced here rather than
    discovered at publish. A grade outside it is dropped entirely: recording it
    would put a value in the identification that the marketplace refuses, which is
    exactly the stall this stage exists to prevent.
    """
    choice = ConditionChoice()
    payload = unwrap_tool_input(
        payload, {"condition", "evidence_ids"}, choice.malformed
    )
    if payload is None or not isinstance(payload, dict):
        choice.malformed.append("tool input was not an object")
        return choice

    condition = str(payload.get("condition", "")).strip()
    if condition not in allowed:
        choice.malformed.append(
            f"{condition!r} is not one of the grades this category accepts "
            f"({', '.join(sorted(allowed))}); nothing recorded"
        )
        return choice

    ids = []
    for item in (payload.get("evidence_ids") or []):
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    unknown = [i for i in ids if i not in valid_evidence_ids]
    if unknown:
        choice.malformed.append(f"cites unknown evidence {unknown}")
    ids = [i for i in ids if i in valid_evidence_ids]
    if not ids:
        choice.malformed.append(
            "cites no observation; a grade is a claim about the object and carries "
            "the same burden as any other"
        )
        return choice

    choice.condition = condition
    choice.evidence_ids = tuple(ids)
    choice.rationale = str(payload.get("rationale", ""))
    choice.uncertain_because = str(payload.get("uncertain_because", "")).strip()
    return choice
