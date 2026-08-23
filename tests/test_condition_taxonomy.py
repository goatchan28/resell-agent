"""Condition wording, resolved to eBay's ids rather than guessed at.

The Canon EOS Rebel T6i is why this exists. Its comps declared `New` -- the
commonest condition string on any marketplace -- and the flat phrase list that
comp research used had `new with tags`, `brand new` and `new other` but not bare
`new`. So every one of those listings landed at `unknown`, which has no rung on
the ladder and cannot be compared to anything, and the item priced from nothing.

A flat list has to enumerate every wording anyone might write, which is a losing
game. Resolving to an eBay condition id and deriving the band from that is not.

The awkward part, which the tests below pin down rather than paper over: eBay's
ids mean slightly different things in different categories. Id 1000 is "New with
tags" in clothing and plain "New" for a camera; 1500 is "New (other)" nearly
everywhere and "New without tags" in apparel; 3000's *enum name* is
USED_EXCELLENT while its *label* in almost every category is "Pre-owned". The id
is canonical precisely because no single name is.
"""

from __future__ import annotations

import pytest

from resell.pricing.comps import ConditionBand
from resell.pricing.condition import (
    BY_ID,
    EBAY_CONDITIONS,
    allowed_in,
    condition_for_band,
    normalise_condition,
)


# --- the failure that prompted this ------------------------------------------------


def test_bare_new_is_recognised():
    """`New` appeared twice in the Canon trace and became `unknown` both times."""
    match = normalise_condition("New")
    assert match.condition_id == 1000
    assert match.band is ConditionBand.NEW_WITH_TAGS
    assert match.resolved


def test_bare_new_no_longer_reaches_the_comp_reader_as_unknown():
    """The path that actually failed, not just the new function."""
    from resell.reasoning.comp_reading import band_for_declared_condition

    band, why = band_for_declared_condition("New")
    assert band is not ConditionBand.UNKNOWN
    assert "matches no known condition wording" not in why


# --- the vocabulary ----------------------------------------------------------------


@pytest.mark.parametrize("wording,condition_id", [
    ("New", 1000),
    ("Brand New", 1000),
    ("New with tags", 1000),
    ("NWT", 1000),
    ("Factory sealed", 1000),
    ("New other", 1500),
    ("New without tags", 1500),
    ("Open box", 1500),
    ("New with defects", 1750),
    ("Certified refurbished", 2000),
    ("Seller refurbished", 2500),
    ("Renewed", 2500),
    ("Like new", 2750),
    ("Used", 3000),
    ("Pre-Owned", 3000),
    ("Very Good", 4000),
    ("Good", 5000),
    ("Acceptable", 6000),
    ("For parts or not working", 7000),
    ("As-is", 7000),
])
def test_wording_resolves_to_the_right_id(wording, condition_id):
    assert normalise_condition(wording).condition_id == condition_id


def test_a_longer_phrase_beats_the_shorter_one_inside_it():
    """"New without tags" contains "new". Matching the short one first would price
    a tagless garment as though it were sealed."""
    assert normalise_condition("New without tags").condition_id == 1500
    assert normalise_condition("Very good").condition_id == 4000   # not "good"


def test_condition_wording_inside_a_longer_sentence_still_resolves():
    match = normalise_condition("Condition: Pre-Owned, some shelf wear")
    assert match.condition_id == 3000
    assert match.matched == "pre-owned"


def test_ebays_own_enum_resolves():
    """API-sourced comps arrive carrying the enum, not prose."""
    assert normalise_condition("USED_GOOD").condition_id == 5000
    assert normalise_condition("FOR_PARTS_OR_NOT_WORKING").condition_id == 7000


def test_a_numeric_id_resolves():
    """The Browse API sends `conditionId` as a string of digits."""
    match = normalise_condition("3000")
    assert match.condition_id == 3000
    assert match.band is ConditionBand.USED_GOOD


def test_nonsense_is_still_unknown():
    """The guard that matters: a wording nobody recognises must not be guessed
    onto the middle of the ladder, where it would join a sample it does not
    belong to."""
    match = normalise_condition("gently loved, see pics")
    assert match.condition_id is None
    assert match.band is ConditionBand.UNKNOWN
    assert not match.resolved


def test_nothing_stated_is_distinct_from_unrecognised():
    assert "no condition was stated" in normalise_condition("").why
    assert "no condition was stated" in normalise_condition(None).why


# --- where the id and the label disagree ------------------------------------------


