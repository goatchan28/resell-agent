"""Deciding what a retrieved listing is, without asking a model.

V1 spent three `comp_judge` calls an item to place listings on the comparability
ladder. It judged well and it judged slowly, and the V1 audit found the thing
that actually drives price quality is not how finely a listing is graded but
whether obviously-wrong listings are kept out at all: MP-000053 was priced from
31 generic Brooks Brothers blazers against 2 of the actual product line, and no
amount of careful grading of those 31 would have produced the right number.

So this does the part that matters and declines the part that does not. It
throws out accessories, parts and different models, and everything surviving is
either a family variant or a category-level neighbour. It does not try to be
right about the difference between them in hard cases.

Two rules it will not break:

  - **Nothing is ever `same_product`.** That rung asserts two things are the same
    product, and `validate_claim` refuses it unless identification resolved the
    item -- which has never once happened. An exact model-number match is recorded
    as the strongest *available* rung with the match named in the rationale, so
    the evidence is on the record even though the ladder cannot express it.
  - **Every claim cites both sides**, because `validate_claim` requires it and
    because a claim citing only the listing is a description of a web page.

Pure: no database, no network, no model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from resell.pricing.comps import Comparability

# Words that make a listing something *other than* the product, whatever else the
# title says. Ordered roughly by how often they appeared in V1's excluded
# judgements. "for" is handled separately -- "case for iPhone" is an accessory,
# "iPhone for parts" is a condition -- and neither is a comp for a working item.
_NOT_THE_PRODUCT = (
    "replacement", "spare part", "spare parts", "repair kit", "repair part",
    "accessory", "accessories", "attachment head", "attachment heads",
    "carrying case", "carry case", "hard case", "soft case", "travel case",
    "screen protector", "tempered glass", "lens cap", "lens cover",
    "manual only", "instruction manual", "user manual", "owners manual",
    "empty box", "box only", "case only", "bag only", "strap only",
    "charger only", "cable only", "battery only", "cover only", "lid only",
    "remote only", "stand only", "dock only", "filter", "filters",
    "brush", "brushes", "bag set", "refill", "refills", "replenishment",
    "decal", "sticker", "skin for", "wrap for",
)

# Phrases that mean the listing is a *bundle of many*, which prices a different
# thing. Deliberately short: the V1 bundle rule took three attempts to get right
# and the lesson was that ambiguity belongs at the rung, not the exclusion.
_LOTS = ("bundle of", "wholesale lot", "job lot", "pallet", "x10", "x20")

# "LOT (2)", "LOT of 2", "LOT 2". The plain-phrase version missed the first two,
# and a two-Roomba lot priced a single Roomba at $38.
_LOT_COUNT = re.compile(r"\blot\b\s*(?:of\s*)?\(?\s*(\d+)")

# Nouns for the things a product comes *with*. A listing selling one of these on
# its own is not a comp for the product; three Achedaway attachment heads at
# $15.89-$17.89 were setting a massage gun's Fast price.
_ACCESSORY_NOUNS = frozenset({
    "attachment", "attachments", "head", "heads", "base", "dock", "docks",
    "case", "cases", "manual", "manuals", "charger", "chargers", "cable",
    "cables", "strap", "straps", "cover", "covers", "lid", "lids", "filter",
    "filters", "brush", "brushes", "remote", "stand", "battery", "batteries",
    "adapter", "adapters", "tip", "tips", "mount", "grip",
})

# Words that say the accessory is *included with* the product rather than being
# it. Without these the rule ate the product it was meant to protect: "Massage
# Gun Kit Extra Attachments Case Charger" is a massage gun, and so is a camera
# listed as "Lens + Battery Grip + Charger".
_INCLUSION = ("with", "w/", "includes", "including", "incl", "plus", "kit",
              "extra", "extras", "bundle", "+", "combo", "set of accessories")

# "No Docks" is a statement about what is missing, not about what is for sale.
_ABSENCE = ("no", "not", "without", "missing", "sans")

# Marketplace suffixes that would otherwise sit in the head-noun position and
# hide the accessory noun behind them.
_TITLE_TAIL = re.compile(r"\s*[|\-–]\s*(ebay|amazon|mercari|poshmark|etsy)\s*$", re.I)

# Multi-packs, generically. The replay found a used moisturiser priced partly
# from "Pack of 2" and "2-pack" listings, which are twice the product at roughly
# twice the price -- the single most direct way to inflate a sample. Covers
# "2-pack", "2 pack" and "pack of 2"; a quantity of one is not a multi-pack and
# is left alone.
_MULTIPACK = re.compile(
    r"\b(?:(?P<n>\d+)\s*-?\s*(?:pack|packs|count|ct)\b"
    r"|(?:pack|packs|set)\s+of\s+(?P<m>\d+)\b)"
)


def multipack_of(title: str) -> int | None:
    """How many units the title says it sells, when it says more than one."""
    for match in _MULTIPACK.finditer((title or "").casefold()):
        raw = match.group("n") or match.group("m")
        try:
            count = int(raw)
        except (TypeError, ValueError):
            continue
        if count > 1:
            return count
    return None

# A model number: a token carrying both letters and digits, or a bare number of
# three or more digits. "T6i", "WH-1000XM4", "18-55mm", "980".
_MODEL_TOKEN = re.compile(r"\b(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9][a-z0-9-]{1,}\b|\b\d{3,}\b")

# Tokens that look like model numbers but are measurements or quantities, and
# would otherwise make every listing mismatch every other one.
_NOT_A_MODEL = re.compile(
    r"^\d{1,2}(oz|ml|mm|cm|in|ft|lb|kg|g|w|v|k|gb|tb|mp|x)$|"
    r"^(18-55mm|55-250mm|\d+mm|\d+x\d+|\d+pack|\d+pcs?)$"
)


@dataclass(frozen=True)
class Verdict:
    comparability: Comparability
    rationale: str
    comp_citations: tuple[str, ...]
    excluded_reason: str | None = None
    exact_model: bool = False       # recorded for analysis; the ladder cannot say it


def model_tokens(text: str) -> set[str]:
    """Model-number-ish tokens in a string, lowercased."""
    found = set()
    for match in _MODEL_TOKEN.finditer((text or "").casefold()):
        token = match.group(0).strip("-")
        if len(token) < 2 or _NOT_A_MODEL.match(token):
            continue
        found.add(token)
    return found


def _tokens(text: str) -> list[str]:
    """Words, with punctuation split so `Base/Dock` is two tokens rather than one."""
    return [t for t in re.split(r"[^a-z0-9+/]+", (text or "").casefold()) if t]


def sold_as_accessory(title: str) -> str | None:
    """The accessory noun this listing is selling, if that is what it is selling.

    Three conditions, all required, because each one on its own over-fires:

      - an accessory noun sits in the **head-noun position** -- the last two
        meaningful words. "Dumbbell Base/Dock" ends in the accessory; "Autoempty
        Dock Lidar Self-Emptying" is a vacuum that has one.
      - no word anywhere says the accessory is *included with* something.
      - the noun is not negated. "No Docks" is a vacuum without its dock.
    """
    cleaned = _TITLE_TAIL.sub("", title or "")
    words = _tokens(cleaned)
    if not words:
        return None
    if any(m in words or m in cleaned.casefold() for m in _INCLUSION):
        return None
    for position, word in enumerate(words):
        if position < len(words) - 2:
            continue
        for part in word.split("/"):
            if part not in _ACCESSORY_NOUNS:
                continue
            if position and words[position - 1] in _ABSENCE:
                continue
            return part
    return None


def _has_any(text: str, needles) -> str | None:
    folded = f" {(text or '').casefold()} "
    for needle in needles:
        if f" {needle} " in folded or folded.startswith(f" {needle}") or needle in folded:
            return needle
    return None


def classify(
    *,
    brand: str | None,
    model: str | None,
    item_title: str | None,
    comp_title: str | None,
    item_evidence_ids: tuple[str, ...],
    ceiling: Comparability = Comparability.SAME_FAMILY_VARIANT,
) -> Verdict:
    """What this listing is, relative to the item. Never raises."""
    title = (comp_title or "").strip()
    if not title:
        return Verdict(
            Comparability.EXCLUDED, "", ("title",),
            excluded_reason="the listing has no title to judge",
        )

    # 1. Is it a different kind of object -- a part, an accessory, a manual?
    hit = _has_any(title, _NOT_THE_PRODUCT)
    if hit:
        return Verdict(
            Comparability.EXCLUDED, "", ("title",),
            excluded_reason=f"an accessory or part, not the product: title says {hit!r}",
        )
    noun = sold_as_accessory(title)
    if noun:
        return Verdict(
            Comparability.EXCLUDED, "", ("title",),
            excluded_reason=f"the listing is selling a {noun}, not the product",
        )

    # 2. Is the sale unit many of them?
    hit = _has_any(title, _LOTS)
    if hit:
        return Verdict(
            Comparability.EXCLUDED, "", ("title",),
            excluded_reason=f"a multi-unit lot, not one item: title says {hit!r}",
        )
    lot = _LOT_COUNT.search(title.casefold())
    if lot and int(lot.group(1)) > 1:
        return Verdict(
            Comparability.EXCLUDED, "", ("title",),
            excluded_reason=f"a lot of {lot.group(1)}, not one item",
        )
    count = multipack_of(title)
    if count:
        return Verdict(
            Comparability.EXCLUDED, "", ("title",),
            excluded_reason=f"a {count}-unit pack, not one item",
        )

    folded = title.casefold()
    brand_ok = bool(brand) and brand.strip().casefold() in folded

    # 3. Model numbers. A listing naming a *different* model of the same brand is
    #    a different product; one naming the item's model is the strongest match
    #    available. Only decided when the item itself has a model number to
    #    compare -- otherwise there is nothing to mismatch against.
    item_models = model_tokens(f"{model or ''} {item_title or ''}")
    comp_models = model_tokens(title)
    shared = item_models & comp_models
    if item_models and comp_models and not shared:
        return Verdict(
            Comparability.EXCLUDED, "", ("title",),
            excluded_reason=(
                f"a different model: listing says {sorted(comp_models)[:3]}, "
                f"the item is {sorted(item_models)[:3]}"
            ),
        )

    if shared and brand_ok:
        return Verdict(
            min(ceiling, Comparability.SAME_FAMILY_VARIANT, key=lambda c: c.rank),
            f"brand {brand!r} and model {sorted(shared)[0]!r} both appear in the "
            f"listing title",
            ("title",), exact_model=True,
        )

    # 4. Brand and product wording, without a model to confirm it.
    if brand_ok:
        return Verdict(
            min(ceiling, Comparability.SAME_FAMILY_VARIANT, key=lambda c: c.rank),
            f"brand {brand!r} appears in the listing title; no model number to "
            f"confirm the exact variant",
            ("title",),
        )

    # 5. Everything else is kept as a neighbour rather than classified further.
    #    This is the deliberate floor: the audit showed precision comes from
    #    excluding the wrong things, not from grading the rest finely.
    return Verdict(
        Comparability.CATEGORY_ATTRIBUTE,
        "same kind of product; the brand is not named in the listing title",
        ("title",),
    )
