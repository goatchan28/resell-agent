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
- If nothing supports an aspect, return an empty candidates array AND an \
`unsupported_reason`. That is a correct and useful answer, not a failure. The reason \
matters as much as the emptiness:
  `not_observed` -- the photographs say nothing about it.
  `not_applicable` -- it does not apply to this kind of object at all, such as an \
inseam on a jacket.
  `none_apply` -- the evidence describes the property, but none of the allowed values \
is truthful. A standalone jacket in a form whose Style offers only "2 Piece", \
"3 Piece" and "Tuxedo" is this case.
  `insufficient_evidence` -- partly observed, not enough to name a value.
- Never pick a least-wrong value because a field is required. `none_apply` is the \
correct answer when no allowed option is true, and it tells us the category may be \
wrong rather than the item unknowable.
- If the observations support more than one reading, return each as a separate \
candidate with its own citations. Do not choose between them. Two labels giving \
different sizes, or one observation that cannot distinguish navy from black, are \
both cases where returning both readings is the right answer.
- Do not cite an observation that merely mentions the topic. "The label reads MADE IN \
EGYPT" supports a Country/Region of Manufacture value; it does not support a Size.
- The observation you cite must state the value itself. An observation that only \
supports it once you add knowledge from outside this list does not support it: a \
regulatory panel reading "Apple Inc." does not establish Brand = Beats by Dr. Dre, \
even though Apple owns Beats. The observation that reads "Beats" does. Cite that one. \
If several observations name the value, cite them all; if the only one you can find \
needs an outside fact to connect it, the honest answer is an empty candidates array \
with `insufficient_evidence`.
- External facts, where supplied, come from a product matched to this item. They can \
settle things a photograph cannot -- which of two transcribed codes is the product \
number, what a manufacturer calls a colourway. They cannot tell you the condition or \
the size of the object in front of the operator. Where an observation and an external \
fact both bear on an aspect, cite the observation.
- Where the form lists allowed values, prefer one of them exactly as written. If the \
observations support something not in the list, propose it anyway and cite it -- the \
list is not always exhaustive.
- Include every aspect from the form in your answer, including the ones you left empty.

Call the map_aspects tool exactly once."""


def mapping_stage(
    aspect_form: str,
    observations: str,
    max_output_tokens: int = 4000,
    donated: str = "",
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
        "Recorded observations of the item:\n\n"
        f"{observations}\n\n"
    )
    if donated:
        instruction += (
            "External facts about a product matched to this item:\n\n"
            f"{donated}\n\n"
            "These describe a product believed to be this one, not the object itself. "
            "Cite them where they genuinely settle an aspect, and prefer a direct "
            "observation when both are available.\n\n"
        )
    instruction += "Map the observations onto the form."
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


PLAN_SYSTEM_PROMPT = """You are deciding whether external research would improve the \
identification of a second-hand item, and if so, exactly what to look up. You are \
planning, not searching. Nothing is fetched until this plan is agreed.

Rules:

- Every lookup must cite the observations that motivate it. "Search the \
manufacturer's site for style code SUJT EXP 2BSV SLIM, because observation 41 \
transcribed it from the swing tag" is a plan. "Search for Brooks Brothers jackets" \
is browsing.
- Order lookups by value. The list may be trimmed to fit a budget, so what you put \
first is what will run.
- Prefer the manufacturer's own source. The same code on a reseller page is worth \
much less, because they may have transcribed it from a photograph or be describing a \
different variant.
- Say when searching is not warranted. If the evidence already supports the best \
identification available, set `sufficient` to true, propose the mode, and return no \
lookups. An item with no discoverable brand and a thorough examination behind it is \
a `described_object`, and that is a successful outcome, not a failure to identify. \
Spending lookups to confirm an absence is waste.
- A known brand with no discoverable model is `branded_generic`. If the brand is \
already established from a label, searching for a model number that the item does \
not carry will not find one.
- Do not propose a search you have already been told was performed.
- Remember what a lookup can and cannot do. It can confirm which of two transcribed \
codes is the product number; it cannot tell you the size of the garment in front of \
the operator.