def test_the_generic_used_id_does_not_claim_to_be_excellent():
    """3000's Sell API enum is USED_EXCELLENT and its label in nearly every
    category is "Pre-owned". It is what most used listings carry, so banding it to
    the top used rung would inflate the entire second-hand market."""
    assert normalise_condition("Pre-owned").band is ConditionBand.USED_GOOD
    assert normalise_condition("Used").band is ConditionBand.USED_GOOD


def test_graded_wording_keeps_its_grade():
    """A seller who wrote "Excellent" said more than one who wrote "Used", and the
    band has to keep the difference."""
    assert normalise_condition("Excellent").band is ConditionBand.USED_EXCELLENT
    assert normalise_condition("Used").band is ConditionBand.USED_GOOD


def test_one_id_with_two_category_labels_keeps_the_finer_reading():
    """"New without tags" and "Open box" are both eBay 1500. The id cannot tell
    them apart without a category; the seller's own words can."""
    tagless = normalise_condition("New without tags")
    opened = normalise_condition("Open box")
    assert tagless.condition_id == opened.condition_id == 1500
    assert tagless.band is ConditionBand.NEW_WITHOUT_TAGS
    assert opened.band is ConditionBand.NEW_OTHER


# --- category policy, which is a separate question ---------------------------------


class FakeOption:
    def __init__(self, condition_id):
        self.condition_id = str(condition_id)


class FakePolicy:
    def __init__(self, category_id, ids):
        self.category_id = category_id
        self.options = tuple(FakeOption(i) for i in ids)


def test_a_condition_the_category_does_not_offer_is_refused():
    """Refurbished is absent from most categories. Setting one anyway is a publish
    failure discovered at the worst possible moment."""
    policy = FakePolicy("31388", [1000, 1500, 3000, 7000])
    ok, why = allowed_in(policy, 2500)
    assert not ok
    assert "not offered in category 31388" in why
    assert "New" in why          # says what the category does allow


def test_a_condition_the_category_offers_is_allowed():
    policy = FakePolicy("31388", [1000, 1500, 3000, 7000])
    assert allowed_in(policy, 3000)[0]


def test_reading_a_comp_never_asks_the_category():
    """A comp's condition is a fact about someone else's listing. Refusing to
    understand "Open box" because our category will not let us list one would be
    nonsense, so normalisation takes no policy at all."""
    import inspect

    assert "policy" not in inspect.signature(normalise_condition).parameters


def test_no_policy_is_not_a_refusal():
    """The metadata call is a convenience. Failing it must not block publishing,
    but the caller can see which answer it got."""
    ok, why = allowed_in(None, 1000)
    assert ok
    assert "nothing was checked" in why


# --- the table itself ---------------------------------------------------------------


def test_every_condition_bands_somewhere():
    for entry in EBAY_CONDITIONS:
        match = normalise_condition(str(entry.condition_id))
        assert match.band is not ConditionBand.UNKNOWN, entry


def test_the_ids_match_ebays_publishing_enum():
    """Two tables in two modules describe the same fourteen conditions. If they
    drift, an id that prices correctly will fail to publish."""
    from resell.ebay.publisher import CONDITION_ID_TO_ENUM

    assert {str(c.condition_id) for c in EBAY_CONDITIONS} == set(CONDITION_ID_TO_ENUM)
    for entry in EBAY_CONDITIONS:
        assert CONDITION_ID_TO_ENUM[str(entry.condition_id)] == entry.enum_value


def test_a_band_can_be_turned_back_into_an_id():
    assert BY_ID[condition_for_band(ConditionBand.FOR_PARTS)].enum_value == (
        "FOR_PARTS_OR_NOT_WORKING"
    )
    assert condition_for_band(ConditionBand.UNKNOWN) is None


def test_the_reverse_direction_is_stated_not_incidental():
    """It used to return whichever id came first in the table, which left
    `new_without_tags` with no id at all once 1500 was rebanded."""
    from resell.pricing.condition import BAND_TO_CONDITION_ID

    for band in ConditionBand:
        if band is ConditionBand.UNKNOWN:
            assert condition_for_band(band) is None
            continue
        assert band in BAND_TO_CONDITION_ID, band
        assert condition_for_band(band) in BY_ID


def test_a_band_that_maps_to_a_condition_may_still_be_disallowed():
    """The two questions stay separate all the way through: a band becomes a
    condition, and a condition is not necessarily on offer where we are listing."""
    policy = FakePolicy("31388", [1000, 1500, 3000, 7000])
    refurb = condition_for_band(ConditionBand.REFURBISHED)
    assert refurb == 2500
    assert not allowed_in(policy, refurb)[0]
