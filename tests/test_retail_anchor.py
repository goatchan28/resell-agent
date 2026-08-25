"""A shop price, turned into a resale anchor -- and kept out of the sample.

MP-000041 is the case. An XD Design Bobby Hero backpack, new-other, priced at
**$75** on the strength of one eBay asking price for a Bobby *Original* -- a
cheaper sub-line, a different colour, condition unknown, never sold. The item's
own record already held the shop price for this exact model: $139.00, on disk,
cited, and unable to touch the price, because a retail figure is barred from
every distribution in `estimate` and there was nothing else it was allowed to
be.

The bar stays. A shop price is not a comparable sale and pooling the two would
corrupt the sample. What is added is the other thing a person does with a shop
price: reason down from it. `retention.py` states, per eBay top-level category
and per condition, how much of a shop price a resale keeps; the estimator weighs
the resulting anchor *alongside* the marketplace sample according to how much
that sample deserves.

Three properties are worth protecting, and they are separable:

  the anchor is an inference, presented as a band and labelled as one;
  it never becomes a comp, and original retail never becomes an anchor;
  and it speaks only in proportion to the silence of the market.
"""

from __future__ import annotations

import pytest

from resell.pricing.comps import (
    Comparability,
    ConditionBand,
    PriceKind,
    RetailKind,
)
from resell.pricing.estimate import (
    BLEND_BELOW,
    ENOUGH_COMPS,
    PriceQualifier,
    RetailReference,
    recommend,
    retail_ceiling_check,
)
from resell.pricing.retention import (
    ANCHOR_SPREAD,
    BASE_RETENTION,
    CONDITION_FACTOR,
    DEFAULT_RETENTION,
    UNKNOWN_CONDITION_FACTOR,
    anchor_from_retail,
    retention_for,
    top_level,
)

from tests.test_pricing_estimate import comp, inp

# MP-000041, verbatim.
BOBBY_CATEGORY = (
    "Clothing, Shoes & Accessories > Backpacks, Bags & Briefcases > Backpacks"
)
BOBBY_RETAIL_CENTS = 13900
BOBBY_CONDITION = ConditionBand.NEW_OTHER


def shop_price(cents=BOBBY_RETAIL_CENTS, kind=RetailKind.CURRENT):
    return (RetailReference(
        price_cents=cents, kind=kind, source="xd-designusa.com", citation="ev_r",
    ),)


def priced(comps, *, retail=None, category=BOBBY_CATEGORY,
           condition=BOBBY_CONDITION, **kw):
    return recommend(inp(
        comps=comps,
        retail=shop_price() if retail is None else retail,
        category_path=category, item_condition_band=condition, **kw,
    ))


# --- the table ------------------------------------------------------------------------


def test_the_key_is_ebays_own_top_level_grouping():
    """Which is the whole reason the category path is captured at identification.
    A leaf nobody has seen before still lands under a group that has a rate."""
    assert top_level(BOBBY_CATEGORY) == "Clothing, Shoes & Accessories"
    assert top_level("Cameras & Photo > Lenses & Filters > Lenses") == "Cameras & Photo"
    assert top_level(None) == "" and top_level("") == ""


def test_an_unknown_category_gets_the_default_and_says_so():
    """Silently applying a made-up rate is the failure. The anchor carries the
    admission in its own words."""
    a = anchor_from_retail(10000, "Nonexistent Department", ConditionBand.NEW_WITH_TAGS)
    assert a.retention == pytest.approx(DEFAULT_RETENTION)
    assert a.is_default_category
    assert "default rate" in a.basis


def test_things_that_hold_value_are_rated_above_things_that_do_not():
    """The table's only real claim. A phone is superseded on a known cadence and
    a wool coat is not, so they cannot share a number."""
    assert BASE_RETENTION["Jewelry & Watches"] > BASE_RETENTION["Consumer Electronics"]
    assert BASE_RETENTION["Clothing, Shoes & Accessories"] > BASE_RETENTION["Furniture"]
    assert all(0.3 <= r <= 0.85 for r in BASE_RETENTION.values())