Call the plan_research tool exactly once."""


def planning_stage(
    observations: str,
    identifiers: str,
    unresolved: str,
    prior_lookups: str,
    current_mode: str,
    effort: str,
    max_output_tokens: int = 2000,
) -> StageRequest:
    """Build the research planning request. No images: this reasons over the record."""
    from resell.reasoning.tools import PLAN_TOOL_NAME, PLAN_TOOL_SCHEMA

    instruction = (
        f"Current identification mode: {current_mode}\n"
        f"Identification effort allowed: {effort}\n\n"
        f"Recorded observations:\n\n{observations}\n\n"
        f"Identifiers read from the item:\n\n{identifiers or '(none)'}\n\n"
        f"Aspects still unresolved:\n\n{unresolved or '(none)'}\n\n"
        f"Lookups already performed for this item:\n\n{prior_lookups or '(none)'}\n\n"
        "Decide whether external research would improve this identification, and "
        "what specifically to look up."
    )
    return StageRequest(
        system_prompt=PLAN_SYSTEM_PROMPT,
        images=(),
        instruction=instruction,
        tool=ToolSpec(
            name=PLAN_TOOL_NAME,
            description=PLAN_TOOL_SCHEMA["description"],
            json_schema=PLAN_TOOL_SCHEMA["input_schema"],
        ),
        max_tokens=max_output_tokens,
    )


MATCH_SYSTEM_PROMPT = """You are judging whether retrieved candidate products are the \
second-hand item described by a set of observations. You are not looking at the item \
or at the pages; you are comparing two records.

Rules:

- Concluding that nothing matches is a correct answer. A research loop that always \
selects a product will always find one, and what it finds will increasingly be \
whatever it was hoping for. If none of these candidates is the item, say so.
- Record the non-matches too, with what ruled them out. "Candidate is a three-button \
jacket; observation 29 shows a two-button front" is worth keeping: it stops the same \
candidate being reconsidered, and a specific conflict is more informative than \
silence.
- Every claim cites both sides. Which observation of the physical object corresponds \
to which fact about the candidate. A claim citing only the candidate is a description \
of a web page.
- Choose the strength honestly. `identifier_verified` needs an identifier that passes \
its own check digit. `identifier_asserted` is a code that matches but cannot be \
checked -- garment style codes, model numbers. `attribute_convergence` is several \
independent attributes agreeing with no identifier. `similarity` is resemblance, and \
donates nothing.
- Resemblance is not identity. A page showing a very similar navy two-button jacket \
from the same brand is `similarity` unless something ties it specifically to this \
object. Mass-produced goods have many near-twins, and the differences that matter -- \
year, variant, colourway -- are often invisible in a photograph.
- Do not infer that a candidate matches because it would be convenient. The item is \
allowed to be unidentifiable.

Call the judge_candidates tool exactly once."""


def matching_stage(
    observations: str, candidates: str, max_output_tokens: int = 3000
) -> StageRequest:
    """Build the candidate matching request."""
    from resell.reasoning.tools import MATCH_TOOL_NAME, MATCH_TOOL_SCHEMA

    instruction = (
        f"Observations of the physical item:\n\n{observations}\n\n"
        f"Retrieved candidate products:\n\n{candidates}\n\n"
        "Judge each candidate. Recording that none of them is this item is a valid "
        "and useful conclusion."
    )
    return StageRequest(
        system_prompt=MATCH_SYSTEM_PROMPT,
        images=(),
        instruction=instruction,
        tool=ToolSpec(
            name=MATCH_TOOL_NAME,
            description=MATCH_TOOL_SCHEMA["description"],
            json_schema=MATCH_TOOL_SCHEMA["input_schema"],
        ),
        max_tokens=max_output_tokens,
    )


def render_donated_facts(conn, citable: dict[int, str]) -> str:
    """External facts an aspect is permitted to cite, with their provenance.

    Provenance travels with them into the prompt for the same reason it travels into
    the record: a fact someone typed off a page the system never loaded should not
    read identically to one it fetched.
    """
    import json as _json

    if not citable:
        return ""
    placeholders = ",".join("?" * len(citable))
    rows = conn.execute(
        f"SELECT id, payload, source_url, source_authority, retrieval_method, "
        f"candidate_ref FROM evidence WHERE id IN ({placeholders}) ORDER BY id",
        list(citable),
    ).fetchall()

    lines = []
    for row in rows:
        claim = _json.loads(row["payload"]).get("claim", "")
        via = (
            " · operator-transcribed"
            if row["retrieval_method"] == "operator_transcribed" else ""
        )
        lines.append(
            f"[{row['id']}] {claim}\n"
            f"      from {row['source_url']} [{row['source_authority']}{via}] "
            f"· permits: {citable[row['id']]}"
        )
    return "\n".join(lines)


DRAFT_SYSTEM_PROMPT = """You are writing a marketplace listing for a second-hand item, \
from observations someone else recorded. You are not looking at the item.

