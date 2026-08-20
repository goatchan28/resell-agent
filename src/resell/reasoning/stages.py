"""Provider-neutral definitions of what a reasoning stage asks for and returns.

Nothing here knows about any vendor. A stage is: a system prompt, some images, an
instruction, and one tool contract the model must fill. A result is: the tool input
it filled, token counts, and a raw response kept for the trace.

Everything downstream -- observations, evidence, the gateway, the database -- sees
only these types. Swapping providers is implementing one adapter, not rewriting
the reasoning plane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from resell.reasoning.tools import OBSERVE_TOOL_NAME, OBSERVE_TOOL_SCHEMA

OBSERVE_SYSTEM_PROMPT = """You are examining photographs of a single second-hand item that \
is about to be resold. Your job at this stage is to describe the object, not to \
decide how it should be listed.

Rules:

- Report only what the photographs support. If you cannot see something, do not \
supply it.
- One claim per observation. Split compound statements: "ceramic, teal, chipped rim" \
is three observations, not one.
- Choose the basis honestly, from exactly these four values:
  `text_read` -- transcribing text that is legible in the image.
  `visual_observation` -- a property you can directly see: colour, shape, damage.
  `measurement` -- a physical dimension; state the method.
  `inference` -- reasoning beyond what is visible. Legitimate and useful, but it \
must be labelled as such rather than presented as something seen.
  Every observation needs one. There is no default.
- Always cite the photo positions supporting a claim, so a person can check it.
- Transcribe identifiers character by character. Do not correct, complete or guess \
one. Check digits are verified afterwards, and a plausible-looking wrong number is \
worse than no number.
- Any code you transcribe must ALSO appear in the `identifiers` array, not only in \
an observation. Style codes, product codes, article numbers, factory codes and \
barcode digits are all identifiers. An observation saying "the tag reads style code \
ABC-123" records that you saw it; the identifiers entry is what gets verified and \
carried into the listing. Do both. If the scheme is unclear, use `other`.
- Emit the tool arguments as real JSON structures: `observations` and `identifiers` \
are arrays of objects, and `identity_search` is an object. Do not serialise them \
into strings.
- Where a property is genuinely ambiguous from the photographs -- a colour that \
could be navy or black, a material that could be wool or a blend -- say so in the \
claim rather than picking one. Ambiguity is information.
- Record what you examined while looking for maker's marks, labels and model \
numbers, whether or not you found any. Many household items have no discoverable \
brand, and that is a normal outcome; but "there is nothing" and "I did not look" \
must be distinguishable.

- `identifiers` is required. Supply an empty array only when no code of any kind is \
legible anywhere in the photographs; an empty array is a claim, not a default.

Call the record_observations tool exactly once, supplying `observations`, \
`identifiers` and `identity_search`."""


@dataclass(frozen=True)
class ToolSpec:
    """A tool contract as neutral JSON Schema.

    Providers differ in what they accept -- some require additionalProperties to be
    false and every property listed as required, others support only a subset of
    the schema vocabulary. Adapters transform this; they do not each get their own
    copy of the contract.
    """

    name: str
    description: str
    json_schema: dict[str, Any]


@dataclass(frozen=True)
class ImageRef:
    """An image ready to send: already converted, downscaled and cached."""

    path: Path
    position: int
    content_sha256: str | None = None


@dataclass(frozen=True)
class StageRequest:
    system_prompt: str
    images: tuple[ImageRef, ...]
    instruction: str
    tool: ToolSpec
    max_tokens: int = 4000
    require_tool: bool = True

    def replay_key(self) -> dict[str, Any]:
        """A provider-independent description of this request.

        Recorded with every trace so the identical input can later be replayed
        against another provider and the outputs compared. Images are identified by
        content hash rather than path, so the comparison survives files moving.
        """
        return {
            "system_prompt_sha256": _digest(self.system_prompt),
            "instruction": self.instruction,
            "tool": self.tool.name,
            "tool_schema_sha256": _digest(repr(self.tool.json_schema)),
            "images": [
                {"position": image.position, "sha256": image.content_sha256}
                for image in self.images
            ],
        }


@dataclass(frozen=True)
class Usage:
    """Token accounting, normalised.

    Providers name these differently and add their own extras (cached tokens,
    reasoning tokens, per-modality counts). The normalised pair is what comparisons
    need; `raw` keeps whatever the provider actually said so nothing is lost.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageResult:
    """What an adapter returns. Deliberately free of vendor structure."""

    tool_input: dict[str, Any]
    usage: Usage
    latency_ms: int
    provider: str
    model: str
    stop_reason: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]


