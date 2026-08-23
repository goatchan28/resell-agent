"""eBay condition IDs as the canonical vocabulary, and how text becomes one.

Three representations were already in the codebase and nothing joined them up:
eBay's numeric ids (`CONDITION_ID_TO_ENUM`, used when publishing), the internal
`ConditionBand` ladder (used when pricing), and a flat list of phrases in
`comp_reading` (used when reading someone else's listing). The phrase list was
the odd one out -- it mapped words straight to a band, so it had to enumerate
every wording anyone might use, and it did not know that "New" and "New with
tags" are the same eBay condition seen from two categories.

The Canon EOS Rebel T6i is what made that concrete. Its comps said `New`, which
was in none of the phrases -- `new with tags`, `brand new`, `new other` were, bare
`new` was not -- so the most common condition string on any marketplace landed at
`unknown`, which has no rung on the ladder and cannot be compared to anything.

So text now resolves to an **eBay condition id**, and the band is derived from
that. One table, one direction, and the id is the thing that travels.

Category dependence, which is real and is why this is structured rather than flat
-------------------------------------------------------------------------------
eBay's conditions are not universal. Id 1000 is "New with tags" in a clothing
category and plain "New" in electronics; 1500 is "New without tags" for apparel
and "New (other)" elsewhere; refurbished ids are absent from most categories
entirely. So a category's *labels* and its *allowed set* both come from eBay, per
category, via `get_item_condition_policies` -- which `EbayPublisher.condition_policy`
already calls.

This module therefore does two separable things and never conflates them:

  `normalise_condition(text)`      what condition somebody's words describe
  `allowed_in(policy, condition)`  whether this category permits it

Only the second needs eBay. Reading a stranger's listing needs the first alone: a
comp's condition is a fact about their listing, not something we are going to
publish, and refusing to understand "Open box" because our category forbids
listing one would be nonsense. When we set *our own* item's condition, both apply.
"""

from __future__ import annotations

from dataclasses import dataclass

from resell.pricing.comps import ConditionBand, band_for_condition_id

__all__ = [
    "CONDITION_ALIASES", "ConditionMatch", "EbayCondition", "allowed_in",
    "condition_for_band", "normalise_condition",
]


@dataclass(frozen=True)
class EbayCondition:
    """One of eBay's conditions. `label` is the general-category name.

    The label is deliberately not authoritative: eBay returns a
    `conditionDescription` per category and that one wins wherever a category is
    known. This is what to call it when nothing better is available.
    """

    condition_id: int
    enum_value: str
    label: str


EBAY_CONDITIONS: tuple[EbayCondition, ...] = (
    EbayCondition(1000, "NEW", "New"),
    EbayCondition(1500, "NEW_OTHER", "New (other)"),
    EbayCondition(1750, "NEW_WITH_DEFECTS", "New with defects"),
    EbayCondition(2000, "CERTIFIED_REFURBISHED", "Certified refurbished"),
    EbayCondition(2010, "EXCELLENT_REFURBISHED", "Excellent refurbished"),
    EbayCondition(2020, "VERY_GOOD_REFURBISHED", "Very good refurbished"),
    EbayCondition(2030, "GOOD_REFURBISHED", "Good refurbished"),
    EbayCondition(2500, "SELLER_REFURBISHED", "Seller refurbished"),
    EbayCondition(2750, "LIKE_NEW", "Like New"),
    # The labels below are eBay's general-category display names, which is where
    # the enum names are at their most misleading: 3000 is shown as "Used", not
    # "Used - excellent". The graded rungs are mostly books and media.
    EbayCondition(3000, "USED_EXCELLENT", "Used"),
    EbayCondition(4000, "USED_VERY_GOOD", "Very Good"),
    EbayCondition(5000, "USED_GOOD", "Good"),
    EbayCondition(6000, "USED_ACCEPTABLE", "Acceptable"),
    EbayCondition(7000, "FOR_PARTS_OR_NOT_WORKING", "For parts or not working"),
)

BY_ID: dict[int, EbayCondition] = {c.condition_id: c for c in EBAY_CONDITIONS}
BY_ENUM: dict[str, EbayCondition] = {c.enum_value: c for c in EBAY_CONDITIONS}


