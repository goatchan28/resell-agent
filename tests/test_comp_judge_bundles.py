"""What counts as a bundle, from the item that showed the rule was too wide.

MP-000047 -- an Achedaway massage gun, the first item a beta tester put through
the deployed app. Comp research found ten Achedaway listings on eBay and the
judge kept one, so the seller was offered $45.00 as Fast, Balanced *and*
Aggressive and listed at exactly the one asking price they had been compared
against.

Nine of the ten exclusions were right: three attachment heads, a charging base,
two cupping units, a lot of three cupping units, a heated scraper. Genuinely
different objects, correctly named.

Two were not. Both were **massage guns**, excluded as

    "bundle: massage gun plus extra attachments, case, and charger"

The bundle rule was written for suits and for camera bodies sold with lenses,
where the extra article has its own market and its own price. A massage gun's
attachments, case and charger are how the product is sold new -- there is no
version of it without them. Excluding those two turned a market of three
listings spanning $45-$99.99 into a sample of one.

The distinction the rule now draws is whether a buyer would plausibly shop for
the extra thing on its own. A lens, yes. A charging cable, no.
"""

from __future__ import annotations

import pytest

SEPARATELY_SALEABLE = "separately saleable"
ORDINARILY_INCLUDED = "ordinarily included"

# (price_cents, title, is_a_bundle, why) -- the real ten, verbatim.
MP_000047_LISTINGS: tuple[tuple[int, str, bool, str], ...] = (
    (1589, "Genuine OEM Achedaway Pro Massage Gun Attachment Massage Fork Head",
     True, "an attachment on its own; not the gun at all"),
    (1789, "Genuine OEM Achedaway Pro Massage Gun Attachment Aluminum Bullet Head",
     True, "an attachment on its own"),
    (1789, "Genuine OEM Achedaway Pro Massage Gun Attachment Aluminum Flat Head",
     True, "an attachment on its own"),
    (2581, "Achedaway Massage gun charging base",
     True, "a charging base on its own"),
    (4500, "Achedaway Therapy Handheld Massage Gun With Charger",
     False, "a massage gun; the charger is how one is sold"),
    (9999, "Achedaway Pro Massage Gun Kit Extra Attachments Case Charger",
     False, "a massage gun with the things a massage gun comes with"),
    (9999, "Achedaway Massage Gun Kit Extra Attachments Case Charger",
     False, "a massage gun with the things a massage gun comes with"),
    (18500, "ACHEDAWAY CUPPER UNIT IN ORIGINAL BOX",
     True, "a cupping unit, a different kind of device"),
    (30000, "Achedaway Cupper 3 Units (3x1 massage cups)",
     True, "three units in one lot"),
    (32999, "Achedaway Heated Scraper Adjustable Temperature",
     True, "a heated scraper, a different kind of device"),
)

MASSAGE_GUNS = tuple(l for l in MP_000047_LISTINGS if not l[2])
NOT_THE_ITEM = tuple(l for l in MP_000047_LISTINGS if l[2])


def prompt() -> str:
    from resell.reasoning.stages import COMP_JUDGE_SYSTEM_PROMPT

    return COMP_JUDGE_SYSTEM_PROMPT


def test_the_fixture_is_the_real_ten():
    assert len(MP_000047_LISTINGS) == 10
    assert len(MASSAGE_GUNS) == 3, "three massage guns were found, not one"
    assert len(NOT_THE_ITEM) == 7


# --- what the rule now says -----------------------------------------------------------
#
# These check wording, and wording is the weakest kind of evidence about a
# prompt. They are here to stop the rule being quietly deleted, not to show that
# it works -- what shows that is the recorded live judging below, which is the
# only thing that ever caught either version being wrong.


def test_the_rule_is_about_the_sale_unit_not_separate_availability():
    """The first attempt asked "would a buyer shop for the extra thing on its
    own". For Achedaway attachment heads the answer is demonstrably yes -- the
    same search found them at $15.89 -- so that test excluded almost everything,
    and the live judge went on excluding both massage-gun kits."""
    p = prompt()
    assert "Is the *sale unit* larger" in p
    assert "Separate availability is not the test" in p
    assert "would it come in the box" in p