def observation_stage(images: tuple[ImageRef, ...], note: str | None = None) -> StageRequest:
    """Build the observation stage request.

    Note what is absent: the eBay aspect form. A model told a `Size` aspect is
    required is under pressure to produce one whether or not it can see a size.
    """
    instruction = (
        f"These {len(images)} photographs show one item. "
        "Describe what you observe, and record what you examined while looking for "
        "any maker's mark, label or model number."
    )
    if note:
        instruction += f"\n\nAdditional context from the operator: {note}"

    return StageRequest(
        system_prompt=OBSERVE_SYSTEM_PROMPT,
        images=images,
        instruction=instruction,
        tool=ToolSpec(
            name=OBSERVE_TOOL_NAME,
            description=OBSERVE_TOOL_SCHEMA["description"],
            json_schema=OBSERVE_TOOL_SCHEMA["input_schema"],
        ),
    )


MAP_SYSTEM_PROMPT = """You are mapping recorded observations of a second-hand item onto \
a marketplace's aspect form. You are not looking at the item; you are working from \
observations someone else recorded, each with an id.

Rules:

- Propose a value only when a recorded observation supports it. Cite the observation \
ids. A value you cannot cite is a guess, and a guess that reaches a listing is worse \
than a blank.
- If nothing supports an aspect, return an empty candidates array for it. That is a \
correct and useful answer, not a failure.
- If the observations support more than one reading, return each as a separate \
candidate with its own citations. Do not choose between them. Two labels giving \
different sizes, or one observation that cannot distinguish navy from black, are \
both cases where returning both readings is the right answer.
- Do not cite an observation that merely mentions the topic. "The label reads MADE IN \
EGYPT" supports a Country/Region of Manufacture value; it does not support a Size.
- Where the form lists allowed values, prefer one of them exactly as written. If the \
observations support something not in the list, propose it anyway and cite it -- the \
list is not always exhaustive.
- Include every aspect from the form in your answer, including the ones you left empty.

Call the map_aspects tool exactly once."""


def mapping_stage(
    aspect_form: str, observations: str, max_output_tokens: int = 4000
) -> StageRequest:
    """Build the aspect mapping request.

    No images. The stage works from recorded observations so that every value is
    traceable to one; letting it re-observe would let it produce values with nothing
    behind them, which is the failure this whole design exists to prevent.
    """
    from resell.reasoning.tools import MAP_TOOL_NAME, MAP_TOOL_SCHEMA

    instruction = (
        "Aspect form:\n\n"
        f"{aspect_form}\n\n"
        "Recorded observations:\n\n"
        f"{observations}\n\n"
        "Map the observations onto the form."
    )
    return StageRequest(
        system_prompt=MAP_SYSTEM_PROMPT,
        images=(),
        instruction=instruction,
        tool=ToolSpec(
            name=MAP_TOOL_NAME,
            description=MAP_TOOL_SCHEMA["description"],
            json_schema=MAP_TOOL_SCHEMA["input_schema"],
        ),
        max_tokens=max_output_tokens,
    )


def render_aspect_form(specs, max_values: int = 60) -> str:
    """The form as the model sees it.

    Allowed values are capped: some aspects carry hundreds, and the token cost is
    real. The cap is stated so a missing value is visibly a truncation rather than
    an absence.
    """
    lines = []
    for spec in specs:
        flag = "REQUIRED" if spec.required else "optional"
        lines.append(f"- {spec.name} [{flag}, {spec.mode.lower()}, {spec.cardinality.lower()}]")
        if spec.allowed_values:
            shown = list(spec.allowed_values[:max_values])
            suffix = (
                f" ... and {len(spec.allowed_values) - len(shown)} more not shown"
                if len(spec.allowed_values) > len(shown) else ""
            )
            lines.append(f"    allowed: {' | '.join(shown)}{suffix}")
        else:
            lines.append("    free text")
    return "\n".join(lines)


def render_observations(rows) -> str:
    """Observations as the model sees them: id, basis, claim, photo positions."""
    import json as _json

    lines = []
    for row in rows:
        payload = _json.loads(row["payload"])
        claim = payload.get("claim") or payload.get("normalized") or payload.get("answer") or ""
        detail = []
        if payload.get("photo_positions"):
            detail.append(f"photos {payload['photo_positions']}")
        if payload.get("surface"):
            detail.append(payload["surface"])
        suffix = f"  ({'; '.join(detail)})" if detail else ""
        lines.append(f"[{row['id']}] {row['basis'] or row['kind']}: {claim}{suffix}")
    return "\n".join(lines)
