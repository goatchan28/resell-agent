"""MP-000041: four questions, three of which the evidence already answered.

The item was an XD Design Bobby Hero backpack. `map_aspects` proposed Brand,
Color and Material correctly and all three were refused by the citation checks,
so its owner was asked for facts printed on the object and already written down:

    Brand    'XD Design'      cited [1376, 1381, 1393, 1394] -- all read "XDDESIGN"
    Color    'Gray'           cited [1374, 1390] -- both read "grey"
    Material '100% Polyester' cited [1384] -- which reads "recycled polyester"

The first two are the same value written differently: one space, and one letter.
`value_appears_in` was a casefolded substring test with a nine-entry synonym
table, so "xd design" was not in "xddesign" and "gray" was not in "grey".

The third is different and the refusal was right -- "100%" is a composition claim
and nothing observed it -- but `Polyester` was an allowed value sitting in the
same citation, so the aspect was answerable without asking anyone. The owner was
asked, and typed "recycled origin".

The fourth, Department, stays a question: the only evidence naming it is
`candidate_product_fact` from products this item was never matched to, and that
boundary is deliberate.
"""

from __future__ import annotations

import pytest

from resell.reasoning.gaps import (
    detect_uncited_value,
    detect_value_substitution,
    supported_generalisation,
    value_appears_in,
)

# The observations, verbatim from the evidence table.
E1374 = ("The backpack has a dark grey/charcoal front panel with a lighter grey "
         "heathered fabric edging and body.")
E1376 = ("Below the CERTIK logo, the text 'XDDESIGN' appears in smaller white "
         "lettering.")
E1381 = ("A rectangular label reading 'XDDESIGN' is stitched onto the left "
         "shoulder strap.")
E1390 = "The overall color scheme of the backpack is grey and black."
E1384 = ("A hang tag shaped like a water bottle is attached to the bag, reading "
         "'Made from Recycled water bottles', 'Each Bobby Hero reused 28 water "
         "bottles', 'saved: 16 L water', and 'XD Design bags produced with Aware "
         "recycled polyester. Trusted recycled content, validated impact.'")


# --- Brand: one space ------------------------------------------------------------------


@pytest.mark.parametrize("text", [E1376, E1381])
def test_the_brand_is_named_by_the_observations_that_read_it(text):
    assert value_appears_in("XD Design", text)


def test_the_brand_candidate_is_no_longer_miscited():
    """It cited the logo and the sewn label -- the strongest evidence there was --
    and was told to cite a hang tag instead."""
    assert detect_uncited_value(
        "Brand", "XD Design", (1376, 1381),
        {1376: E1376, 1381: E1381, 1384: E1384},
    ) is None


# --- Color: one letter -----------------------------------------------------------------


@pytest.mark.parametrize("text", [E1374, E1390])
def test_the_colour_is_named_by_the_observations_that_saw_it(text):
    assert value_appears_in("Gray", text)


def test_the_colour_candidate_is_no_longer_read_as_a_swap():
    """The check found 'Black' in "grey and black", could not find 'Gray', and
    concluded the value had been substituted."""
    assert detect_value_substitution(
        "Color", "Gray", f"{E1374} {E1390}", ("Gray", "Black", "Navy"),
    ) is None


def test_a_real_swap_is_still_caught():
    """The guard this must not disable. Nothing in the text says grey."""
    assert detect_value_substitution(
        "Color", "Gray", "The bag is black and navy.", ("Gray", "Black", "Navy"),
    )


# --- Material: narrower, not different -------------------------------------------------


def test_the_material_claim_is_still_refused():
    """"100%" is a composition claim and the tag does not make it."""
    assert value_appears_in("100% Polyester", E1384) is None


def test_the_material_falls_back_to_what_the_tag_says():
    assert supported_generalisation(
        "100% Polyester", E1384, ("100% Polyester", "Polyester", "Nylon"),
    ) == "Polyester"


def test_narrowing_never_becomes_a_different_value():
    """`Gray` may not fall back to `Black`. Only a value the proposal contains."""
    assert supported_generalisation(
        "Gray", "the scheme is black", ("Gray", "Black"),
    ) is None


def test_the_longest_supported_narrowing_wins():
    assert supported_generalisation(
        "Full Grain Leather", "made of grain leather",
        ("Full Grain Leather", "Grain Leather", "Leather"),
    ) == "Grain Leather"


# --- what must still be refused --------------------------------------------------------


def test_two_fibres_are_still_two_fibres():
    """The Elastane/Elastodiene swap this check was built for. Rubber-based
    versus polyurethane-based, both legal eBay values."""
    assert value_appears_in("Elastodiene", "the tag reads 4% Elastane") is None
    assert detect_value_substitution(
        "Material", "Elastodiene", "the tag reads 4% Elastane",
        ("Elastane", "Elastodiene", "Spandex"),
    )


def test_a_parent_company_is_still_not_the_brand():
    """MP-000005: Brand 'Beats by Dr. Dre' cited from a panel reading 'Apple Inc.'"""
    assert value_appears_in("Beats by Dr. Dre", "the panel reads Apple Inc.") is None


def test_squeezing_spaces_does_not_widen_single_token_values():
    """The new path asks one question -- "is this multi-word value written as one
    word?" -- so it only runs for a value containing a space. Without that guard
    "Gap" would start finding itself inside "flagship"."""
    assert value_appears_in("Gap", "the flagship model") is None


def test_the_plain_substring_rule_is_unchanged_and_still_loose():
    """Recorded rather than fixed. `value_appears_in` has always matched a bare
    substring, so a short value can be found inside a longer word -- "Ted" in
    "dented". Nothing here made that worse and nothing here made it better; it
    predates MP-000041 and did not cause it. Tightening it to word boundaries is
    a separate change with its own risk ("Navy" is cited from "navy blue" all the
    time), so it is written down here instead of quietly altered."""
    assert value_appears_in("Ted", "the item is dented") == "Ted"


def test_the_spelling_table_is_spellings_and_not_meanings():
    """Kept apart from VALUE_SYNONYMS, which pairs different words for the same
    thing. Everything here is one word written two ways."""
    from resell.reasoning.gaps import SPELLING_VARIANTS

    for written, canonical in SPELLING_VARIANTS.items():
        assert written != canonical
        # same first letter and similar length: a spelling, not a synonym
        assert written[0] == canonical[0]
        assert abs(len(written) - len(canonical)) <= 2


# --- Department stays a question -------------------------------------------------------


def test_department_is_not_rescued_by_any_of_this():
    """Its only support is `candidate_product_fact` -- "Both models are unisex" --
    from products the item was never matched to. `map_aspects` was shown 26 of
    224 evidence rows for exactly that reason, and returned `not_observed`, which
    was honest. None of these changes touch that boundary."""
    assert value_appears_in("Unisex Adult", "Both models are unisex") is None
    assert supported_generalisation(
        "Unisex Adult", "Both models are unisex", ("Unisex Adult", "Men", "Women"),
    ) is None
