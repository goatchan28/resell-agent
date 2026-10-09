"""Deterministic identification: tiers, one search, and what settles an identity.

The three-stage research loop these replace ran 97 model calls across 57 items and
never once produced a match, so the properties worth pinning down are the ones that
made it fail: a mode that depended on which English words a vision model chose, a
RESOLVED that only one dead stage could write, and a lookup nobody could tell had
happened.
"""

from __future__ import annotations

import json

import pytest

from resell import db
from resell.gateway import Gateway
from resell.reasoning.identity import (
    StrongIdentifier,
    best_identifier,
    carried_by,
    confirm,
    query_for,
    registrable_domain,
    run_identity_round,
    strong_identifiers,
    tier_for,
)
from resell.reasoning.research_loop import identity_resolution, mode_evidence
from resell.reasoning.schema import Basis, IdentifierScheme, IdentityResolution, Observation


class Hit:
    def __init__(self, url, title, snippet=""):
        self.url, self.title, self.snippet = url, title, snippet
        self.extra_snippets = ()


class Backend:
    provider = "fake_search"

    def __init__(self, hits=()):
        self.hits, self.queries = list(hits), []

    def find(self, query, limit=20):
        self.queries.append(query.query)
        return self.hits[:limit]


def item(tmp_path, name, *, brand=None, model=None, observations=(), identifiers=()):
    from resell.reasoning.schema import IdentifierObservation

    conn = db.connect(tmp_path / f"{name}.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=500).sku
    for claim in observations:
        gateway.record_observation(
            sku, Observation(claim=claim, basis=Basis.VISUAL_OBSERVATION,
                             photo_positions=(1,)),
        )
    for scheme, value in identifiers:
        gateway.record_identifier(
            sku, IdentifierObservation(IdentifierScheme(scheme), value,
                                       photo_position=1),
        )
    gateway.propose_identification(sku, category_id="3001", brand=brand, model=model)
    return conn, gateway, sku


def looked_hard(conn, sku):
    """The negative finding `observe` produces, as the observation stage stores it."""
    db.kv_set(conn, f"identity_search:{sku}", json.dumps({
        "surfaces_examined": ["front", "back", "underside"],
        "photos_reviewed": 3,
        "note": "no model number is legible on any surface",
    }))
    conn.commit()


# --- which tier, and why ------------------------------------------------------


def test_no_brand_and_no_code_is_tier_zero(tmp_path):
    conn, _, sku = item(tmp_path, "t0", observations=["A pink plush rabbit cushion"])
    tier = tier_for(conn, sku)
    assert tier.tier == 0
    assert not tier.searches


def test_a_brand_with_no_code_is_tier_one_and_searches_for_nothing(tmp_path):
    """A brand alone is a query that returns the catalogue, not this object."""
    conn, _, sku = item(tmp_path, "t1", brand="Swingline",
                        observations=["The brand name Swingline is on the top"])
    tier = tier_for(conn, sku)
    assert tier.tier == 1
    assert not tier.searches
    assert "catalogue" in tier.why


def test_a_product_code_is_tier_two(tmp_path):
    conn, _, sku = item(tmp_path, "t2", brand="Dell",
                        identifiers=[("model_number", "KM713")])
    tier = tier_for(conn, sku)
    assert tier.tier == 2
    assert tier.searches
    assert tier.identifiers[0].normalized == "KM713"


@pytest.mark.parametrize("scheme", ["makers_mark", "serial", "date_code", "other"])
def test_codes_that_do_not_denote_a_product_are_not_strong(tmp_path, scheme):
    """MP-000057 carried nine identifier observations. Eight were an FCC ID, an IC
    number, a CMIIT registration, a dealer code and the like -- searching those
    returns a certification, not a product."""
    conn, _, sku = item(tmp_path, f"weak-{scheme}", brand="Dell",
                        identifiers=[(scheme, "E8HKG-1152")])
    assert strong_identifiers(conn, sku) == ()
    assert tier_for(conn, sku).tier == 1


def test_a_model_an_observation_names_reaches_tier_two(tmp_path):
    conn, _, sku = item(tmp_path, "named", brand="Bowflex", model="SelectTech 552",
                        observations=["The base is stamped SelectTech 552"])
    assert tier_for(conn, sku).tier == 2


def test_a_model_nothing_observed_was_inferred_and_stays_at_tier_one(tmp_path):
    """It cannot be cited, so it cannot drive a search."""
    conn, _, sku = item(tmp_path, "inferred", brand="Bowflex", model="SelectTech 552",
                        observations=["A pair of adjustable dumbbells"])
    assert tier_for(conn, sku).tier == 1


# --- the one query ------------------------------------------------------------


def test_the_brand_joins_the_query_when_it_is_known(tmp_path):
    identifier = StrongIdentifier(1, IdentifierScheme.MODEL_NUMBER, "17070")
    assert query_for("iRobot", identifier) == "iRobot 17070"


def test_a_code_with_no_brand_is_still_searched(tmp_path):
    """The case with the most to gain from asking somebody else."""
    identifier = StrongIdentifier(1, IdentifierScheme.MODEL_NUMBER, "17070")
    assert query_for(None, identifier) == "17070"


def test_a_check_digit_scheme_is_preferred_when_several_were_read(tmp_path):
    identifiers = (
        StrongIdentifier(1, IdentifierScheme.MODEL_NUMBER, "DS126571"),
        StrongIdentifier(2, IdentifierScheme.UPC, "013803248210"),
    )
    assert best_identifier(identifiers).scheme is IdentifierScheme.UPC


def test_an_mpn_does_not_outrank_the_code_that_was_read_first(tmp_path):
    """MP-000033 recorded its camera body as a model number and its kit lens as an
    MPN. Ranking MPN higher sent the one query at the lens."""
    identifiers = (
        StrongIdentifier(1, IdentifierScheme.MODEL_NUMBER, "EOS Rebel T6i"),
        StrongIdentifier(2, IdentifierScheme.MPN, "EF-S 18-55mm"),
    )
    assert best_identifier(identifiers).normalized == "EOS Rebel T6i"


def test_a_brand_the_model_already_carries_is_not_repeated(tmp_path):
    """MP-000017 read its model as "DJI Osmo". `DJI DJI Osmo` is a worse query
    than either half."""
    identifier = StrongIdentifier(1, IdentifierScheme.MODEL_NUMBER, "DJI Osmo")
    assert query_for("DJI", identifier) == "DJI Osmo"


def test_read_order_breaks_a_tie(tmp_path):
    """MP-000026's camera body precedes its kit lens because that is the order the
    photographs were taken in."""
    identifiers = (
        StrongIdentifier(1, IdentifierScheme.MODEL_NUMBER, "EOS Rebel T6i"),
        StrongIdentifier(2, IdentifierScheme.MODEL_NUMBER, "EF-S 18-55mm"),
    )
    assert best_identifier(identifiers).normalized == "EOS Rebel T6i"


# --- does this page name the identifier ---------------------------------------


def test_a_code_is_matched_as_a_token_not_a_substring():
    """A collapsed-string search for S02 matches inside NS0214, and half of a
    different identifier is exactly the false positive that would send a wrong
    exact_product into comps."""
    assert carried_by("KM713", "Dell KM713 Wireless Keyboard")
    assert not carried_by("KM713", "Dell KM7134 Wireless Keyboard")


def test_a_composite_code_matches_on_its_code_token():
    """`100220547 - NAVY MINI HT` is mostly a colourway. A page naming the number is
    talking about this product whether or not it repeats the colour words."""
    assert carried_by("100220547 - NAVY MINI HT", "Brooks Brothers Suit 100220547")


def test_an_all_words_identifier_must_appear_in_order():
    assert carried_by("EOS Rebel T6i", "Canon EOS Rebel T6i Digital SLR")
    assert not carried_by("EOS Rebel T6i", "Canon EOS 90D and a Rebel T6i strap")


def test_a_subdomain_is_the_same_source():
    assert registrable_domain("https://shop.example.com/x") == "example.com"
    assert registrable_domain("https://www.example.com/y") == "example.com"


# --- what settles an identity -------------------------------------------------


IDENT = StrongIdentifier(7, IdentifierScheme.STYLE_NUMBER, "SUJT EXP 2BSV SLIM")
TITLE = "Explorer Slim Suit Jacket SUJT EXP 2BSV SLIM"


def test_one_authoritative_source_would_resolve_it_but_does_not():
    outcome = confirm(IDENT, [Hit("https://www.brooksbrothers.com/p/1", TITLE)])
    assert outcome.provisional is True
    assert outcome.resolved is False
    assert "not shipped" in outcome.reason
    assert "manufacturer" in outcome.reason


def test_a_registry_counts_as_authoritative():
    """The sources that actually answer identifier questions are registries, and
    they have no commercial interest in the answer. Recorded, not acted on."""
    outcome = confirm(IDENT, [Hit("https://fccid.io/E8HKG-1152", TITLE)])
    assert outcome.provisional is True
    assert outcome.resolved is False


def test_one_reseller_is_not_enough():
    outcome = confirm(IDENT, [Hit("https://poshmark.com/listing/1", TITLE)])
    assert outcome.resolved is False
    assert "single" in outcome.reason
    assert len(outcome.matches) == 1


def test_two_independent_resellers_corroborate_on_paper_only():
    outcome = confirm(IDENT, [
        Hit("https://poshmark.com/listing/1", TITLE),
        Hit("https://www.mercari.com/us/item/2", TITLE),
    ])
    assert outcome.provisional is True
    assert outcome.resolved is False
    assert len(outcome.sources) == 2


def test_one_site_saying_it_three_times_is_still_one_source():
    outcome = confirm(IDENT, [
        Hit("https://poshmark.com/listing/1", TITLE),
        Hit("https://poshmark.com/listing/2", TITLE),
        Hit("https://www.poshmark.com/listing/3", TITLE),
    ])
    assert outcome.resolved is False
    assert outcome.sources == ("poshmark.com",)


def test_sources_that_agree_on_nothing_else_do_not_corroborate():
    outcome = confirm(IDENT, [
        Hit("https://poshmark.com/1", "Explorer Slim Suit Jacket SUJT EXP 2BSV SLIM"),
        Hit("https://www.mercari.com/2", "Garden Hose Reel SUJT EXP 2BSV SLIM"),
    ])
    assert outcome.resolved is False
    assert "disagreement" in outcome.reason


def test_results_that_never_name_the_code_settle_nothing():
    outcome = confirm(IDENT, [Hit("https://www.brooksbrothers.com/p/1", "Suit Jackets")])
    assert outcome.resolved is False
    assert outcome.matches == []


# --- the round, end to end ----------------------------------------------------


def test_tier_zero_declares_described_object_without_searching(tmp_path):
    """`described_object` is a successful outcome for most household objects, and
    the loop this replaces could never reach it: the gate skipped the round, so
    nothing ever declared anything and the item stayed unresolved by default."""
    conn, gateway, sku = item(tmp_path, "round0",
                              observations=["A pink plush rabbit cushion"])
    looked_hard(conn, sku)
    backend = Backend()
    outcome = run_identity_round(conn, gateway, sku, backend=backend)
    assert backend.queries == []
    assert outcome.mode.accepted == "described_object"


def test_tier_one_declares_branded_generic_without_searching(tmp_path):
    conn, gateway, sku = item(tmp_path, "round1", brand="Swingline",
                              observations=["The name Swingline is on the top"])
    looked_hard(conn, sku)
    backend = Backend()
    outcome = run_identity_round(conn, gateway, sku, backend=backend)
    assert backend.queries == []
    assert outcome.mode.accepted == "branded_generic"


def test_without_the_negative_finding_the_claim_is_refused(tmp_path):
    """Both modes assert that no identity is discoverable, which is a claim about
    how hard somebody looked. MP-000056 was refused for exactly this, because the
    orchestrator dropped the finding `observe` had produced."""
    conn, gateway, sku = item(tmp_path, "nofinding", brand="Swingline",
                              observations=["The name Swingline is on the top"])
    outcome = run_identity_round(conn, gateway, sku, backend=Backend())
    assert outcome.mode.supported is False
    assert outcome.mode.accepted == "unresolved"


def test_tier_two_searches_once_and_holds_at_product_family(tmp_path):
    """The lookup runs, the sources are recorded, and the ceiling does not move.
    `exact_product` needs a resolved identity and resolution is held closed."""
    conn, gateway, sku = item(tmp_path, "round2", brand="Dell",
                              observations=["A Dell keyboard"],
                              identifiers=[("model_number", "KM713")])
    backend = Backend([Hit("https://fccid.io/E8HKG", "Dell KM713 Wireless Keyboard")])
    outcome = run_identity_round(conn, gateway, sku, backend=backend)

    assert backend.queries == ["Dell KM713"]
    assert outcome.confirmation.provisional is True
    assert outcome.mode.accepted == "product_family"
    assert identity_resolution(conn, sku) is IdentityResolution.SEARCHED_NOT_FOUND


def test_a_tier_two_search_that_confirms_nothing_is_product_family(tmp_path):
    conn, gateway, sku = item(tmp_path, "round2b", brand="Dell",
                              observations=["A Dell keyboard"],
                              identifiers=[("model_number", "KM713")])
    outcome = run_identity_round(
        conn, gateway, sku, backend=Backend([Hit("https://x.test/1", "Keyboards")]),
    )
    assert outcome.mode.accepted == "product_family"
    assert identity_resolution(conn, sku) is IdentityResolution.SEARCHED_NOT_FOUND


def test_a_lookup_is_recorded_so_searched_and_unattempted_stay_apart(tmp_path):
    conn, gateway, sku = item(tmp_path, "recorded", brand="Dell",
                              identifiers=[("model_number", "KM713")])
    assert identity_resolution(conn, sku) is IdentityResolution.UNATTEMPTED
    run_identity_round(conn, gateway, sku, backend=Backend([Hit("https://x.test/1", "x")]))
    assert conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'identity'",
        (sku,),
    ).fetchone()[0] == 1