def test_condition_scales_the_rate_and_never_inverts_the_ladder():
    """`ConditionBand` is already an ordered ladder. Money has to run the same
    way, or a for-parts item outranks a mint one somewhere in the table."""
    # `ordinal` runs 1 (for parts) to 8 (new with tags), so best-first is
    # descending -- and the money has to fall the same way with no crossings.
    best_first = sorted(CONDITION_FACTOR, key=lambda b: b.ordinal, reverse=True)
    factors = [CONDITION_FACTOR[b] for b in best_first]
    assert factors == sorted(factors, reverse=True)
    assert factors[0] == 1.00 and best_first[0] is ConditionBand.NEW_WITH_TAGS
    assert best_first[-1] is ConditionBand.FOR_PARTS


def test_an_ungraded_item_is_anchored_low_not_in_the_middle():
    """Assuming "probably fine" of a thing nobody has graded is how a listing is
    priced as new and arrives used. Unknown sits down near used, not at the
    average of the ladder."""
    assert UNKNOWN_CONDITION_FACTOR < CONDITION_FACTOR[ConditionBand.USED_EXCELLENT]
    assert UNKNOWN_CONDITION_FACTOR > CONDITION_FACTOR[ConditionBand.USED_FAIR]
    unknown = retention_for(BOBBY_CATEGORY, ConditionBand.UNKNOWN)
    assert unknown < retention_for(BOBBY_CATEGORY, ConditionBand.NEW_OTHER)


def test_the_anchor_is_a_band_because_it_is_inferred():
    """A number derived from a rate should not present as a measurement."""
    a = anchor_from_retail(10000, BOBBY_CATEGORY, ConditionBand.NEW_WITH_TAGS)
    assert a.low_cents < a.point_cents < a.high_cents
    assert a.point_cents == 6500  # 0.65 of the shop price, mint
    assert a.low_cents == round(a.point_cents * (1 - ANCHOR_SPREAD))
    assert a.high_cents == round(a.point_cents * (1 + ANCHOR_SPREAD))


def test_the_anchor_states_its_own_arithmetic():
    """So that a price nobody expected can be argued with rather than guessed at."""
    a = anchor_from_retail(BOBBY_RETAIL_CENTS, BOBBY_CATEGORY, BOBBY_CONDITION)
    assert a.basis == (
        "59% of the $139.00 shop price for Clothing, Shoes & Accessories, new other"
    )
    assert a.retail_cents == BOBBY_RETAIL_CENTS


# --- the bar that stays ---------------------------------------------------------------


def test_original_retail_is_still_not_a_price():
    """A manufacturer's list price is a marketing number and may be years stale.
    Only what the thing can be bought for *today* is worth reasoning down from."""
    r = priced([], retail=shop_price(kind=RetailKind.ORIGINAL))
    assert r.unpriceable
    assert PriceQualifier.RETAIL_ONLY in r.qualifiers
    assert PriceQualifier.RETAIL_ANCHORED not in r.qualifiers


def test_the_anchor_never_becomes_a_comp():
    """The distribution boundary is the thing being protected. An anchored price
    is built from nothing in the basis, and the shop price appears in the account
    under the role it actually played."""
    from resell.pricing.estimate import EvidenceRole

    r = priced([])
    assert r.n_in_basis == 0
    assert r.retail_anchor is not None, "kept beside the basis, not inside it"
    assert not any(c.contributed for c in r.contributions), "nothing set the band"
    retail_lines = [c for c in r.contributions if c.role is EvidenceRole.CEILING_CHECK]
    assert len(retail_lines) == 1 and retail_lines[0].low_cents == BOBBY_RETAIL_CENTS


def test_a_shop_price_of_zero_anchors_nothing():
    r = priced([], retail=shop_price(cents=0))
    assert r.unpriceable
    assert r.retail_anchor is None


def test_the_lowest_current_price_is_the_one_reasoned_down_from():
    """Two shops, two prices. What it can actually be bought for new today is the
    honest ceiling; taking the dearer one would inflate every resale under it."""
    r = priced([], retail=shop_price() + shop_price(cents=19900))
    assert r.retail_anchor.retail_cents == BOBBY_RETAIL_CENTS


# --- no market at all -----------------------------------------------------------------


def test_a_shop_price_and_no_comps_is_a_price_now():
    """It used to be UNPRICEABLE, which sent the seller to type a number of their
    own with strictly less information than the agent already had."""
    r = priced([])
    assert not r.unpriceable
    assert PriceQualifier.RETAIL_ANCHORED in r.qualifiers
    assert r.anchor_weight == 1.0
    assert (r.band_low_cents, r.band_central_cents, r.band_high_cents) == (
        7156, 8132, 9108,
    )
    assert "no marketplace comps" in r.reason


