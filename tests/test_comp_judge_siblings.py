"""The Comp Judge and the sibling problem, from the case that exposed it.

MP-000036 was a Brooks Brothers Explorer Slim navy check wool jacket. Comp
research found 24 eBay listings and the judge excluded 23 of them, so pricing
fell through to "decide a price without comparables" on an item with a perfectly
ordinary second-hand market.

Every exclusion was a sub-line distinction -- Regent instead of Slim, 1818
Madison instead of Explorer, 346 instead of Explorer. Those are different names
for the same object: a Brooks Brothers wool jacket. Second-hand they do not sell
for wildly different money, and one of them tells you something real about what
this one fetches.

Two entries were corrected against a live judging run rather than the other way
round: the 1818 Madison jackets are wool-cashmere, which is a dearer cloth than
this item's wool blend, and the judge was right to call that a different market.
A fixture that argues with a defensible judgement is a fixture that will be
edited until it stops failing.

The fixture below is those 24 listings, verbatim, with the disposition the rule
should produce. `SIBLING` is a step down the ladder to `category_attribute`,
which still contributes. `DIFFERENT_OBJECT` is a genuine exclusion: another
material, a jacket-and-trousers bundle, an accessory, a part, a lot.
"""

from __future__ import annotations

import pathlib

import pytest

from resell.pricing.comps import Comparability

SIBLING = "sibling"
DIFFERENT_OBJECT = "different object"

# (price_cents, title, disposition, why)
MP_000036_COMPS: tuple[tuple[int, str, str, str], ...] = (
    (7500, "BROOKS BROTHERS Explorer Classic Wool Blend Blazer Jacket Gold Buttons 44 Short",
     SIBLING, "Explorer Classic instead of Explorer Slim: a fit line"),
    (5249, "Brooks Brothers Men's Explorer Regent Fit 2-Button Blazer Navy Blue 48R",
     SIBLING, "Regent instead of Slim, and navy like this one"),
    (10999, "Brooks Brothers Explorer Regent Fit Suit Men's 42R Blue Wool Blend",
     DIFFERENT_OBJECT, "a suit, so trousers are in the price"),
    (10000, "Brooks Brothers Regent 42L Gray Wool Sport Coat 2-Button Notch Lapel",
     SIBLING, "same brand, wool, single jacket"),
    (7900, "Brooks Brothers Men's 2-Piece Suit Explorer Regent Fit 42 Short, Pants 36x30 B",
     DIFFERENT_OBJECT, "two-piece bundle"),
    (7900, "Brooks Brothers 1818 Regent Men's Linen Blazer 40R",
     DIFFERENT_OBJECT, "linen, not wool"),
    (5999, "Brooks Brothers 1818 Madison Sport Coat 40S E. Thomas Wool Cashmere Plaid Blazer",
     DIFFERENT_OBJECT, "wool cashmere is a dearer cloth than a wool blend"),
    (5299, "New Brooks Brothers Blazer 346 Sport Coat Charcoal Gray Harringbone Mens 42S",
     SIBLING, "346 is a sub-line, not a different garment"),
    (14999, "NWT Brooks Brothers Explorer Suit Jacket Fitzgerald Fit Navy Blue Mens 46 Long",
     SIBLING, "same Explorer line, different fit -- already graded correctly"),
    (8999, "Brooks Brothers Mens 40S Suit Jacket Gray Explorer Regent Fit Wool Pants 35x28",
     DIFFERENT_OBJECT, "jacket and trousers"),
    (2650, "Brooks Brothers Dark Brown Corduroy Blazer 42R Regular Fit 2-Button Notch Lined",
     DIFFERENT_OBJECT, "corduroy"),
    (13300, "Rare Vintage Brooks Brothers Men's Double Breasted Navy Blazer Sz 44 LN-USA Made",
     SIBLING, "double-breasted is a cut, and it is still a navy wool blazer"),
    (12000, "Brooks Brothers Sport Coat 38S Mens Navy Wool Stretch Blazer",
     SIBLING, "navy wool jacket, unnamed line"),
    (6500, "Brooks Brothers 1818 Madison Blazer Mens 44R Brown Houndstooth Wool Cashmere",
     DIFFERENT_OBJECT, "wool cashmere, same reason"),
    (11999, "Brooks Brothers Mens 40S 2-Piece Suit Explorer Regent Fit Navy Wool Blazer 35x27",
     DIFFERENT_OBJECT, "two-piece bundle"),
    (14999, "Brooks Brothers Explorer Regent Suit Charcoal Gray Mens 40R 34 x 30 Blazer Pant",
     DIFFERENT_OBJECT, "suit with trousers"),
    (16400, "NWT B by BROOKS BROTHERS Seersucker Blazer Sport Coat Blue White Stripe Size 38S",
     DIFFERENT_OBJECT, "seersucker"),
    (4299, "Brooks Brothers Explorer Regent Fit Blazer Mens 40R Blue Wool Blend Windowpane",
     SIBLING, "the clearest case: same Explorer family, wool blend, blue"),
    (6999, "Brooks Brothers Suit Jacket Mens 42R Blue Plaid Explorer Regent Fit Stretch Wool",
     SIBLING, "Explorer Regent, stretch wool, single jacket"),
    (8500, "Brooks Brothers Explorer Regent Fit Sport Coat 40R Linen Quiet Luxury Hamptons",
     DIFFERENT_OBJECT, "linen"),
    (7000, "VTG Brooks Brothers Tweed Blazer Wool Houndstooth Prince of Wales 41R Grey USA",
     DIFFERENT_OBJECT, "tweed is a different cloth at a different price"),
    (2700, "Brooks Brothers Blazer Men's 42R Explorer Regent Wool Window Pane Sport Coat",
     SIBLING, "Explorer Regent, wool"),
    (12999, "Brooks Brothers Harris Tweed Sport Coat 42R Made In USA 100% Wool Herringbone",
     DIFFERENT_OBJECT, "Harris Tweed"),
    (7500, "Brooks Brothers Brooksease navy blue Loro Piana wool blazer, gold buttons, 37R",
     SIBLING, "Brooksease is a sub-line; navy wool blazer"),
)