# Phrases sellers actually write, against the id they mean. Matched longest-first,
# so "new with tags" is not swallowed by "new" and "very good" is not swallowed by
# "good" -- the reason the old flat list was order-dependent and fragile.
#
# Apparel wordings sit on the ids eBay uses for apparel: "new with tags" is 1000
# there, and "new without tags" is 1500. That is the same pair of ids a camera
# category calls "New" and "New (other)", which is precisely why the id is the
# canonical thing and the label is not.
CONDITION_ALIASES: dict[str, int] = {
    # 1000 -- new, however it is phrased
    "new": 1000,
    "brand new": 1000,
    "new with tags": 1000,
    "new w/ tags": 1000,
    "nwt": 1000,
    "new in box": 1000,
    "new in sealed box": 1000,
    "brand new in box": 1000,
    "bnib": 1000,
    "new sealed": 1000,
    "sealed": 1000,
    "factory sealed": 1000,
    "unopened": 1000,
    "unused": 1000,
    # 1500 -- new but not as the factory sent it
    "new other": 1500,
    "new (other)": 1500,
    "new without tags": 1500,
    "new w/o tags": 1500,
    "nwot": 1500,
    "new without box": 1500,
    "open box": 1500,
    "open-box": 1500,
    "openbox": 1500,
    # 1750
    "new with defects": 1750,
    "new with flaws": 1750,
    # refurbished, which most categories do not offer at all
    "certified refurbished": 2000,
    "manufacturer refurbished": 2000,
    "excellent refurbished": 2010,
    "very good refurbished": 2020,
    "good refurbished": 2030,
    "seller refurbished": 2500,
    "refurbished": 2500,
    "refurb": 2500,
    "renewed": 2500,
    # 2750
    "like new": 2750,
    "mint": 2750,
    "as new": 2750,
    # Used, down the ladder. Two kinds of wording are deliberately split here.
    #
    # Graded words -- "excellent", "very good", "good", "acceptable" -- go to the
    # graded ids, whose bands are the graded rungs. Plain "used" and "pre-owned"
    # go to 3000, the generic used condition almost every category shows, which
    # bands to the middle. Sending "used" to a graded id would read a grade nobody
    # gave; sending "excellent" to 3000 would throw away one they did.
    #
    # "excellent" lands on 4000 rather than 3000 for that reason, even though 4000's
    # label is "Used - very good": the band is what a comparison uses, and the band
    # is right. The label is only ever a display of a category we may not know.
    "used - excellent": 4000,
    "used excellent": 4000,
    "excellent": 4000,
    "excellent condition": 4000,
    "used - very good": 4000,
    "used very good": 4000,
    "very good": 4000,
    "used - good": 5000,
    "used good": 5000,
    "good": 5000,
    "good condition": 5000,
    "used": 3000,
    "pre-owned": 3000,
    "preowned": 3000,
    "pre owned": 3000,
    "second hand": 3000,
    "secondhand": 3000,
    "gently used": 3000,
    "used - acceptable": 6000,
    "acceptable": 6000,
    "fair": 6000,
    "fair condition": 6000,
    "heavily worn": 6000,
    "well used": 6000,
    "poor": 6000,
    # 7000
    "for parts or not working": 7000,
    "for parts": 7000,
    "parts only": 7000,
    "parts or repair": 7000,
    "not working": 7000,
    "does not work": 7000,
    "spares or repair": 7000,
    "spares or repairs": 7000,
    "as-is": 7000,
    "as is": 7000,
    "broken": 7000,
    "faulty": 7000,
    "defective": 7000,
    "untested": 7000,
}

# A few phrases say more than their id can hold. "New without tags" and "Open box"
# are both eBay 1500 -- one id whose label is category-dependent -- but the words
# themselves are unambiguous, and a tagless garment is a rung above an opened box.
# The id stays canonical for anything that travels to eBay; the band takes the
# finer reading when the seller's own words supply one.
ALIAS_BAND_OVERRIDES: dict[str, ConditionBand] = {
    "new without tags": ConditionBand.NEW_WITHOUT_TAGS,
    "new w/o tags": ConditionBand.NEW_WITHOUT_TAGS,
    "nwot": ConditionBand.NEW_WITHOUT_TAGS,
    "new without box": ConditionBand.NEW_WITHOUT_TAGS,
}


# Longest first: a phrase that contains another must win, or "new without tags"
# reads as "new" and a garment nobody can sell as new is priced as one.
_ALIASES_BY_LENGTH: tuple[tuple[str, int], ...] = tuple(
    sorted(CONDITION_ALIASES.items(), key=lambda kv: -len(kv[0]))
)


@dataclass(frozen=True)
class ConditionMatch:
    """What a condition string resolved to, and how confidently.

    `condition_id` is None when nothing matched, which stays distinct from
    matching something the category disallows -- one is "we do not understand
    this", the other is "we understand it and it is not on offer here".
    """

    condition_id: int | None
    band: ConditionBand
    why: str
    matched: str = ""

    @property
    def resolved(self) -> bool:
        return self.condition_id is not None

    @property
    def enum_value(self) -> str | None:
        entry = BY_ID.get(self.condition_id) if self.condition_id else None
        return entry.enum_value if entry else None

    @property
    def label(self) -> str:
        entry = BY_ID.get(self.condition_id) if self.condition_id else None
        return entry.label if entry else "Unknown"