def test_no_comps_and_no_retail_is_still_the_operators_question():
    """The one state where "set a price yourself" is the honest answer: research
    ran, and there is nothing on record to reason from in either direction."""
    r = priced([], retail=())
    assert r.unpriceable
    assert r.retail_anchor is None
    assert "ask the operator" in r.reason


# --- how loudly the anchor is allowed to speak ----------------------------------------


def test_the_mp_000041_case_end_to_end():
    """One category-attribute ask at $75 for a cheaper sub-line, against a $139
    shop price for this exact model. Before, the ask was the whole answer."""
    ask = comp(7500, kind=PriceKind.ASKING, band=ConditionBand.USED_GOOD,
               comparability=Comparability.CATEGORY_ATTRIBUTE, cid="bobby_original")
    r = priced([ask])
    assert PriceQualifier.ANCHOR_BLENDED in r.qualifiers
    assert r.anchor_weight == pytest.approx(1 - 0.5 * (1 / ENOUGH_COMPS), abs=1e-3)
    assert (r.band_low_cents, r.band_central_cents, r.band_high_cents) == (
        7156, 8027, 9108,
    )
    assert r.band_low_cents <= 7500, "the observed ask is still inside the band"
    assert r.band_high_cents >= r.retail_anchor.high_cents


def test_one_listing_for_the_identical_product_is_not_diluted():
    """A fact about this product's market outranks an inference from a shop
    price, however lonely the fact is. Two same-product asks above the shop price
    mean the thing sells above the shop price, and averaging that toward a
    depreciation rate would erase a real finding."""
    same = comp(7500, kind=PriceKind.ASKING, band=BOBBY_CONDITION,
                comparability=Comparability.SAME_PRODUCT, cid="identical")
    r = priced([same])
    assert PriceQualifier.ANCHOR_BLENDED not in r.qualifiers
    assert r.anchor_weight == 0.0
    assert r.band_central_cents == 7500
    assert r.retail_anchor is not None, "still recorded, just not applied"


def test_enough_of_a_market_and_the_anchor_goes_quiet():
    """At `ENOUGH_COMPS` on the same rung the market is talking. The anchor stays
    on the record as context and stops touching the band."""
    market = [comp(7000 + 500 * i, kind=PriceKind.ASKING, band=BOBBY_CONDITION,
                   comparability=Comparability.CATEGORY_ATTRIBUTE, cid=f"m{i}")
              for i in range(ENOUGH_COMPS)]
    r = priced(market)
    assert PriceQualifier.ANCHOR_BLENDED not in r.qualifiers
    assert r.band_central_cents == 7500
    assert (r.band_low_cents, r.band_high_cents) == (7000, 8000)


def test_thin_evidence_widens_the_band_rather_than_narrowing_it():
    """Two weak comps and a shop price disagree. The honest output spans both --
    a wider band, not a confident wrong one."""
    market = [comp(7000 + 500 * i, kind=PriceKind.ASKING, band=BOBBY_CONDITION,
                   comparability=Comparability.CATEGORY_ATTRIBUTE, cid=f"t{i}")
              for i in range(2)]
    thin = priced(market)
    full = priced(market + [comp(8000, kind=PriceKind.ASKING, band=BOBBY_CONDITION,
                                 comparability=Comparability.CATEGORY_ATTRIBUTE,
                                 cid="t2")])
    assert PriceQualifier.ANCHOR_BLENDED in thin.qualifiers
    spread = thin.band_high_cents - thin.band_low_cents
    assert spread > full.band_high_cents - full.band_low_cents
    assert thin.band_low_cents <= 7000 and thin.band_high_cents >= 9108


def test_the_blend_moves_the_centre_in_proportion_and_not_further():
    """`anchor_weight` is the whole disclosure. Whatever share it names is the
    share the anchor actually took, or the number is decoration."""
    ask = comp(7500, kind=PriceKind.ASKING, band=BOBBY_CONDITION,
               comparability=Comparability.CATEGORY_ATTRIBUTE, cid="lonely")
    r = priced([ask])
    market_share = 1 - r.anchor_weight
    assert r.band_central_cents == round(
        7500 * market_share + r.retail_anchor.point_cents * r.anchor_weight
    )
    assert 0 < r.anchor_weight < 1