def test_a_round_with_no_backend_records_no_lookup(tmp_path):
    """"Nobody searched" and "we searched and found nothing" are different facts,
    and only one of them is about the object."""
    from resell.reasoning.adapters.search import NoSearchBackend

    conn, gateway, sku = item(tmp_path, "nobackend", brand="Dell",
                              observations=["A Dell keyboard"],
                              identifiers=[("model_number", "KM713")])
    outcome = run_identity_round(conn, gateway, sku, backend=NoSearchBackend())
    assert outcome.stopped == "not_retrieved"
    assert identity_resolution(conn, sku) is IdentityResolution.UNATTEMPTED


def test_the_search_reads_the_index_and_never_claims_to_have_read_the_page(tmp_path):
    conn, gateway, sku = item(tmp_path, "provenance", brand="Dell",
                              identifiers=[("model_number", "KM713")])
    run_identity_round(
        conn, gateway, sku,
        backend=Backend([Hit("https://fccid.io/E8HKG", "Dell KM713 Keyboard")]),
    )
    methods = {row[0] for row in conn.execute(
        "SELECT retrieval_method FROM evidence WHERE subject = 'candidate_product'")}
    assert methods == {"search_index"}


# --- the mode gate reads the record, not the prose ----------------------------


def test_the_brand_is_read_off_the_identification_not_the_word_brand(tmp_path):
    """MP-000057's brand is Dell and it scored zero brand support, because no
    observation happened to contain the string "brand"."""
    conn, _, sku = item(tmp_path, "dell", brand="Dell",
                        observations=["A DELL logo is printed above the arrow keys"])
    assert mode_evidence(conn, sku)["brand_support"]


