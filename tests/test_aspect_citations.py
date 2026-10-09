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


# --- a free-text aspect's values are suggestions, and the form must say so -----
#
# eBay marks every aspect SELECTION_ONLY or FREE_TEXT, and only the first is a
# closed set. Both used to print under `allowed:`.
#
# MP-000063 and MP-000065 are crocheted yarn hacky sacks. eBay's `Material` for
# their category is free text with five suggestions -- Carbon Fiber, Foam, Metal,
# Plastic, Wood -- and a ball of yarn is none of them. Told those were the allowed
# values, the mapper chose `Wood` three times across the two items, and MP-000065
# went to eBay with it. Replayed against the corrected form, six samples produced
# `Fabric`, `Cotton/Yarn (Crochet)` and similar, and `Wood` not once.


def _spec(name, mode, values=(), required=False):
    from resell.ebay.publisher import AspectSpec

    return AspectSpec(name, required, mode, "SINGLE", "STRING", None, tuple(values))


def test_free_text_values_are_offered_as_suggestions():
    from resell.reasoning.stages import render_aspect_form

    form = render_aspect_form([
        _spec("Material", "FREE_TEXT", ("Carbon Fiber", "Foam", "Wood")),
    ])
    assert "common values" in form
    assert "not a closed list" in form
    assert "allowed:" not in form, "the word that caused the problem"


def test_a_free_text_aspect_is_told_what_to_do_when_nothing_fits():
    """The instruction that replaces picking the nearest wrong answer."""
    from resell.reasoning.stages import render_aspect_form

    form = render_aspect_form([_spec("Material", "FREE_TEXT", ("Foam", "Wood"))])
    assert "write the observed value instead" in form
    assert "never pick the nearest one" in form


def test_a_selection_only_aspect_is_still_a_closed_set():
    """The other half must not soften: eBay refuses a value outside the list."""
    from resell.reasoning.stages import render_aspect_form

    form = render_aspect_form([
        _spec("Age Level", "SELECTION_ONLY", ("1-2 Years", "3-4 Years")),
    ])
    assert "allowed (choose one of these or leave it unset)" in form
    assert "common values" not in form
    assert "write the observed value" not in form


def test_an_aspect_with_no_values_is_plain_free_text():
    from resell.reasoning.stages import render_aspect_form

    form = render_aspect_form([_spec("Notes", "FREE_TEXT")])
    assert "free text -- write the observed value" in form


def test_the_truncation_notice_survives_on_both_kinds():
    """A missing value must still be visibly a truncation rather than an absence."""
    from resell.reasoning.stages import render_aspect_form

    many = tuple(f"v{i}" for i in range(80))
    for mode in ("FREE_TEXT", "SELECTION_ONLY"):
        form = render_aspect_form([_spec("Year", mode, many)], max_values=60)
        assert "and 20 more not shown" in form, mode


# --- a phrasing is not a substitution -----------------------------------------
#
# MP-000066 is a back cushion. Its observation reads "a contoured lumbar cushion
# for chair back support"; the mapping proposed Type = "Back Cushion" and cited
# it; and the substitution guard refused, because "Back Support" is also one of
# eBay's allowed values for that category and *is* in that sentence. Two
# near-synonyms from one list read as a swap, Type became unsupported, and the
# seller was asked to supply a value the system had already worked out.


def test_a_value_the_evidence_names_in_pieces_is_not_a_substitution():
    from resell.reasoning.gaps import detect_value_substitution

    allowed = ("Back Cushion", "Back Support", "Seat Cushion", "Lumbar Roll")
    assert detect_value_substitution(
        "Type", "Back Cushion",
        "The item is a contoured lumbar cushion for chair back support",
        allowed,
    ) is None


def test_a_genuine_swap_is_still_caught():
    """The case the guard exists for: elastodiene is a rubber-based fibre and
    elastane is polyurethane. Both legal values, real citation, different thing."""
    from resell.reasoning.gaps import detect_value_substitution

    allowed = ("Wool", "Polyester", "Spandex", "Elastodiene")
    complaint = detect_value_substitution(
        "Material", "Elastodiene",
        "The tag lists fabric content 'Plain 88% Wool, 8% Polyester, 4% Elastane'",
        allowed,
    )
    assert complaint and "Elastodiene" in complaint


def test_a_trailing_plus_is_part_of_the_word():
    """Without it "Beats Pill+" and "Beats Pill" tokenise the same, and they are
    different speakers. A model designation is exactly where this is load-bearing."""
    from resell.reasoning.gaps import _every_word_present

    assert not _every_word_present(
        "Beats Pill+", "The item is a Beats Pill portable Bluetooth speaker.")
    assert _every_word_present(
        "Beats Pill", "The item is a Beats Pill portable Bluetooth speaker.")