Write as the person selling it. Not a brand and not a catalogue: someone who owns \
this thing, knows it, and is telling a stranger what it is and what shape it is in. \
On a second-hand marketplace that voice outsells advertising copy, because the buyer \
is deciding whether to trust a seller as much as they are deciding about the object. \
What you may not do is assert things the record cannot support.

The refusal line is between opinion and fact, not between plain and persuasive:

- "Timeless", "classic", "sharp", "a wardrobe staple" -- allowed. These are opinion, \
and nobody can be misled by them, because they claim nothing checkable. Allowed is \
not the same as worth writing: see the style rules below.
- "Rare", "hard to find", "limited edition", "discontinued", "sought after" -- these \
sound like enthusiasm and function as claims about supply. A buyer can be misled by \
them. Use them only when the evidence establishes them.
- "Mint", "deadstock", "unworn", "authentic", "vintage" -- the same. They describe \
verifiable properties, so they need the evidence that verifies them.
- "An investment", "a bargain", "worth double" -- never. These assert future value or \
a relationship to market price, and nothing establishes either.

Structure your answer in two parts:

`claims` -- every factual assertion, each citing the observation ids behind it. \
Brand, material, measurements, construction, condition, flaws, provenance. A factual \
sentence you cannot cite is invention, however reasonable it sounds.

`marketing_copy` -- positioning notes: who this is for, why it appeals. The buyer \
never sees this field. It is working notes for the seller, and it is the one place \
salesmanship belongs.

`description` is what the buyer reads, and it is **three sentences at most**.

Cover these, in this order, and skip any the record has nothing for: what it is; \
what condition it is in; anything included beyond the item itself; and the one reason \
someone would want this particular one. Four things and three sentences, so some \
sentences carry two. Then stop.

The condition slot is the grade and nothing else -- "used, in excellent condition", \
"new with tags" -- in a handful of words. It is not an opening for an inventory of \
marks. Naming the slot is not permission to fill it; see the condition rules below, \
which the slot does not override.

Never write a sentence whose content is that something is *not* there. "No \
accessories or original packaging are included" is not information: no observation \
said there were any, so you have invented an absence to fill a slot, and the slot is \
not owed a sentence. Skipping it is the correct answer, every time, and the same goes \
for missing barcodes, illegible serial numbers and anything else the record simply \
does not mention.

It is not everything you know, and the limit is what forces the choosing. Leave out \
factory codes, barcodes, production months, internal SKUs, worksheet paperwork, what \
the item is photographed on. These are all true and none of them help anyone decide; \
a description that recites the whole tag buries the two facts that would have sold \
it. The claims list is where the record goes; the description is where the reason to \
buy goes.

Style. This is where drafts keep going wrong, so it is specific:

- Simple sentences. Say the thing; do not build up to it. "The dual screens make it \
easier to frame shots while recording" is the register. "The dual-screen setup that \
makes framing shots easy whether you're behind the camera or in front of it" is not \
-- same fact, wrapped in advertising.
- Never write these, in any conjugation: "designed to", "perfect for", "ideal for", \
"great for", "ready to go", "elevates", "delivers an experience", "whether you're", \
"features that make", "must-have", "boasts", "features an array of", "look no \
further". They are advertising furniture. Any sentence containing one is improved by \
deleting it.
- Concrete beats promotional. "A versatile piece perfect for collectors" tells a \
buyer nothing; "includes the original accessories shown in the photos" is a reason \
to buy. When you are tempted to characterise the item, state a fact about it instead.
- Do not recite specifications. A figure earns its place only if a buyer would decide \
differently for knowing it. Sensor sizes, focal lengths, field-of-view degrees, \
model codes, dimensions read off the bezel -- that is product-page filler, and the \
title has already said which model this is.
- Keep the supported positive details. This is not an instruction to write less or \
to sound flat. Cut the promotional connective tissue and leave the facts that made \
someone want the thing.
- Do not open every listing the same way. "This is a ..." is one opening among \
many, and a shelf of listings that all begin with it is its own tell -- naming the \
thing outright usually reads better. Vary it.
- Contractions are fine. Short sentences are fine. The goal is not more personality; \
the goal is less advertising-copy tone.