SIBLINGS = tuple(c for c in MP_000036_COMPS if c[2] == SIBLING)
DIFFERENT = tuple(c for c in MP_000036_COMPS if c[2] == DIFFERENT_OBJECT)


def test_the_fixture_is_the_real_twenty_four():
    assert len(MP_000036_COMPS) == 24
    assert len(SIBLINGS) == 11
    assert len(DIFFERENT) == 13


# --- what the judge is now told -------------------------------------------------------


def prompt() -> str:
    from resell.reasoning.stages import COMP_JUDGE_SYSTEM_PROMPT

    return COMP_JUDGE_SYSTEM_PROMPT


def test_a_sub_line_is_a_step_down_not_an_exclusion():
    assert "is a step down to `category_attribute` -- not an exclusion" in prompt()
    for kind in ("fit", "cut", "sub-line", "sub-brand", "diffusion line"):
        assert kind in prompt(), kind


def test_the_sibling_case_is_described_where_the_rung_is_defined():
    """A reader choosing between rungs is reading the rung list, so the rule has
    to be visible from there and not only in a paragraph below it."""
    ladder = prompt()[prompt().index("`category_attribute` -- the same kind of thing"):]
    assert "sibling" in ladder[:400]
    for word in ("fit", "sub-line", "material"):
        assert word in ladder[:400], word


def test_the_test_is_ordered_and_bundles_come_first():
    """Order is the whole fix. A first pass that only added the sibling rule
    started admitting jacket-and-trousers bundles, which drag the band upward --
    so "how many articles are in this price" is asked before anything about the
    brand."""
    p = prompt()
    assert "stop at the first line that applies" in p
    assert p.index("More than one article in the price") < p.index("different material")
    assert p.index("different material") < p.index("step down")
    for kind in ("accessory", "part", "lot"):
        assert kind in p, kind