def test_the_threshold_is_a_named_constant_both_sides_agree_on():
    """`BLEND_BELOW` is exclusive: a sample worth exactly half an answer is
    already the market's answer."""
    assert BLEND_BELOW == 0.5
    exactly_half = [comp(7500, kind=PriceKind.ASKING, band=BOBBY_CONDITION,
                         comparability=Comparability.CATEGORY_ATTRIBUTE, cid=f"h{i}")
                    for i in range(ENOUGH_COMPS)]
    assert PriceQualifier.ANCHOR_BLENDED not in priced(exactly_half).qualifiers
    assert PriceQualifier.ANCHOR_BLENDED in priced(exactly_half[:2]).qualifiers


# --- a shop price for a different product ---------------------------------------------
#
# MP-000022, a Bowflex SelectTech 552 set. Comp research reached bowflex.com and
# recorded nine current shop prices; the judge graded six of them `excluded`.
# `_current_retail` took the lowest of all nine, so a $399 pair of dumbbells
# anchored on a **$29.99 JRNY Tablet Holder** and priced at $9.82-$12.50.
#
# Nothing had gone wrong upstream. The judge did the work of noticing the page
# was a different product; the retail harvest simply never read its answer.
#
# (price_cents, title, comparability) -- the nine rows verbatim, with the grades
# the judge actually gave them. Note what is *not* here: no `same_product`. A
# comp cannot claim it while the identity is unresolved, which is why
# `ANCHORABLE_RETAIL` has to include the variant rung -- restricted to exact
# matches this feature would almost never fire on a real item.
MP_000022_RETAIL: tuple[tuple[int, str, Comparability], ...] = (
    (2999, "JRNY Tablet Holder", Comparability.EXCLUDED),
    (17900, "SelectTech Dumbbell Stand with Media Rack", Comparability.EXCLUDED),
    (34900, "5.1S Bench", Comparability.EXCLUDED),
    (69900, "Results Series 1090 SelectTech Dumbbells", Comparability.EXCLUDED),
    (69900, "Results Series 90 SelectTech Dumbbells", Comparability.EXCLUDED),
    (19900, "SelectTech Adjustable Dumbbell Stand", Comparability.SAME_FAMILY_VARIANT),
    (39900, "Results Series 552 SelectTech Dumbbells", Comparability.SAME_FAMILY_VARIANT),
    (39900, "Results Series 552 SelectTech Dumbbells", Comparability.SAME_FAMILY_VARIANT),
    (39900, "Results Series 52 SelectTech Dumbbells", Comparability.SAME_FAMILY_VARIANT),
)


def bowflex(*, only=None) -> tuple[RetailReference, ...]:
    rows = MP_000022_RETAIL if only is None else [
        r for r in MP_000022_RETAIL if r[2] in only
    ]
    return tuple(
        RetailReference(price_cents=cents, kind=RetailKind.CURRENT,
                        source="bowflex.com", citation=title, match=match)
        for cents, title, match in rows
    )


def test_the_judges_verdict_reaches_the_retail_harvest():
    """The whole fix. A shop price arrives on a comp observation the judge has
    already graded, and that grade is what says whether it is this item's price
    tag or the next item's along."""
    from resell.pricing.estimate import _current_retail

    chosen = _current_retail(bowflex())
    assert chosen.price_cents == 39900
    assert chosen.citation == "Results Series 552 SelectTech Dumbbells"


def test_the_middle_of_the_tier_not_the_cheapest_thing_in_it():
    """`min` looks conservative and is a systematic bias: the cheapest member of
    a product family anchors every item in it. Bowflex's four family-variant
    prices are a $199 stand and three $399 dumbbell sets, and the item is the
    dumbbells."""
    from resell.pricing.estimate import _current_retail

    variants = bowflex(only={Comparability.SAME_FAMILY_VARIANT})
    assert sorted(r.price_cents for r in variants) == [19900, 39900, 39900, 39900]
    assert _current_retail(variants).price_cents == 39900

    # `category_path=None` because that is MP-000022's record -- it predates the
    # column -- so these are the figures `resell price recommend` prints for it.
    r = recommend(inp(comps=[], retail=bowflex(), category_path=None,
                      item_condition_band=ConditionBand.USED_GOOD))
    assert r.retail_anchor.retail_cents == 39900
    assert (r.band_low_cents, r.band_central_cents, r.band_high_cents) == (
        13062, 14843, 16624,
    ), "a used pair of SelectTech 552s, not a tablet holder"