def normalise_condition(declared: str | None) -> ConditionMatch:
    """Resolve a seller's condition wording to an eBay condition id.

    No category is consulted and none is needed: this answers what the words mean,
    not whether we could list one. `allowed_in` is the separate question.
    """
    text = (declared or "").strip()
    if not text:
        return ConditionMatch(None, ConditionBand.UNKNOWN, "no condition was stated")

    folded = " ".join(text.casefold().replace("_", " ").split())

    # Exact first, so "new" is 1000 rather than matching some longer phrase that
    # happens to contain it.
    if folded in CONDITION_ALIASES:
        return _match(folded, CONDITION_ALIASES[folded], text, exact=True)

    # eBay's own enum, which arrives on API-sourced comps.
    upper = text.strip().upper().replace(" ", "_")
    if upper in BY_ENUM:
        entry = BY_ENUM[upper]
        return ConditionMatch(
            entry.condition_id, band_for_condition_id(entry.condition_id),
            f"{text!r} is eBay's {entry.enum_value}", upper,
        )

    # A numeric id, which is what the Browse API sends.
    if folded.isdigit() and int(folded) in BY_ID:
        entry = BY_ID[int(folded)]
        return ConditionMatch(
            entry.condition_id, band_for_condition_id(entry.condition_id),
            f"condition id {entry.condition_id} is {entry.label}", folded,
        )

    # Then the longest phrase contained in the text, so "Condition: Pre-Owned,
    # some wear" still resolves.
    for phrase, condition_id in _ALIASES_BY_LENGTH:
        if phrase in folded:
            return _match(phrase, condition_id, text, exact=False)

    return ConditionMatch(
        None, ConditionBand.UNKNOWN,
        f"{text!r} matches no known condition wording; left unknown rather than "
        f"guessed onto the ladder",
    )


def _match(phrase: str, condition_id: int, text: str, *, exact: bool) -> ConditionMatch:
    band = ALIAS_BAND_OVERRIDES.get(phrase) or band_for_condition_id(condition_id)
    verb = "is" if exact else "reads as"
    label = BY_ID[condition_id].label
    # The band is named too. A seller's word and eBay's nearest condition are often
    # not the same word -- "Excellent" maps to eBay's "Very Good" -- and an
    # operator reading this should see both rather than wonder which one moved.
    return ConditionMatch(
        condition_id, band,
        f"{text.strip()!r} {verb} eBay {label} ({condition_id}), banded {band}",
        phrase,
    )


def allowed_in(policy, condition_id: int | None) -> tuple[bool, str]:
    """Whether a category permits this condition, given eBay's policy for it.

    Separate from understanding the words, and only asked about our own item.
    `policy` is an `ebay.publisher.ConditionPolicy`; anything with an
    `options` sequence of objects carrying `condition_id` will do, which keeps
    this module free of an import from the eBay client.

    A policy we do not have is not permission. It returns True with a reason
    saying so, because refusing every condition when the metadata call has not
    been made would block publishing on a lookup that is a convenience -- but the
    caller can see which answer it got.
    """
    if policy is None:
        return True, "no category policy loaded, so nothing was checked"
    allowed = {int(o.condition_id) for o in getattr(policy, "options", ()) or ()
               if str(getattr(o, "condition_id", "")).isdigit()}
    if not allowed:
        return True, "the category lists no conditions, so nothing was checked"
    if condition_id is None:
        return False, "no condition to check"
    if condition_id in allowed:
        return True, f"{condition_id} is allowed in category {policy.category_id}"
    names = ", ".join(
        BY_ID[i].label for i in sorted(allowed) if i in BY_ID
    )
    return False, (
        f"{BY_ID.get(condition_id).label if condition_id in BY_ID else condition_id} "
        f"is not offered in category {policy.category_id}. That category allows: "
        f"{names or sorted(allowed)}"
    )


# The lossy direction, stated rather than left to whichever id happens to come
# first. Several ids share a band -- every refurbished flavour, and both new-ish
# rungs -- so this cannot round-trip, and the choice of representative is a
# judgement that belongs in one place where it can be read.
BAND_TO_CONDITION_ID: dict[ConditionBand, int] = {
    ConditionBand.NEW_WITH_TAGS: 1000,
    # 1500 for both: eBay has one id for "New (other)" and "New without tags", and
    # which label a category shows is eBay's business, not ours.
    ConditionBand.NEW_WITHOUT_TAGS: 1500,
    ConditionBand.NEW_OTHER: 1500,
    ConditionBand.REFURBISHED: 2500,   # seller refurbished: what we would be
    ConditionBand.USED_EXCELLENT: 2750,
    ConditionBand.USED_GOOD: 3000,     # the generic used condition
    ConditionBand.USED_FAIR: 6000,
    ConditionBand.FOR_PARTS: 7000,
}


def condition_for_band(band: ConditionBand) -> int | None:
    """The id we would use for one of our own bands. Deliberately lossy.

    Turning a band we hold into something eBay will accept. The result still has
    to pass `allowed_in` for the item's category: a band maps to a condition, and
    a condition is not necessarily on offer where we are listing.
    """
    return BAND_TO_CONDITION_ID.get(band)