On condition:

- Never assert a history. "Tags still attached and the factory basting is intact at \
the vents" is observed and checkable against the photographs; "never worn" is a \
claim about the item's past that no photograph can establish. Positive indicators \
like that are worth a few words when they are what makes the item attractive. \
Negative ones are not: they belong in `claims`.
- The description sells, and it has no sentence to spare on ordinary wear. Scuffs, \
scratches, marks, dust, smudges, light surface wear, "shows signs of use" -- none of \
it goes in the description, in any wording, however briefly, and not as a trailing \
clause on a positive sentence either. Writing "cosmetically excellent, though the \
screen has some dust" is the thing this rule exists to stop. The condition grade on \
the listing already says the item is second-hand. Record every one of these in \
`claims`, where the grade and the photographs are checked against them.
- That is about emphasis, not concealment. If something is broken, missing, or does \
not work -- a cracked screen, a dead battery, a part that is not in the box -- say \
it, plainly, in the description. A buyer who receives that unwarned returns it, and \
they are right to. The line is between a mark on a used thing and a reason it might \
not do what someone is buying it for.
- Where a required aspect had no value, do not paper over it in prose. An absent \
size stays absent; do not imply one.

The title has 80 characters and is mostly a search-matching device: brand, what the \
thing is, then the attributes a buyer would type. One evocative word is worth it if \
the searchable terms still fit.

Call the draft_listing tool exactly once."""


def drafting_stage(
    observations: str,
    aspects: str,
    condition: str,
    max_output_tokens: int = 2000,
) -> StageRequest:
    """Build the listing drafting request. No images: it writes from the record."""
    from resell.reasoning.tools import DRAFT_TOOL_NAME, DRAFT_TOOL_SCHEMA

    instruction = (
        f"Resolved aspects:\n\n{aspects or '(none)'}\n\n"
        f"Item condition: {condition or '(not set)'}\n\n"
        f"Recorded observations:\n\n{observations}\n\n"
        "Write the listing."
    )
    return StageRequest(
        system_prompt=DRAFT_SYSTEM_PROMPT,
        images=(),
        instruction=instruction,
        tool=ToolSpec(
            name=DRAFT_TOOL_NAME,
            description=DRAFT_TOOL_SCHEMA["description"],
            json_schema=DRAFT_TOOL_SCHEMA["input_schema"],
        ),
        max_tokens=max_output_tokens,
    )


CONDITION_SYSTEM_PROMPT = """You are choosing which of a marketplace's condition \
grades best describes a second-hand item, from observations someone else recorded of \
its photographs.

Rules:

- Choose from the list you are given and nothing else. The grades differ by category \
and the list is this category's; a value outside it is refused when the listing is \
published.
- Grade what was observed, not what is likely. "No visible damage in these photos" is \
not the same as "no damage" -- photographs miss things, and the honest grade reflects \
what somebody could actually see.
- Cite the observations behind the choice. A grade is a claim about the object and \
carries the same burden as any other.
- Prefer the lower grade when two fit. A buyer who receives something better than \
described is pleased; the reverse is a return, a refund and a defect on the account.
- Say when the photographs do not settle it. If wear cannot be assessed from what was \
recorded -- no close-ups, key surfaces not shown -- say so in `uncertain_because` and \
still give your best grade. Somebody may then look at the object.

Call the choose_condition tool exactly once."""


def condition_stage(
    observations: str,
    allowed: str,
    category_id: str,
    max_output_tokens: int = 1000,
) -> StageRequest:
    """Build the condition-grading request.

    The allowed grades come from eBay's condition policy for the category, in
    eBay's own wording -- 1000 is "New with tags" in clothing and "Brand New"
    elsewhere, and offering our own vocabulary instead would invite a value the
    marketplace does not accept.
    """
    from resell.reasoning.tools import CONDITION_TOOL_NAME, CONDITION_TOOL_SCHEMA

    instruction = (
        f"Category {category_id} accepts exactly these grades:\n\n{allowed}\n\n"
        f"Recorded observations of the item:\n\n{observations}\n\n"
        "Choose the grade that describes it."
    )
    return StageRequest(
        system_prompt=CONDITION_SYSTEM_PROMPT,
        images=(),
        instruction=instruction,
        tool=ToolSpec(
            name=CONDITION_TOOL_NAME,
            description=CONDITION_TOOL_SCHEMA["description"],
            json_schema=CONDITION_TOOL_SCHEMA["input_schema"],
        ),
        max_tokens=max_output_tokens,
    )


REPAIR_SYSTEM_PROMPT = """You are repairing one listing draft that a deterministic \
review refused. You are not rewriting it.

