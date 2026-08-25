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


def test_a_bundle_is_about_separately_saleable_products():
    """Not "more than one article". A charger is an article and is not a product
    anybody shops for when they want a massage gun."""
    p = prompt()
    assert SEPARATELY_SALEABLE in p
    assert "naming the extra product" in p


def test_the_ordinary_contents_of_a_box_are_named():
    """Left implicit, this reads as a licence to admit anything. Named, it is a
    rule about what comes in the box."""
    p = prompt()
    for included in ("charger", "cable", "case", "attachment set", "manual"):
        assert included in p, included


def test_the_test_a_judge_can_actually_apply():
    """"Would a buyer plausibly shop for the extra thing on its own" decides the
    camera lens and the charging cable in opposite directions, which is the whole
    difficulty."""
    p = prompt()
    assert "shop for the extra thing on its own" in p
    assert "a camera lens yes, a charging cable no" in p


def test_the_genuine_bundles_are_still_excluded():
    """The rule must not have become "admit everything". A suit, a body with a
    lens and a multi-unit lot are all still named."""
    p = prompt()
    for still_out in ("A suit is a jacket", "body sold with a lens",
                      "three units in one lot"):
        assert still_out in p, still_out


def test_the_case_that_exposed_it_is_written_down():
    p = prompt()
    assert "Achedaway" in p
    assert "one massage gun" in p


def test_bundles_are_still_tested_before_anything_else():
    """Order is what stopped the first version of this rule admitting suits. It
    has to survive the narrowing."""
    p = prompt()
    assert "stop at the first line that applies" in p
    assert p.index(SEPARATELY_SALEABLE) < p.index("different material")
    assert p.index("different material") < p.index("different kind of object")


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