def test_an_eyebrow_is_not_a_product_line(tmp_path):
    """MP-000058's only line support was `LIKE '%line%'` matching the sentence
    "the right eyebrow is a short straight horizontal black line"."""
    conn, _, sku = item(tmp_path, "eyebrow", observations=[
        "The right eyebrow is a short straight horizontal black line",
    ])
    assert mode_evidence(conn, sku)["line_support"] == ()


def test_the_gate_does_not_cite_its_own_summary_back_to_itself(tmp_path):
    """MP-000056's brand support included the `research not pursued: ...` summary
    the previous round had written."""
    conn, gateway, sku = item(tmp_path, "circular", brand="Swingline",
                              observations=["The name Swingline is on the top"])
    gateway.record_research_negative(
        sku, summary="research not pursued: the brand is Swingline but no model",
        detail={},
    )
    ids = {ref.evidence_id for ref in mode_evidence(conn, sku)["brand_support"]}
    negatives = {row[0] for row in conn.execute(
        "SELECT id FROM evidence WHERE sku = ? AND kind = 'research_negative'", (sku,))}
    assert not (ids & negatives)


def test_a_makers_mark_alone_does_not_establish_a_family(tmp_path):
    """MP-000056 had three identifier observations and `product_family` came out
    supported. Two were the brand mark and the third was garbled boilerplate."""
    conn, _, sku = item(tmp_path, "marks", brand="Swingline",
                        observations=["The name Swingline is on the top"],
                        identifiers=[("makers_mark", "Swingline"),
                                     ("other", "PRO-N-LUE ... ANCE 3 STAPLES")])
    evidence = mode_evidence(conn, sku)
    assert evidence["brand_support"]
    assert evidence["line_support"] == ()