def test_an_excluded_page_can_never_price_the_item():
    excluded = bowflex(only={Comparability.EXCLUDED})
    assert not any(r.prices_this_item for r in excluded)
    r = recommend(inp(comps=[], retail=excluded, category_path=BOBBY_CATEGORY,
                      item_condition_band=ConditionBand.USED_GOOD))
    assert r.unpriceable
    assert r.retail_anchor is None
    assert "different product" in r.reason


def test_merely_the_same_kind_of_thing_is_not_a_price_tag():
    """Tighter than `Comparability.contributes` on purpose. A comp one rung down
    still says something about *this market*; a shop price one rung down is
    another product's sticker."""
    from resell.pricing.estimate import ANCHORABLE_RETAIL

    assert Comparability.CATEGORY_ATTRIBUTE.contributes
    assert Comparability.CATEGORY_ATTRIBUTE not in ANCHORABLE_RETAIL
    some_other_backpack = (RetailReference(
        price_cents=4900, kind=RetailKind.CURRENT,
        match=Comparability.CATEGORY_ATTRIBUTE),)
    assert recommend(inp(comps=[], retail=some_other_backpack,
                         category_path=BOBBY_CATEGORY)).retail_anchor is None


def test_what_the_operator_typed_needs_no_grade():
    """There is no page and no judge: the assertion is already about the thing in
    hand. `match=None` is that case, and it is trusted."""
    typed = (RetailReference(price_cents=13900, kind=RetailKind.CURRENT),)
    assert typed[0].match is None and typed[0].prices_this_item
    assert recommend(inp(comps=[], retail=typed, category_path=BOBBY_CATEGORY,
                         item_condition_band=BOBBY_CONDITION)).retail_anchor


def test_an_exact_match_outranks_a_variant():
    """Constructed, not from MP-000022: nothing there could be graded
    `same_product`. The property still has to hold -- once the identity *is*
    resolved, this item's own shop price must not be averaged in with the rest of
    the product line."""
    from resell.pricing.estimate import _current_retail

    line = bowflex(only={Comparability.SAME_FAMILY_VARIANT}) + (
        RetailReference(price_cents=44900, kind=RetailKind.CURRENT,
                        citation="this exact set", match=Comparability.SAME_PRODUCT),
    )
    chosen = _current_retail(line)
    assert chosen.price_cents == 44900
    assert chosen.citation == "this exact set"


def test_the_ceiling_check_honours_the_same_gate():
    """Otherwise a correctly-priced $399 item is flagged for exceeding the
    "retail" of a $29.99 accessory found on the same site."""
    ok, why = retail_ceiling_check(30000, bowflex())
    assert ok, why
    assert "399.00" in why
    refused, _ = retail_ceiling_check(45000, bowflex())
    assert not refused, "still a real ceiling, just the right one"
    # The tablet holder is the reason: under the old rule a $300 ask on a $399
    # item was refused for exceeding "retail" of $29.99.
    assert retail_ceiling_check(30000, bowflex(only={Comparability.EXCLUDED}))[0]


def test_a_shop_price_that_did_nothing_is_reported_as_such():
    """"9 retail references" over a band built from one of them is the line that
    hid the tablet holder."""
    from resell.pricing.estimate import EvidenceRole

    r = recommend(inp(comps=[], retail=bowflex(), category_path=BOBBY_CATEGORY,
                      item_condition_band=ConditionBand.USED_GOOD))
    retail_lines = {c.role: c for c in r.contributions if c.source == "retail context"}
    assert retail_lines[EvidenceRole.CEILING_CHECK].n == 4
    assert retail_lines[EvidenceRole.EXCLUDED].n == 5
    assert "different product" in retail_lines[EvidenceRole.EXCLUDED].detail


# --- the hierarchy, rung by rung ------------------------------------------------------


def strategies_for(comps, *, retail=None, condition=BOBBY_CONDITION):
    from resell.pricing.strategy import build_strategies

    return build_strategies(priced(comps, retail=retail, condition=condition))


