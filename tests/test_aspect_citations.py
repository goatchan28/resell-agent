"""Citation checks on a mapping proposal: does the cited evidence say it?

The case that prompted these: MP-000005 is a Beats speaker. Two observations read
the brand off the front and a third names it as an inference. The mapping proposed
Brand correctly and cited the regulatory panel on the bottom, which reads "Apple
Inc." -- true, related, and only supportive if you already know Apple owns Beats.

Two faults were in the way. The correct citation was *also* being rejected,
because eBay's value is "Beats by Dr. Dre" and no observation contains that
string; and the rejection the operator did see named 'Apple' as the value the
evidence supported, which is the opposite of what happened.
"""

from __future__ import annotations

from resell.reasoning.gaps import (
    detect_uncited_value,
    detect_value_substitution,
    missing_synonyms,
    synonym_for,
    value_appears_in,
)

# The observations as the mapper sees them, verbatim in shape: the payload JSON.
BRAND_FRONT = 88, '{"claim": "The item is a Beats Pill portable Bluetooth speaker."}'
BRAND_LOGO = 91, '{"claim": "A silver Beats b logo is centered on the front."}'
PANEL = 96, '{"claim": "The bottom panel reads Apple Inc. Model: A3211 FCC ID:BCGA3211"}'
INFERENCE = 99, '{"claim": "The item is manufactured by Apple Inc. under the Beats brand."}'
SHAPE = 90, '{"claim": "The speaker has a pill/capsule shape with rounded ends."}'

EVIDENCE = dict([BRAND_FRONT, BRAND_LOGO, PANEL, INFERENCE, SHAPE])

BRAND_VALUES = ("Unbranded", "Apple", "Apple iPod", "Beats by Dr. Dre", "Sony", "JBL")


def cited(*ids: int) -> str:
    return " ".join(EVIDENCE[i] for i in ids)


# --- the brand alias -----------------------------------------------------------


def test_ebays_full_brand_form_is_supported_by_the_short_one_on_the_object():
    """eBay records "Beats by Dr. Dre"; the speaker just says "Beats"."""
    assert value_appears_in("Beats by Dr. Dre", EVIDENCE[88]) == "beats"


def test_the_short_form_read_off_the_item_resolves_to_ebays_value():
    assert synonym_for("Beats", BRAND_VALUES) == "Beats by Dr. Dre"


def test_a_brand_the_evidence_names_but_nobody_proposed_is_reported():
    overlooked = missing_synonyms(cited(88, 91), BRAND_VALUES, proposed=set())
    assert any("Beats by Dr. Dre" in entry for entry in overlooked)


# --- the correct citation must be accepted -------------------------------------
#
# This is the regression that mattered most. Rejecting a bad citation is only
# useful if the good one gets through, and it did not.


def test_the_correct_citation_is_accepted():
    assert detect_uncited_value(
        "Brand", "Beats by Dr. Dre", (88,), EVIDENCE
    ) is None
    assert detect_value_substitution(
        "Brand", "Beats by Dr. Dre", cited(88), BRAND_VALUES
    ) is None


def test_citing_every_supporting_observation_is_accepted():
    """Including the inference, which mentions Apple as the parent company.

    Concatenating the citations put "Apple Inc." into the text being checked, so
    citing all three of the observations that do support the brand was rejected
    while citing one of them passed. Citing more of the truth cannot be worse.
    """
    assert detect_uncited_value(
        "Brand", "Beats by Dr. Dre", (88, 91, 99), EVIDENCE
    ) is None
    assert detect_value_substitution(
        "Brand", "Beats by Dr. Dre", cited(88, 91, 99), BRAND_VALUES
    ) is None


# --- the miscitation is caught ------------------------------------------------


def test_a_related_entity_does_not_support_the_value():
    problem = detect_uncited_value("Brand", "Beats by Dr. Dre", (96,), EVIDENCE)
    assert problem is not None
    assert "not named by the cited evidence [96]" in problem


def test_the_rejection_names_the_evidence_that_would_have_supported_it():
    """Actionable or it is just a refusal: the operator needs the ids to cite."""
    problem = detect_uncited_value("Brand", "Beats by Dr. Dre", (96,), EVIDENCE)
    assert "88, 91, 99" in problem


def test_the_check_covers_a_free_text_aspect():
    """Model and MPN carry no allowed values, so the substitution check cannot see
    them at all. Before this, a free-text aspect had no citation check."""
    assert detect_value_substitution("Model", "Pill", cited(96), ()) is None
    problem = detect_uncited_value("Model", "Pill", (96,), EVIDENCE)
    assert problem is not None
    assert "88, 90" in problem


def test_a_value_the_cited_evidence_does_state_is_accepted():
    assert detect_uncited_value("Model", "A3211", (96,), EVIDENCE) is None


# --- what it deliberately does not do ------------------------------------------


def test_a_genuine_inference_that_nothing_names_is_left_alone():
    """The check fires on a citation error, not on inference. Nothing here reads
    "Wireless" anywhere, so no better citation was available and there is nothing
    to report."""
    assert detect_uncited_value("Connectivity", "Wireless", (90,), EVIDENCE) is None


def test_paraphrase_is_not_a_miscitation():
    evidence = {1: '{"claim": "The jacket is navy blue with a check pattern."}'}
    assert detect_uncited_value("Color", "Navy", (1,), evidence) is None


def test_evidence_we_cannot_read_fails_open():
    """A citation outside the readable set is already dropped as invented. Guessing
    about it here would turn a missing lookup into a rejection."""
    assert detect_uncited_value("Brand", "Beats by Dr. Dre", (4242,), EVIDENCE) is None


def test_no_citations_at_all_is_not_this_checks_business():
    assert detect_uncited_value("Brand", "Beats by Dr. Dre", (), EVIDENCE) is None


def test_the_check_declines_to_break_a_tie_between_competing_candidates():
    """The narrowing that two existing tests caught, pinned here directly.

    Read alone, this check would drop "Navy" -- a swing tag elsewhere in the set
    reads NAVY, so a better citation for that value existed. But the model cited a
    hedged observation twice on purpose, which is it declining to choose between
    two readings, and removing one leaves the other looking confident. `analyse`
    refuses to pick and asks for a photo, which is the right outcome; `map_aspects`
    therefore asks this only about a single-candidate aspect.
    """
    evidence = {
        1: '{"claim": "The fabric could read as either shade depending on light."}',
        2: '{"claim": "Swing tag colour reads NAVY MINI HT"}',
    }
    # Asked directly, it does fire -- which is why the caller has to scope it.
    assert detect_uncited_value("Color", "Navy", (1,), evidence) is not None
    # And "Blue", which nothing else names, is untouched either way.
    assert detect_uncited_value("Color", "Blue", (1,), evidence) is None


def test_the_fibre_substitution_case_still_reaches_the_substitution_check():
    """Elastodiene for Elastane: no other observation names elastodiene either, so
    the miscitation check declines and the swap check is what catches it. Ordering
    the two the other way round must not shadow this."""
    evidence = {7: '{"claim": "The swing tag reads 88% wool, 8% polyester, 4% elastane."}'}
    allowed = ("Wool", "Polyester", "Spandex", "Elastodiene")
    assert detect_uncited_value("Material", "Elastodiene", (7,), evidence) is None
    problem = detect_value_substitution(
        "Material", "Elastodiene", evidence[7], allowed
    )
    assert problem is not None
    assert "Spandex" in problem