# --- what the live replay found -----------------------------------------------
#
# Four defects that only appeared against real Brave results, kept here so they
# cannot come back quietly.


def test_a_composite_code_is_searched_by_its_number(tmp_path):
    """`Brooks Brothers 100220547 - NAVY MINI HT` returned twenty results and none
    named the code, because the colourway words narrowed the query. `carried_by`
    already matched on the number alone; the query has to ask the same thing."""
    from resell.reasoning.identity import matched_part

    identifier = StrongIdentifier(1, IdentifierScheme.STYLE_NUMBER,
                                  "100220547 - NAVY MINI HT")
    assert query_for("Brooks Brothers", identifier) == "Brooks Brothers 100220547"
    assert matched_part(identifier.normalized) == "100220547"


def test_a_host_no_adapter_may_fetch_is_not_a_witness():
    """eBay donates nothing because it is absent from the authority table. Letting
    it *count* toward corroboration would let it decide instead."""
    outcome = confirm(IDENT, [
        Hit("https://www.ebay.com/itm/1", TITLE),
        Hit("https://poshmark.com/listing/1", TITLE),
    ])
    assert "ebay.com" not in outcome.sources
    assert outcome.resolved is False


def test_sources_that_disagree_are_not_recorded_as_matches(tmp_path):
    """iRobot's `17070` was named by a manual, a charger, a dock and a refurb.
    `confirm` called that a disagreement -- and `identity_resolution` then counted
    the rows and called it RESOLVED anyway. The agreement test is asked once."""
    conn, gateway, sku = item(tmp_path, "disagree", brand="iRobot",
                              observations=["A label reads 17070"],
                              identifiers=[("model_number", "17070")])
    outcome = run_identity_round(conn, gateway, sku, backend=Backend([
        Hit("https://manualslib.com/1", "Irobot 17070 Manuals"),
        Hit("https://www.newegg.com/2", "Refurbished Roomba Dock Charger 17070"),
    ]))
    assert outcome.confirmation.resolved is False
    assert identity_resolution(conn, sku) is IdentityResolution.SEARCHED_NOT_FOUND
    assert {row[0] for row in conn.execute(
        "SELECT is_match FROM product_match WHERE sku = ?", (sku,))} == {0}