def market(n, *, comparability=Comparability.SAME_PRODUCT):
    return [comp(7000 + 500 * i, kind=PriceKind.ASKING, band=BOBBY_CONDITION,
                 comparability=comparability, cid=f"mk{i}") for i in range(n)]


def test_rung_one_marketplace_evidence_alone():
    from resell.pricing.strategy import AnchorSource

    built = strategies_for(market(4))
    assert built is not None
    for price in built.prices.values():
        assert price.anchor.source is AnchorSource.MARKETPLACE
        assert price.anchor.is_observed
        assert price.anchor.n == 4


def test_rung_two_sparse_marketplace_plus_anchor_is_still_marketplace():
    """The anchor moved the band inside `estimate`. The strategies still take
    positions in an observed sample, and say so."""
    from resell.pricing.estimate import PriceQualifier
    from resell.pricing.strategy import AnchorSource

    rec = priced(market(2, comparability=Comparability.CATEGORY_ATTRIBUTE))
    assert PriceQualifier.ANCHOR_BLENDED in rec.qualifiers
    built = strategies_for(market(2, comparability=Comparability.CATEGORY_ATTRIBUTE))
    assert all(p.anchor.source is AnchorSource.MARKETPLACE
               for p in built.prices.values())


def test_rung_three_the_anchor_alone_prices_the_item():
    """The rung that did not exist. `estimate` produced a defensible band and
    `build_strategies` returned None, so the seller was sent to type a number
    with strictly less information than the agent had."""
    from resell.pricing.strategy import AnchorSource, SellerObjective

    built = strategies_for([])
    assert built is not None
    prices = {o: built.get(o).price_cents for o in SellerObjective}
    assert prices[SellerObjective.FAST_SALE] == 7156
    assert prices[SellerObjective.BALANCED] == 8132
    assert prices[SellerObjective.MAX_PROCEEDS] == 9108
    for price in built.prices.values():
        assert price.anchor.source is AnchorSource.RETAIL_ANCHOR
        assert not price.anchor.is_observed


def test_rung_four_is_reached_only_when_there_is_nothing():
    """"Set a price yourself" is the honest answer here and nowhere above it."""
    assert strategies_for([], retail=()) is None
    assert strategies_for([], retail=bowflex(only={Comparability.EXCLUDED})) is None


# --- the two kinds of evidence never merge --------------------------------------------


def test_an_anchored_price_is_not_described_as_comps():
    """`n=0`, no price kind, and words instead of sample statistics. "p75 of the
    anchor" would imply a distribution that does not exist."""
    built = strategies_for([])
    said = built.get(list(built.prices)[0]).anchor.describe()
    assert "comps" not in said
    for stat in ("min", "p25", "median", "p75", "max"):
        assert stat not in said, stat
    assert "retail-derived anchor" in said
    assert all(p.anchor.n == 0 and p.anchor.price_kind is None
               for p in built.prices.values())


def test_the_anchor_never_becomes_a_distribution():
    r = priced([])
    assert r.basis_distribution is None, "no sample was invented"
    assert r.price_kind is None
    assert r.n_in_basis == 0


def test_the_operator_page_can_render_an_anchored_price():
    """`describe()` asserted a price kind that an anchored recommendation does
    not have, so opening pricing on any such item raised AssertionError."""
    said = priced([]).describe()
    assert "no comps" in said
    assert "shop price" in said


def test_brand_strength_does_not_reach_into_the_anchor():
    """It selects which statistic of a *sample* to take, and there is no sample.
    A strong brand's standing is already inside the shop price."""
    from resell.pricing.strategy import (
        BrandSignal,
        BrandStrength,
        SellerObjective,
        build_strategies,
    )

    rec = priced([])
    plain = build_strategies(rec)
    premium = build_strategies(rec, brand=BrandSignal(
        strength=BrandStrength.PREMIUM, citations=("ev_1",)))
    for objective in SellerObjective:
        assert plain.get(objective).price_cents == premium.get(objective).price_cents
    assert "brand strength" not in premium.get(SellerObjective.MAX_PROCEEDS).selection_reason