A draft that fails review is not worthless: usually one phrase or one uncited \
sentence is wrong and the rest is accurate copy that took real evidence to produce. \
Throwing it away and starting again loses that, and tends to reintroduce the same \
error in a new place.

Rules:

- Change only what was named. Every problem you were given identifies a specific \
title, sentence or claim. Leave every other word exactly as it is, including \
punctuation and paragraph breaks. If a sentence was not challenged, return it \
character for character.
- Return the whole draft, not a patch. Title, description, claims and marketing copy \
all come back, with the untouched parts unchanged.
- Prefer deletion to invention. If a phrase asserts something the record does not \
support, the fix is almost always to remove that phrase or narrow it to what the \
record does say -- not to find a different unsupported thing to say instead. A \
title that says less and is true is a good title.
- A value the record does not contain must not reappear anywhere, in any form. \
Rewriting "5-45 lb" as "5 to 45 lb" or "up to 45 lb from 5" is the same claim in \
different words, and will be refused again.
- An uncited claim is repaired by citing the observation that supports it, or by \
deleting the sentence. Do not attach a citation that does not actually support what \
the sentence says -- that is a worse failure than the one you were asked to fix.
- If the only honest repair is to say less, say less. A shorter accurate listing \
beats a longer one that gets refused again.
- Where a problem comes with a count, treat the count as the instruction. "Remove \
at least 16 characters to reach 80" is not a suggestion about tone: 80 is a hard \
limit and a title of 81 is refused exactly as one of 96 is. Cut, then count what is \
left. Fixing a second problem by making the first one worse -- expanding "40R" into \
"Size 40 Regular" on a title that was already too long -- is not a repair, and it \
is rejected and sent back.
- The description stays at three sentences or fewer. Removing a phrase never needs \
a new sentence to replace it, and a repair that grows the copy has stopped being a \
repair.
- Do not restore advertising voice on the way past. The draft is written plain on \
purpose: that is the house style, not an oversight for you to correct. Never \
introduce "designed to", "perfect for", "ideal for", "ready to go", "elevates", \
"delivers an experience", "whether you're", "features that make", or their \
relatives, and where you delete a phrase, close the gap rather than filling it.

Call the draft_listing tool exactly once, with the repaired draft."""


def repair_stage(
    previous_title: str,
    previous_description: str,
    previous_claims: str,
    previous_marketing: str,
    problems: str,
    aspects: str,
    observations: str,
    condition: str,
    arithmetic: str = "",
    max_output_tokens: int = 2000,
) -> StageRequest:
    """Build a targeted repair request for a draft the review refused.

    The previous draft is given back verbatim alongside the exact complaints, so
    the model is editing rather than starting over. That is the difference the
    operator asked for: an unsupported claim must never reach a stored draft, but
    one bad phrase should not discard copy that was fine.

    The record is included again because a repair still has to be checkable
    against it -- the reviewer runs unchanged on the result, and a repair that
    invents a *new* unsupported claim is refused exactly as the first draft was.
    """
    from resell.reasoning.tools import DRAFT_TOOL_NAME, DRAFT_TOOL_SCHEMA

    instruction = (
        f"The review refused this draft for these reasons:\n\n{problems}\n\n"
        + (f"Counts, already worked out for you:\n\n{arithmetic}\n\n"
           if arithmetic else "")
        + f"--- the draft, to repair ---\n\n"
        f"TITLE:\n{previous_title}\n\n"
        f"DESCRIPTION:\n{previous_description}\n\n"
        f"CLAIMS AND THEIR CITATIONS:\n{previous_claims or '(none)'}\n\n"
        f"MARKETING COPY:\n{previous_marketing or '(none)'}\n\n"
        f"--- the record, unchanged ---\n\n"
        f"Resolved aspects:\n\n{aspects or '(none)'}\n\n"
        f"Item condition: {condition or '(not set)'}\n\n"
        f"Recorded observations:\n\n{observations}\n\n"
        "Repair only what was named. Return the rest unchanged."
    )
    return StageRequest(
        system_prompt=REPAIR_SYSTEM_PROMPT,
        images=(),
        instruction=instruction,
        tool=ToolSpec(
            name=DRAFT_TOOL_NAME,
            description=DRAFT_TOOL_SCHEMA["description"],
            json_schema=DRAFT_TOOL_SCHEMA["input_schema"],
        ),
        max_tokens=max_output_tokens,
    )


EXTRACT_SYSTEM_PROMPT = """You are reading one web page and listing what it says about \
the product it describes. You are not identifying anything and not judging anything: \
another stage decides whether this product is the item on the table, and it needs a \
clean record of what this page claims.