def test_the_mode_and_the_resolution_never_disagree(tmp_path):
    """A live replay produced items reading `resolution=resolved, mode=unresolved`,
    which is not a position anything downstream knows how to read. Both now ask
    `identity_resolution`, so they move together or not at all."""
    conn, gateway, sku = item(tmp_path, "agree", brand="Beats by Dr. Dre",
                              observations=["A Beats by Dr. Dre label reads A3211"],
                              identifiers=[("model_number", "A3211")])
    outcome = run_identity_round(conn, gateway, sku, backend=Backend([
        Hit("https://ezpawn.com/1", "Beats By Dr. Dre A3211 Portable Speaker"),
        Hit("https://paymore.com/2", "Beats By Dr. Dre Beats Pill A3211 Speaker"),
    ]))
    assert identity_resolution(conn, sku) is IdentityResolution.SEARCHED_NOT_FOUND
    assert outcome.mode.accepted == "product_family"
    assert outcome.mode.supported is True


# --- the brand `observe` read, before map_aspects writes it down --------------


def test_a_makers_mark_supplies_the_brand_before_map_aspects_runs(tmp_path):
    """The identity round runs *before* `map_aspects`, and `map_aspects` is what
    populates `identification.brand`. Reading only that column made MP-000059 -- a
    stapler carrying `makers_mark: Swingline` twice -- come out tier 0 and declare
    `described_object`, a mode whose entire content is that no brand is
    discoverable."""
    from resell.reasoning.identity import observed_brand

    conn, _, sku = item(tmp_path, "mark", brand=None,
                        observations=["The name Swingline is printed on the top"],
                        identifiers=[("makers_mark", "Swingline")])
    assert observed_brand(conn, sku) == "Swingline"
    tier = tier_for(conn, sku)
    assert tier.tier == 1
    assert tier.brand == "Swingline"