def test_condition_is_not_counted_twice():
    """`retention_for` already scales by condition. Applying a condition
    adjustment on top would discount the same fact again."""
    from resell.pricing.comps import AdjustmentSource, ConditionAdjustment
    from resell.pricing.strategy import SellerObjective, build_strategies

    adjustment = ConditionAdjustment(
        magnitude_pct=-0.20, reason="a scuff on the base",
        source=AdjustmentSource.OPERATOR_STATED,
        from_band=BOBBY_CONDITION, to_band=ConditionBand.USED_GOOD,
        citations=("ev_1",),
    )
    rec = recommend(inp(comps=[], retail=shop_price(), category_path=BOBBY_CATEGORY,
                        item_condition_band=BOBBY_CONDITION,
                        adjustments=(adjustment,)))
    built = build_strategies(rec)
    assert built.get(SellerObjective.BALANCED).price_cents == 8132


def test_the_operator_is_told_the_band_was_not_observed():
    from resell.pricing.strategy import build_strategies

    note = build_strategies(priced([])).uncertainty_note
    assert "no comparable listing survived judging" in note
    assert "reasoned down from the current shop price" in note


def test_the_seller_is_told_in_their_own_words():
    from resell.views_consumer import price_confidence

    said = price_confidence(tuple(str(q) for q in priced([]).qualifiers))
    assert said.startswith("Nothing like this is for sale right now")


# --- the button the seller actually presses -------------------------------------------


def anchor_only_item(tmp_path):
    """An item whose only pricing evidence is the maker's own current price.

    Graded `same_family_variant` rather than `same_product` because that is what
    the record allows: `same_product` requires a resolved identity, and a comp
    found by research on an unresolved item cannot claim it. Which is exactly why
    `ANCHORABLE_RETAIL` includes the variant rung -- restricted to `same_product`
    this feature would almost never fire on a real item.
    """
    from tests.test_webui import NOW, seeded
    from resell import store_pricing as sp
    from resell.pricing.comps import CompBasis, CompObservation

    app, conn, gateway, sku = seeded(tmp_path, identified=True)
    gateway.begin_pricing(sku)
    sp.record_comp_observation(conn, CompObservation(
        comp_id="comp_shop", marketplace="beatsbydre.com", external_id="comp_shop",
        price_kind=PriceKind.REFERENCE, basis=CompBasis.ACTIVE_EXACT,
        price_cents=12995, observed_at=NOW,
        condition_band=ConditionBand.NEW_WITH_TAGS, shipping_cents=None,
        title="Beats Pill", url="https://beatsbydre.com/pill",
        retail_kind=RetailKind.CURRENT,
    ))
    candidate = sp.record_comp_candidate(
        conn, sku=sku, comp_id="comp_shop",
        proposed_comparability="same_family_variant",
        item_citations=("1",), comp_citations=("title",),
        rationale="the maker's page for this model",
    )
    client = app.test_client()
    client.post(f"/candidates/{candidate}/accept", data={"sku": sku},
                follow_redirects=True)
    return app, conn, sku, client


def test_the_price_shown_is_the_price_approved(tmp_path):
    """`approve_price` built its own `PricingInput` without the retail references
    or the category path, so it recomputed a *different* recommendation from the
    one on screen: three prices displayed, and "the evidence supports no
    strategy" when one was pressed. Every existing test missed it, because every
    existing test checked one side or the other."""
    from resell import store_pricing as sp
    from resell import views

    app, conn, sku, client = anchor_only_item(tmp_path)
    shown = views.pricing_view(
        conn, sku, views.default_pricing_request(conn, sku, marketplace="EBAY_US"),
        marketplace="EBAY_US",
    )
    assert [s.price_cents for s in shown.strategies] == [4254, 4834, 5414]

    response = client.post(f"/items/{sku}/price", data={"objective": "balanced"},
                           follow_redirects=True)
    assert response.status_code == 200
    assert sp.approved_price_cents(conn, sku) == 4834
    page = response.get_data(as_text=True)
    for leak in ("supports no strategy", "unpriceable", "Traceback"):
        assert leak not in page, leak


def test_one_assembly_serves_every_surface(tmp_path):
    """Three call sites built three different inputs. The page, the approval and
    the CLI have to price the same item the same way or the record is a record of
    a number nobody was shown."""
    import inspect

    from resell import views
    from resell.webui import app as webui_app
    from resell import cli_price

    assert "PricingInput(" not in inspect.getsource(webui_app)
    assert "PricingInput(" not in inspect.getsource(cli_price)
    assert inspect.getsource(views).count("PricingInput(") == 1