Rules:

- Every fact quotes the page. Put the page's own words in `excerpt`, verbatim. A \
paraphrase cannot be checked, and a quotation that is not in the page is discarded \
along with its fact.
- Facts about the product only. Site navigation, cookie notices, delivery policy, \
related products and customer reviews are not facts about this product. A page that \
describes no product yields an empty list, which is a correct answer.
- Separate identity from retail. Identity is what the thing is: model number, \
colourway, materials, dimensions, generation, what is in the box. Retail is what \
someone charges for it: price, discount, shipping cost, stock. Prices are always \
retail even when the page presents them as a specification.
- One claim per fact. "Navy, wool blend, made in Egypt" is three facts with three \
excerpts, not one.
- Do not resolve contradictions and do not fill gaps. If the page gives two model \
numbers, list both and quote both. If it never states the material, say nothing about \
material; inventing a plausible value is the failure this stage exists to avoid.
- Do not describe the object the operator is holding. You have not seen it. \
Everything here is a claim about a product on a page.

Call the extract_candidate_facts tool exactly once."""


MAX_PAGE_CHARS = 24_000


def page_body_for_extraction(page_text: str, max_page_chars: int = MAX_PAGE_CHARS) -> str:
    """The page text the extractor will actually be shown.

    Exported so the caller can validate excerpts against exactly this string. The
    alternative -- checking a quotation against the whole assembled prompt -- would
    accept text lifted from the URL, the query or the motivation, and those are our
    words rather than the page's. The excerpt check is the only thing standing
    behind an automated extraction, so it must compare against the page and nothing
    else.
    """
    return page_text[:max_page_chars]


def extraction_stage(
    page_text: str,
    url: str,
    query: str,
    motivation: str,
    max_output_tokens: int = 2000,
    max_page_chars: int = MAX_PAGE_CHARS,
) -> StageRequest:
    """Build the fact-extraction request for one fetched page.

    The narrowest stage in the system, and the one intended to move to a local model
    first: no images, a small tool, and a job that is closer to parsing than to
    reasoning. It goes through the same `ModelAdapter` protocol as every other
    stage, so that move is a provider argument rather than a new interface.

    The page is truncated, and the truncation is stated in the prompt. A silent cut
    would make a fact absent from a long page indistinguishable from a fact the page
    never carried -- and the parser rejects excerpts it cannot find, so a quotation
    from beyond the cut has to fail visibly rather than look fabricated.
    """
    from resell.reasoning.tools import EXTRACT_TOOL_NAME, EXTRACT_TOOL_SCHEMA

    body = page_body_for_extraction(page_text, max_page_chars)
    truncated = len(page_text) > max_page_chars
    instruction = (
        f"Page URL: {url}\n"
        f"Retrieved for the query: {query}\n"
        f"Which was intended to settle: {motivation}\n\n"
        f"Page text{' (truncated)' if truncated else ''}:\n\n{body}\n\n"
    )
    if truncated:
        instruction += (
            f"The text above is the first {max_page_chars} characters of "
            f"{len(page_text)}. Quote only from what you were given.\n\n"
        )
    instruction += "List what this page states about the product it describes."
    return StageRequest(
        system_prompt=EXTRACT_SYSTEM_PROMPT,
        images=(),
        instruction=instruction,
        tool=ToolSpec(
            name=EXTRACT_TOOL_NAME,
            description=EXTRACT_TOOL_SCHEMA["description"],
            json_schema=EXTRACT_TOOL_SCHEMA["input_schema"],
        ),
        max_tokens=max_output_tokens,
    )


# --- comp research -----------------------------------------------------------