def test_an_exclusion_is_told_to_carry_its_reason():
    """13 of 24 judgements were discarded on the way in for want of one, which
    leaves a listing neither counted nor accounted for."""
    p = prompt()
    assert "naming the extra item" in p
    assert "naming the cloth" in p
    assert "`excluded_reason`, every time" in p
    from resell.reasoning.tools import COMP_JUDGE_TOOL_SCHEMA

    described = COMP_JUDGE_TOOL_SCHEMA["input_schema"]["properties"]["judgements"][
        "items"]["properties"]["excluded_reason"]["description"]
    assert "discarded without it" in described


def test_the_prompt_names_the_case_that_exposed_this():
    """The Brooks Brothers example, kept concrete. "Do not be too strict" is
    advice; a worked example is an instruction."""
    assert "Regent" in prompt() and "Explorer Slim" in prompt()


def test_the_judge_is_warned_in_both_directions():
    p = prompt()
    assert "Keeping one listing out of twenty-four" in p
    assert "a wider band, not an empty one" in p
    assert "Keeping almost everything usually means a bundle or a different cloth got in" in p


def test_the_tool_schema_agrees_with_the_prompt():
    """The enum description is what the model reads at the moment it chooses, so
    it cannot say something narrower than the prompt."""
    from resell.reasoning.tools import COMP_JUDGE_TOOL_SCHEMA

    described = COMP_JUDGE_TOOL_SCHEMA["input_schema"]["properties"]["judgements"][
        "items"]["properties"]["comparability"]["description"]
    assert "sibling" in described
    assert "different object" in described


# --- stepping down has to actually keep the comp --------------------------------------


def test_the_rung_it_steps_down_to_still_counts():
    """If `category_attribute` did not contribute, the whole instruction would be
    a politer way of throwing the listing away."""
    assert Comparability.CATEGORY_ATTRIBUTE.contributes
    assert not Comparability.SUPERFICIAL.contributes
    assert not Comparability.EXCLUDED.contributes


def test_the_siblings_alone_are_enough_to_price_from():
    """The point of the change. Eleven asking prices spanning $27 to $150 is a
    market -- a wide one, which is the honest answer for a garment whose sub-line
    the buyer cannot see -- where one listing is nothing at all."""
    prices = sorted(c[0] for c in SIBLINGS)
    assert len(prices) >= 8, "enough to form a band"
    assert prices[0] == 2700 and prices[-1] == 14999


def test_every_genuine_exclusion_names_its_reason_in_the_fixture():
    """So that a later loosening of the rule cannot quietly reclassify a bundle
    as a sibling without someone editing this list and noticing."""
    for _, title, _, why in DIFFERENT:
        assert why, title
        assert any(word in why.lower() for word in
                   ("linen", "corduroy", "seersucker", "tweed", "suit", "bundle",
                    "trousers", "cloth", "cashmere")), (title, why)


@pytest.mark.parametrize("price,title,disposition,why", MP_000036_COMPS,
                         ids=[c[1][:40] for c in MP_000036_COMPS])
def test_each_real_listing_has_a_stated_disposition(price, title, disposition, why):
    """The fixture is the regression. A live judging run is checked against it by
    hand; this keeps the expected answer written down and reviewable rather than
    living in a transcript."""
    assert disposition in (SIBLING, DIFFERENT_OBJECT)
    assert price > 0
    assert "Brooks Brothers" in title or "BROOKS BROTHERS" in title


def test_the_case_is_recorded_where_someone_will_find_it():
    """A test file that does not say which item it came from is a list of
    opinions about blazers."""
    source = pathlib.Path(__file__).read_text()
    assert "MP-000036" in source
    assert "excluded 23" in source