def test_the_guard_still_refuses_what_the_evidence_does_not_name():
    from resell.reasoning.gaps import detect_value_substitution

    for value, cited, allowed in (
        ("Gray", "the overall panel is black", ("Gray", "Black")),
        ("Steel", "a chrome/metal grip bar section", ("Steel", "Chrome")),
        ("40 lb", "the dial is marked 5 lb", ("40 lb", "5 lb")),
        ("Herringbone", "a subtle mini check pattern", ("Herringbone", "Check")),
    ):
        assert detect_value_substitution("A", value, cited, allowed), value


# --- an inference does not contradict a look ----------------------------------
#
# MP-000067 is a Canon EOS Rebel T6i. Two pieces of evidence read that designation
# off the body; a third *inferred* "This is a Canon EOS Rebel T6i (also marketed
# as EOS 750D)" -- one sentence saying the two names are the same camera. The
# mapping split it into two candidates with disjoint evidence, and the seller was
# asked to choose between two names for the object in their hand.


def _at(evidence_id, basis):
    from resell.reasoning.schema import EvidenceRef

    return EvidenceRef(evidence_id, basis)


def test_an_inference_does_not_contradict_an_observation():
    from resell.reasoning.gaps import Candidate, Resolution, resolve_aspect
    from resell.reasoning.schema import Basis

    outcome = resolve_aspect("Model", [
        Candidate("EOS Rebel T6i", (_at(2548, Basis.TEXT_READ),)),
        Candidate("EOS 750D", (_at(2572, Basis.INFERENCE),)),
    ])
    assert outcome.resolution is Resolution.RESOLVED
    assert outcome.value == "EOS Rebel T6i"
    assert "inferred" in outcome.explanation
    # The rejected reading is retained, not erased.
    assert any(c.value == "EOS 750D" for c in outcome.candidates)


def test_a_value_with_mixed_support_counts_as_observed():
    """MP-000053: 'Suit Jacket' rested on a visual observation and a spec tag,
    'Sport Coat' on an inference alone."""
    from resell.reasoning.gaps import Candidate, Resolution, resolve_aspect
    from resell.reasoning.schema import Basis

    outcome = resolve_aspect("Type", [
        Candidate("Suit Jacket", (_at(1, Basis.VISUAL_OBSERVATION),
                                  _at(2, Basis.TEXT_READ))),
        Candidate("Sport Coat", (_at(3, Basis.INFERENCE),)),
    ])
    assert outcome.resolution is Resolution.RESOLVED
    assert outcome.value == "Suit Jacket"


def test_two_observations_that_disagree_are_still_a_question():
    """MP-000052 is a sneaker with a white toe box, a black collar and a grey
    panel, every one a `visual_observation`. That is a real question about a
    genuinely multi-coloured shoe, and it must still be asked."""
    from resell.reasoning.gaps import Candidate, Resolution, resolve_aspect
    from resell.reasoning.schema import Basis

    outcome = resolve_aspect("Color", [
        Candidate("White", (_at(2153, Basis.VISUAL_OBSERVATION),)),
        Candidate("Black", (_at(2155, Basis.VISUAL_OBSERVATION),)),
        Candidate("Gray", (_at(2154, Basis.VISUAL_OBSERVATION),)),
    ])
    assert outcome.resolution is Resolution.CONTRADICTED


def test_text_read_does_not_outrank_visual_observation():
    """The principle `ADJUDICATING_BASES` was kept narrow to protect: no hidden
    ranking *between observations*. Only the operator adjudicates those."""
    from resell.reasoning.gaps import Candidate, Resolution, resolve_aspect
    from resell.reasoning.schema import Basis

    outcome = resolve_aspect("Size", [
        Candidate("42R", (_at(1, Basis.TEXT_READ),)),
        Candidate("38", (_at(2, Basis.VISUAL_OBSERVATION),)),
    ])
    assert outcome.resolution is Resolution.CONTRADICTED


def test_two_inferences_that_disagree_are_still_a_question():
    """The rule needs exactly one observed value to prefer. Two inferences against
    each other is not something a basis can settle."""
    from resell.reasoning.gaps import Candidate, Resolution, resolve_aspect
    from resell.reasoning.schema import Basis

    outcome = resolve_aspect("Model", [
        Candidate("A", (_at(1, Basis.INFERENCE),)),
        Candidate("B", (_at(2, Basis.INFERENCE),)),
    ])
    assert outcome.resolution is Resolution.CONTRADICTED