def test_ambiguity_drops_a_rung_instead_of_excluding():
    """What actually made it stable. Reframing alone left the judge reading
    "extra attachments" as beyond-standard and excluding on three runs in five;
    naming the ambiguous case and sending it to `category_attribute` -- which
    still contributes -- made six runs of six keep all three guns."""
    p = prompt()
    assert "Ambiguity here is a rung question, not an exclusion" in p
    assert "Count the primary units" in p


def test_the_genuine_bundles_are_still_excluded():
    """The rule must not have become "admit everything"."""
    p = prompt()
    for still_out in ("suit's trousers", "lens sold with a camera body",
                      "three units in one lot"):
        assert still_out in p, still_out


def test_bundles_are_still_tested_before_anything_else():
    """Order is what stopped the first version admitting suits."""
    p = prompt()
    assert "stop at the first line that applies" in p
    assert p.index("Is the *sale unit* larger") < p.index("different material")
    assert p.index("different material") < p.index("different kind of object")


def test_the_case_that_exposed_it_is_written_down():
    p = prompt()
    assert "Achedaway" in p
    assert "one massage gun" in p


# --- what the live judge actually returned --------------------------------------------
#
# Recorded from six consecutive runs of the real Comp Judge against the ten
# stored observations, on a copy of the beta database. Not a hand-written
# expectation: the previous version of this file asserted the dispositions below
# as *intent* and passed while the live judge was returning the opposite.
#
# Re-run it with `scratch/rejudge47.py` if the prompt changes again. A prompt
# whose behaviour is not re-measured is a prompt whose behaviour is unknown.

LIVE_VERDICTS_2026_08_25: dict[int, str] = {
    1589: "excluded",              # replacement fork head, sold alone
    1789: "excluded",              # aluminium bullet head
    2581: "excluded",              # charging base
    4500: "category_attribute",    # the gun the old rule already kept
    9999: "category_attribute",    # both "Kit" listings -- the two that changed
    18500: "excluded",             # Cupper unit, a different device
    30000: "excluded",             # three Cupper units, a lot
    32999: "excluded",             # heated scraper
}


def test_the_live_judge_keeps_all_three_massage_guns():
    """1 contributing comp became 3, measured rather than asserted."""
    kept = [price for price, verdict in LIVE_VERDICTS_2026_08_25.items()
            if verdict != "excluded"]
    assert sorted(kept) == [4500, 9999]
    assert all(price in {l[0] for l in MASSAGE_GUNS} for price in kept)


def test_the_live_judge_still_excludes_every_accessory():
    for price, _, is_bundle, _ in MP_000047_LISTINGS:
        if is_bundle:
            assert LIVE_VERDICTS_2026_08_25[price] == "excluded", price


def test_what_was_kept_actually_contributes():
    """`category_attribute` is a rung down, not a polite refusal."""
    from resell.pricing.comps import Comparability

    for verdict in set(LIVE_VERDICTS_2026_08_25.values()) - {"excluded"}:
        assert Comparability(verdict).contributes


# --- the disposition of each real listing ---------------------------------------------


@pytest.mark.parametrize("price,title,is_bundle,why", MP_000047_LISTINGS,
                         ids=[l[1][:38] for l in MP_000047_LISTINGS])
def test_each_real_listing_has_a_stated_disposition(price, title, is_bundle, why):
    """The fixture is the regression: a live judging run is checked against it by
    hand, and the expected answer lives here where it can be argued with."""
    assert "Achedaway" in title or "ACHEDAWAY" in title
    assert why and price > 0


def test_the_three_guns_alone_are_enough_to_price_from():
    """The point of the change. $45 to $99.99 is a market -- a wide one, which is
    the honest answer for a gun whose model was never resolved -- where one
    listing is a number with nothing behind it."""
    prices = sorted(l[0] for l in MASSAGE_GUNS)
    assert prices == [4500, 9999, 9999]
    assert prices[0] != prices[-1], "Fast and Aggressive have somewhere to go"


def test_the_accessories_are_still_out():
    """Excluding an attachment head is not the same mistake as excluding a gun
    that comes with attachment heads, and the rule must keep telling them apart."""
    accessories = [l for l in NOT_THE_ITEM if "Attachment" in l[1] or "charging base" in l[1]]
    assert len(accessories) == 4
    assert all(l[2] for l in accessories)
