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
    reasoning_by_value: dict[tuple[str, str], str] = field(default_factory=dict)
    malformed: list[str] = field(default_factory=list)


def parse_map_tool_input(payload: object, *, valid_evidence_ids: set[int]) -> MappingProposal:
    """Turn a mapping tool call into candidate sets. Never raises.

    Citations are checked against the evidence actually in scope. A value citing an
    id that does not exist, or that belongs to another item or an earlier run, is
    dropped rather than trusted -- an invented citation is worse than an absent one
    because it looks like support.
    """
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

            unknown = [i for i in ids if i not in valid_evidence_ids]
            if unknown:
                proposal.malformed.append(
                    f"{name} candidate {value!r}: cites evidence {unknown} which is not "
                    f"in scope for this item"
                )
            ids = [i for i in ids if i in valid_evidence_ids]
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
            if isinstance(entry.get("reasoning"), str):
                proposal.reasoning_by_value[(name, value.strip())] = entry["reasoning"]

        proposal.candidates_by_aspect[name] = candidates

    return proposal