def test_the_identification_brand_still_wins_when_it_exists(tmp_path):
    conn, _, sku = item(tmp_path, "prefer", brand="Bowflex",
                        observations=["A Bowflex label"],
                        identifiers=[("makers_mark", "SelectTech")])
    assert tier_for(conn, sku).brand == "Bowflex"


def test_the_observed_brand_reaches_the_tier_two_query(tmp_path):
    """The replay showed this is worth having: a bare `A3211` matched a senate bill
    and an airline flight, while `Beats by Dr. Dre A3211` matched the speaker."""
    from resell.reasoning.identity import best_identifier

    conn, _, sku = item(tmp_path, "q", brand=None,
                        identifiers=[("makers_mark", "Dell"),
                                     ("model_number", "KM713")])
    tier = tier_for(conn, sku)
    assert tier.tier == 2
    assert query_for(tier.brand, best_identifier(tier.identifiers)) == "Dell KM713"


def test_a_makers_mark_is_not_itself_something_to_search_for(tmp_path):
    """It is a brand, which is tier 1 by definition -- it supplies the brand and
    never becomes the identifier."""
    conn, _, sku = item(tmp_path, "markonly", brand=None,
                        identifiers=[("makers_mark", "Swingline")])
    tier = tier_for(conn, sku)
    assert tier.identifiers == ()
    assert not tier.searches


def test_the_first_mark_read_wins(tmp_path):
    """Several marks are almost always one brand repeated across surfaces. Taking
    the earliest keeps this a transcription rather than a judgement."""
    from resell.reasoning.identity import observed_brand

    conn, _, sku = item(tmp_path, "several", brand=None,
                        identifiers=[("makers_mark", "Swingline"),
                                     ("makers_mark", "Swingline Inc")])
    assert observed_brand(conn, sku) == "Swingline"
