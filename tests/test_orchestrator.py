"""What an item needs next, and stopping the moment it needs a person.

The sequencing knowledge tested here used to live in the operator's head: that
observing precedes mapping, that a blocking question stops everything, that
pricing needs an accepted comparable rather than a recorded one, and that
proposing a listing will refuse without an approved price.

`advance` is tested with a fake runner. The point of these tests is the order and
the stopping rule, and standing up a model provider to assert an order would make
them slow and flaky without testing anything more.
"""

from __future__ import annotations

import json

import pytest

from resell import db, store_pricing as sp
from resell.domain import FeeModel
from resell.gateway import Gateway
from resell.orchestrator import (
    Actor, RunReport, Step, advance, comp_research_exhausted, next_step,
)
from resell.pricing.comps import (
    CompBasis,
    CompClaim,
    Comparability,
    CompObservation,
    ConditionBand,
    PriceKind,
)

NOW = __import__("datetime").datetime(2026, 8, 22, tzinfo=__import__("datetime").UTC)


def fixture(tmp_path):
    conn = db.connect(tmp_path / "flow.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    return conn, gateway, sku


def with_photo(conn, gateway, sku):
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )


def with_observation(conn, sku, text="a red speaker"):
    conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
        "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
        (sku, "vision_observation", "fake/m", json.dumps({"claim": text}),
         db.now_iso(), "visual_observation"),
    )
    conn.commit()


def identified(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    with_observation(conn, sku)
    gateway.begin_identification(sku)
    gateway.propose_identification(
        sku, title="Beats Pill", category_id="111694", condition_id="USED_GOOD",
        aspects={"Brand": ["Beats by Dr. Dre"]},
    )
    return conn, gateway, sku


# --- what comes next ---------------------------------------------------------


def test_a_new_item_needs_photographs(tmp_path):
    conn, _, sku = fixture(tmp_path)
    step = next_step(conn, sku)
    assert step.step is Step.ATTACH_PHOTOS
    assert step.actor is Actor.OPERATOR


def test_a_photographed_item_is_the_agents_to_start(tmp_path):
    """intake -> identifying first. Naming a later step from intake produced one
    the gateway then refused, which is the sequencing bug this module removes."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    step = next_step(conn, sku)
    assert step.step is Step.START_IDENTIFICATION
    assert step.actor is Actor.AGENT


def test_a_started_item_is_looked_at(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    assert next_step(conn, sku).step is Step.OBSERVE


def test_a_blocking_question_outranks_every_agent_step(tmp_path):
    """A question asked and worked around is the failure the operator loop exists
    to prevent, so it comes before anything the agent could do instead."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.ask_operator(sku, question="What size?", why_it_matters="")
    step = next_step(conn, sku)
    assert step.step is Step.ANSWER_QUESTIONS
    assert step.actor is Actor.OPERATOR
    assert step.count == 1


def test_an_item_with_no_category_needs_one_chosen(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    assert next_step(conn, sku).step is Step.SUGGEST_CATEGORY


def test_an_unresolved_item_asks_before_pricing(tmp_path):
    """`identified` here means "the agent filled in a form", not "the agent knows
    what this is". Research never resolved it, so pricing waits for a person --
    which is the gap MP-000013 fell through."""
    conn, _, sku = identified(tmp_path)
    step = next_step(conn, sku)
    assert step.step is Step.CONFIRM_IDENTITY
    assert step.actor is Actor.OPERATOR


def test_a_confirmed_item_moves_to_pricing(tmp_path):
    from resell.orchestrator import confirm_identity

    conn, _, sku = identified(tmp_path)
    confirm_identity(conn, sku)
    step = next_step(conn, sku)
    assert step.step is Step.BEGIN_PRICING
    assert step.actor is Actor.AGENT


def test_a_priced_item_with_no_comps_asks_the_operator_for_links(tmp_path):
    """The operator's step, and that is what stops the hang: the retrieval adapter
    used to ask for URLs on stdin, so running it from the web server parked the
    request on `input()` against the terminal the server was launched from."""
    conn, gateway, sku = identified(tmp_path)
    gateway.begin_pricing(sku)
    step = next_step(conn, sku)
    assert step.step is Step.COMP_RESEARCH
    assert step.actor is Actor.OPERATOR


def test_a_listed_item_needs_nothing(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    conn.execute("UPDATE item SET state = 'listed' WHERE sku = ?", (sku,))
    conn.commit()
    step = next_step(conn, sku)
    assert step.step is Step.DONE
    assert step.actor is Actor.NOBODY


def test_an_abandoned_item_needs_nothing(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.abandon(sku, reason="not worth it")
    assert next_step(conn, sku).actor is Actor.NOBODY


# --- comparables are a decision, not two -------------------------------------


def comp_row(comp_id, price_cents):
    return CompObservation(
        comp_id=comp_id, marketplace="poshmark.com", external_id=comp_id,
        price_kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
        price_cents=price_cents, observed_at=NOW,
        condition_band=ConditionBand.USED_GOOD, title=f"listing {comp_id}",
    )


def offered(conn, sku, comp_id="comp_a", price_cents=7995):
    sp.record_comp_observation(conn, comp_row(comp_id, price_cents))
    return sp.record_comp_candidate(
        conn, sku=sku, comp_id=comp_id,
        proposed_comparability="same_family_variant",
        item_citations=("1",), comp_citations=("title",), rationale="same line",
    )


def test_a_pending_comparable_is_the_operators(tmp_path):
    conn, gateway, sku = identified(tmp_path)
    gateway.begin_pricing(sku)
    offered(conn, sku)
    step = next_step(conn, sku)
    assert step.step is Step.REVIEW_COMPS
    assert step.actor is Actor.OPERATOR


def test_accepting_a_comparable_writes_the_claim(tmp_path):
    """One action, both rows. The split never surfaces."""
    conn, gateway, sku = identified(tmp_path)
    gateway.begin_pricing(sku)
    candidate_id = offered(conn, sku)
    sp.accept_comp_candidate(conn, candidate_id, identity_resolution="searched_not_found")
    assert len(sp.load_scored_comps(conn, sku)) == 1
    assert sp.pending_comp_candidates(conn, sku) == []


def test_rejecting_a_comparable_records_the_exclusion(tmp_path):
    conn, gateway, sku = identified(tmp_path)
    gateway.begin_pricing(sku)
    candidate_id = offered(conn, sku)
    sp.reject_comp_candidate(conn, candidate_id, reason="a bundle of three")
    row = conn.execute("SELECT comparability, excluded_reason FROM comp_claim").fetchone()
    assert row["comparability"] == "excluded"
    assert row["excluded_reason"] == "a bundle of three"


def test_a_rejection_without_a_reason_is_refused(tmp_path):
    conn, gateway, sku = identified(tmp_path)
    gateway.begin_pricing(sku)
    candidate_id = offered(conn, sku)
    with pytest.raises(ValueError, match="must record why"):
        sp.reject_comp_candidate(conn, candidate_id, reason="   ")


def test_a_ceiling_refusal_leaves_the_candidate_pending(tmp_path):
    """The operator sees why rather than finding it silently gone."""
    conn, gateway, sku = identified(tmp_path)
    gateway.begin_pricing(sku)
    sp.record_comp_observation(conn, comp_row("comp_b", 8995))
    candidate_id = sp.record_comp_candidate(
        conn, sku=sku, comp_id="comp_b", proposed_comparability="same_product",
        item_citations=("1",), comp_citations=("title",),
    )
    with pytest.raises(ValueError, match="requires identity_resolution=resolved"):
        sp.accept_comp_candidate(
            conn, candidate_id, identity_resolution="searched_not_found"
        )
    assert len(sp.pending_comp_candidates(conn, sku)) == 1


def test_an_accepted_comparable_leads_to_a_price_decision(tmp_path):
    conn, gateway, sku = identified(tmp_path)
    gateway.begin_pricing(sku)
    candidate_id = offered(conn, sku)
    sp.accept_comp_candidate(conn, candidate_id, identity_resolution="searched_not_found")
    step = next_step(conn, sku)
    assert step.step is Step.APPROVE_PRICE
    assert step.actor is Actor.OPERATOR


def test_a_comp_is_not_offered_twice(tmp_path):
    """Re-running research must not multiply the queue, and must not resurrect a
    comp already decided."""
    conn, gateway, sku = identified(tmp_path)
    gateway.begin_pricing(sku)
    sp.record_comp_observation(conn, comp_row("comp_a", 7995))
    for _ in range(3):
        sp.record_comp_candidate(
            conn, sku=sku, comp_id="comp_a",
            proposed_comparability="same_family_variant",
            item_citations=("1",), comp_citations=("title",),
        )
    assert len(sp.pending_comp_candidates(conn, sku)) == 1


# --- running ------------------------------------------------------------------


class FakeRunner:
    """Records the order and performs the state change each step implies."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.ran: list[Step] = []

    def run(self, conn, gateway, sku, step):
        self.ran.append(step)
        if step is Step.START_IDENTIFICATION:
            gateway.begin_identification(sku)
        elif step is Step.OBSERVE:
            with_observation(conn, sku)
        elif step is Step.SUGGEST_CATEGORY:
            gateway.propose_identification(sku, category_id="111694")
        elif step is Step.RESEARCH_IDENTITY:
            from resell.reasoning.identity import run_identity_round

            run_identity_round(conn, gateway, sku)
        elif step is Step.MAP_ASPECTS:
            from resell.cli_item import merged_identification

            fields, _ = merged_identification(
                conn, sku, aspects={"Brand": ["Beats by Dr. Dre"]}
            )
            gateway.propose_identification(sku, **fields)
        elif step is Step.GRADE_CONDITION:
            from resell.cli_item import merged_identification

            fields, _ = merged_identification(conn, sku, condition_id="USED_GOOD")
            gateway.propose_identification(sku, **fields)
        elif step is Step.DRAFT:
            from resell.cli_item import merged_identification

            fields, _ = merged_identification(conn, sku, title="Beats Pill")
            gateway.propose_identification(sku, **fields)
        elif step is Step.BEGIN_PRICING:
            gateway.begin_pricing(sku)
        return "done"


def test_advance_runs_the_agent_steps_in_order(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    runner = FakeRunner(gateway)
    report = advance(conn, gateway, sku, runner=runner, max_steps=12)

    assert runner.ran == [
        Step.START_IDENTIFICATION, Step.OBSERVE, Step.SUGGEST_CATEGORY,
        Step.RESEARCH_IDENTITY, Step.MAP_ASPECTS, Step.GRADE_CONDITION,
        Step.DRAFT,
    ]
    # Research never resolved this fixture to a product, so the agent stops and
    # asks rather than spending a pricing budget on a guess. MP-000013 went
    # photographs-to-pricing in sixty-two seconds without this.
    assert report.stopped_at.step is Step.CONFIRM_IDENTITY
    assert report.stopped_at.waiting_on_operator


def test_a_confirmed_identity_lets_pricing_begin(tmp_path):
    from resell.orchestrator import confirm_identity

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    advance(conn, gateway, sku, runner=FakeRunner(gateway), max_steps=12)
    assert next_step(conn, sku).step is Step.CONFIRM_IDENTITY

    confirm_identity(conn, sku, note="it is a Bowflex")

    runner = FakeRunner(gateway)
    report = advance(conn, gateway, sku, runner=runner, max_steps=12)
    assert Step.BEGIN_PRICING in runner.ran
    assert report.stopped_at.step is Step.COMP_RESEARCH


def test_a_supported_identification_is_not_rubber_stamped(tmp_path):
    """Stopping on an identification the agent can defend would be asking the
    operator to approve work that already carries its own evidence.

    MP-000059 and MP-000060 were both asked, and neither was ambiguous: a Swingline
    stapler and a handmade crocheted footbag, each with a mode the gate had already
    accepted. The seam was reading `RESOLVED`, which is held closed."""
    from resell.orchestrator import identity_needs_confirming

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    gateway.propose_identification(sku, category_id="111694", title="A stapler")
    needs, why = identity_needs_confirming(conn, sku)
    assert needs and "could not work out what this is" in why

    conn.execute(
        "UPDATE identification SET mode = 'described_object' "
        "WHERE sku = ? AND superseded_at IS NULL", (sku,),
    )
    conn.commit()
    needs, why = identity_needs_confirming(conn, sku)
    assert not needs
    assert "described_object" in why


def test_an_unresolved_mode_is_still_a_question(tmp_path):
    """The narrow case that remains: the record earns nothing, so nothing the
    agent drafted rests on a supported identification."""
    from resell.orchestrator import identity_needs_confirming

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    gateway.propose_identification(sku, category_id="111694", title="A thing")
    conn.execute(
        "UPDATE identification SET mode = 'unresolved' "
        "WHERE sku = ? AND superseded_at IS NULL", (sku,),
    )
    conn.commit()
    assert identity_needs_confirming(conn, sku)[0] is True


def test_advance_stops_at_a_question_without_running_anything(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.ask_operator(sku, question="What size?", why_it_matters="")
    runner = FakeRunner(gateway)
    report = advance(conn, gateway, sku, runner=runner)
    assert runner.ran == []
    assert report.stopped_at.step is Step.ANSWER_QUESTIONS
    assert not report.progressed


def test_advance_stops_when_a_stage_fails(tmp_path):
    """And tries it again first. A stage failing is a normal outcome and most of
    them in practice are transient, so the run gets a second attempt before it
    gives the step back."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class Broken:
        def __init__(self):
            self.attempts = 0

        def run(self, *a):
            self.attempts += 1
            raise RuntimeError("the model refused")

    broken = Broken()
    report = advance(conn, gateway, sku, runner=broken)
    assert broken.attempts == 2
    # Both attempts are on the record, filed by what they turned out to be: the
    # first was recovered from as far as anyone knew at the time, the second is
    # what actually stopped the run.
    assert report.retried == ["start_identification: the model refused"]
    assert report.errors == ["start_identification: the model refused"]
    assert not report.progressed
    # and the step is still owed, not stepped over
    assert report.blocked is not None
    assert report.blocked.step is Step.START_IDENTIFICATION
    assert report.blocked_attempts == 2


def test_a_stage_that_succeeds_on_the_second_attempt_carries_on(tmp_path):
    """The case the retry exists for. MP-000037's drafting failed, the operator
    pressed the button again, and the very next attempt produced a 74-character
    title."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class FlakyOnce:
        def __init__(self):
            self.attempts = 0

        def run(self, conn, gateway, sku, step):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("a page timed out")
            from resell.orchestrator import StageRunner

            return StageRunner().run(conn, gateway, sku, step)

    report = advance(conn, gateway, sku, runner=FlakyOnce(), max_steps=1)
    assert report.progressed
    assert report.blocked is None


def test_a_refusal_is_not_retried(tmp_path):
    """A second attempt would be refused by the same rule, having spent the
    money to find out."""
    from resell.gateway import Rejected

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class Refuses:
        def __init__(self):
            self.attempts = 0

        def run(self, *a):
            self.attempts += 1
            raise Rejected("BeginIdentification", ["no photos have passed validation"])

    refuses = Refuses()
    report = advance(conn, gateway, sku, runner=refuses)
    assert refuses.attempts == 1
    assert report.blocked is not None


def test_an_exhausted_budget_is_not_retried(tmp_path):
    from resell.reasoning.budget import BudgetExceeded

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class Spent:
        def __init__(self):
            self.attempts = 0

        def run(self, *a):
            self.attempts += 1
            raise BudgetExceeded("that is the ceiling for this stage")

    spent = Spent()
    advance(conn, gateway, sku, runner=spent)
    assert spent.attempts == 1


def test_a_step_that_changes_nothing_stops_rather_than_repeating(tmp_path):
    """A stage that runs and leaves the item where it was would otherwise spin
    against a paid model call."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class Idle:
        def run(self, *a):
            return "nothing changed"

    report = advance(conn, gateway, sku, runner=Idle())
    assert len(report.ran) == 1
    assert any("still needs it" in e for e in report.errors)


def test_advance_on_a_finished_item_does_nothing(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    conn.execute("UPDATE item SET state = 'listed' WHERE sku = ?", (sku,))
    conn.commit()
    report = advance(conn, gateway, sku, runner=FakeRunner(gateway))
    assert not report.progressed
    assert report.stopped_at.actor is Actor.NOBODY


# --- what the orchestrator may not do ------------------------------------------


def test_the_runner_has_no_method_for_any_operator_step():
    """The stopping rule as a property of the code: there is no implementation of
    a decision, so no amount of sequencing can reach one."""
    from resell.orchestrator import StageRunner

    for step in (Step.ANSWER_QUESTIONS, Step.REVIEW_COMPS, Step.APPROVE_PRICE,
                 Step.APPROVE_LISTING, Step.PUBLISH, Step.ATTACH_PHOTOS):
        assert not hasattr(StageRunner, f"_{step}"), step


def test_next_step_writes_nothing(tmp_path):
    conn, gateway, sku = identified(tmp_path)
    before = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    for _ in range(3):
        next_step(conn, sku)
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == before


# --- assembling the listing ---------------------------------------------------


def test_a_proposal_with_no_price_is_refused_rather_than_crashing(tmp_path):
    """`Proposal.problems()` compared the price with `<=`, so a missing one raised
    a TypeError instead of the refusal the operator should have seen. The price
    belongs to the pricing approval, so omitting it is an ordinary caller error."""
    from resell.domain import Proposal, ShippingTerms

    proposal = Proposal(
        sku="MP-000001", marketplace="EBAY_US", title="A speaker",
        description="Words.", category_id="111694", condition_id="USED_GOOD",
        aspects={}, price_cents=None, currency="USD",
        shipping_terms=ShippingTerms.SELLER_PAID,
        seller_shipping_cost_cents=0, buyer_shipping_charge_cents=0,
        photo_hashes=("a" * 64,),
        fulfillment_policy_id="f", payment_policy_id="p",
        return_policy_id="r", merchant_location_key="m",
    )
    problems = proposal.validate()
    assert any("price is missing" in p for p in problems)


def test_the_runner_takes_the_price_from_the_approval(tmp_path):
    """Not chosen here. The gateway checks this figure against the approval and
    refuses a mismatch, so reading it is fetching the answer."""
    from resell.orchestrator import StageRunner

    conn, gateway, sku = identified(tmp_path)
    gateway.begin_pricing(sku)
    candidate_id = offered(conn, sku)
    sp.accept_comp_candidate(conn, candidate_id, identity_resolution="searched_not_found")

    runner = StageRunner()
    with pytest.raises(RuntimeError, match="no approved price"):
        runner._propose_listing(conn, gateway, sku)


# --- identification research, decided rather than asked for ---------------------


def with_identifier(conn, sku, scheme="model_number", value="A3211"):
    import json as _json

    conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
        "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
        (sku, "identifier_observation", "fake/m",
         _json.dumps({"scheme": scheme, "normalized": value,
                      "raw_transcription": value, "photo_position": 1}),
         db.now_iso(), "text_read"),
    )
    conn.commit()


def test_research_is_warranted_when_identity_is_open_and_there_is_a_code(tmp_path):
    from resell.orchestrator import research_warranted

    conn, gateway, sku = identified(tmp_path)
    with_identifier(conn, sku)
    warranted, why = research_warranted(conn, sku)
    assert warranted
    assert "A3211" in why


def test_a_brand_alone_still_runs_the_round_but_searches_for_nothing(tmp_path):
    """A brand alone returns the catalogue, not this object -- so no lookup. But
    the round still runs: `branded_generic` is a conclusion, and the old gate
    skipped the round entirely, which left every such item `unresolved` by
    default rather than by decision."""
    from resell.orchestrator import research_warranted
    from resell.reasoning.identity import tier_for

    conn, gateway, sku = identified(tmp_path)
    gateway.propose_identification(sku, brand="Beats by Dr. Dre")
    warranted, why = research_warranted(conn, sku)
    assert warranted
    assert "tier 1" in why
    assert not tier_for(conn, sku).searches


def test_a_serial_number_is_not_something_to_search_for(tmp_path):
    """It identifies one physical unit. A lookup on it cannot succeed."""
    from resell.reasoning.identity import strong_identifiers, tier_for

    conn, gateway, sku = identified(tmp_path)
    with_identifier(conn, sku, scheme="serial", value="FK4HQR32390")
    assert strong_identifiers(conn, sku) == ()
    assert not tier_for(conn, sku).searches


def test_the_brand_joins_the_query_when_a_code_was_read(tmp_path):
    """A brand is not a search on its own; it is what disambiguates one. `17070`
    is ambiguous across every manufacturer that ever numbered a product."""
    from resell.reasoning.identity import best_identifier, query_for, tier_for

    conn, gateway, sku = identified(tmp_path)
    with_identifier(conn, sku, scheme="model_number", value="SelectTech 552")
    gateway.propose_identification(sku, brand="Bowflex")
    tier = tier_for(conn, sku)
    assert tier.searches
    assert query_for(tier.brand, best_identifier(tier.identifiers)) == (
        "Bowflex SelectTech 552"
    )


def test_one_donating_match_resolves_the_identity(tmp_path):
    """The original rule, unchanged: an identifier-strength match from a source
    good enough to donate."""
    from resell.reasoning.research_loop import identity_resolution
    from resell.reasoning.schema import IdentityResolution

    conn, gateway, sku = identified(tmp_path)
    with_identifier(conn, sku)
    conn.execute(
        "INSERT INTO product_match (sku, candidate_ref, strength, source_authority, "
        "rationale, item_evidence, candidate_evidence, is_match, donation_scope, "
        "created_at) VALUES (?,?,?,?,?,?,?,1,?,?)",
        (sku, "beatsbydre.com", "identifier_verified", "manufacturer", "matched",
         "[]", "[]", "attributes", db.now_iso()),
    )
    conn.commit()
    assert identity_resolution(conn, sku) is IdentityResolution.RESOLVED


def test_the_round_is_not_repeated_once_a_mode_has_been_declared(tmp_path):
    """The round is deterministic, so running it twice against an unchanged record
    produces the same answer and spends a second lookup to get it."""
    from resell.orchestrator import research_warranted

    conn, gateway, sku = identified(tmp_path)
    with_identifier(conn, sku)
    assert research_warranted(conn, sku)[0]
    db.log_event(conn, "identification.mode_declared", {"accepted": "product_family"},
                 item_id=sku)
    conn.commit()
    warranted, why = research_warranted(conn, sku)
    assert not warranted
    assert "already been declared" in why


def test_the_orchestrator_offers_research_before_aspect_mapping(tmp_path):
    """What research finds becomes citable evidence that mapping can use."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    with_identifier(conn, sku)
    gateway.propose_identification(sku, category_id="111694", title="A speaker")

    step = next_step(conn, sku)
    assert step.step is Step.RESEARCH_IDENTITY
    assert step.actor is Actor.AGENT


def test_research_is_the_agents_and_never_the_operators(tmp_path):
    """The product rule: the operator does not trigger research."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    with_identifier(conn, sku)
    gateway.propose_identification(sku, category_id="111694")
    step = next_step(conn, sku)
    assert step.step is Step.RESEARCH_IDENTITY
    assert step.actor is Actor.AGENT
    from resell.orchestrator import StageRunner

    assert hasattr(StageRunner, "_research_identity")


def test_an_item_with_no_identifier_declares_its_mode_then_maps(tmp_path):
    """Even with nothing to search for. `described_object` is the conclusion for
    most household objects, and it has to be reached rather than defaulted to."""
    from resell.reasoning.identity import run_identity_round

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    gateway.propose_identification(sku, category_id="111694", title="A speaker")
    assert next_step(conn, sku).step is Step.RESEARCH_IDENTITY
    run_identity_round(conn, gateway, sku)
    assert next_step(conn, sku).step is Step.MAP_ASPECTS


# --- condition, decided from the photographs -------------------------------------


def test_an_item_with_no_condition_is_graded_before_drafting(tmp_path):
    """Nothing set this before, so every item stalled at begin_pricing."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    gateway.propose_identification(
        sku, category_id="137865", aspects={"Brand": ["Bowflex"]}
    )
    step = next_step(conn, sku)
    assert step.step is Step.GRADE_CONDITION
    assert step.actor is Actor.AGENT


def test_a_graded_item_moves_on_to_drafting(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    gateway.propose_identification(
        sku, category_id="137865", condition_id="USED_EXCELLENT",
        aspects={"Brand": ["Bowflex"]},
    )
    assert next_step(conn, sku).step is Step.DRAFT


def test_a_grade_outside_the_categorys_list_is_refused(tmp_path):
    """Recording one would put a value in the identification that publishing
    rejects, which is the stall this closes rather than moves."""
    from resell.reasoning.tools import parse_condition_tool_input

    choice = parse_condition_tool_input(
        {"condition": "USED", "evidence_ids": [1], "rationale": "worn"},
        allowed={"NEW", "USED_EXCELLENT"}, valid_evidence_ids={1},
    )
    assert not choice.usable
    assert "not one of the grades" in choice.malformed[0]


def test_an_uncited_grade_is_refused(tmp_path):
    from resell.reasoning.tools import parse_condition_tool_input

    choice = parse_condition_tool_input(
        {"condition": "NEW", "evidence_ids": [], "rationale": "looks new"},
        allowed={"NEW"}, valid_evidence_ids={1},
    )
    assert not choice.usable
    assert "cites no observation" in choice.malformed[0]


def test_uncertainty_travels_with_the_grade(tmp_path):
    """Photographs miss things, and 'no visible damage in these photos' is not the
    same claim as 'no damage'."""
    from resell.reasoning.tools import parse_condition_tool_input

    choice = parse_condition_tool_input(
        {"condition": "USED_EXCELLENT", "evidence_ids": [1], "rationale": "r",
         "uncertain_because": "no close-up of the dial mechanism"},
        allowed={"USED_EXCELLENT"}, valid_evidence_ids={1},
    )
    assert choice.uncertain_because == "no close-up of the dial mechanism"


# --- brand and model reach their columns ------------------------------------------


def test_resolved_brand_and_model_reach_the_columns(tmp_path):
    """They were in the aspects blob only, so identification research -- which
    looks for a brand-and-line pair -- skipped items whose brand was resolved."""
    from resell.cli_item import _brand_and_model_from

    assert _brand_and_model_from(
        {"Brand": ["Bowflex"], "Product Line": ["SelectTech 552"]}
    ) == {"brand": "Bowflex", "model": "SelectTech 552"}


def test_a_model_an_observation_names_is_searchable(tmp_path):
    """The end of MP-000009: a model somebody read off the object reaches tier 2
    even though it was written down on the identification rather than as an
    identifier observation."""
    from resell.reasoning.identity import tier_for

    conn, gateway, sku = identified(tmp_path)
    with_observation(conn, sku, text="The base is printed SelectTech 552.")
    gateway.propose_identification(sku, brand="Bowflex", model="SelectTech 552")
    tier = tier_for(conn, sku)
    assert tier.tier == 2
    assert tier.identifiers[0].normalized == "SelectTech 552"


def test_a_model_nothing_observed_is_an_inference_and_stays_at_tier_one(tmp_path):
    """It was inferred rather than read, so there is nothing to cite for it."""
    from resell.reasoning.identity import tier_for

    conn, gateway, sku = identified(tmp_path)
    gateway.propose_identification(sku, brand="Bowflex", model="SelectTech 552")
    assert tier_for(conn, sku).tier == 1


# --- who finds the comparables ----------------------------------------------------


def priced_and_unclaimed(tmp_path):
    """An item that has reached pricing with nothing to price from."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    gateway.propose_identification(
        sku, category_id="137865", condition_id="USED_EXCELLENT",
        title="Bowflex SelectTech Dumbbells", description="A pair.",
        aspects={"Brand": ["Bowflex"]},
    )
    gateway.begin_pricing(sku)
    return conn, gateway, sku


def test_comp_research_is_the_agents_when_it_can_search(tmp_path, monkeypatch):
    """The operator should not have to go and find listings. This was made the
    operator's job to fix a stdin hang, and the hang was the reason -- not a view
    about whose work it is."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    step = next_step(conn, sku)
    assert step.step is Step.COMP_RESEARCH
    assert step.actor is Actor.AGENT


def test_comp_research_falls_back_to_the_operator_with_no_backend(tmp_path, monkeypatch):
    """Without one there is genuinely nothing else that can do it, and pretending
    otherwise would stall the item with an agent step that cannot run."""
    monkeypatch.delenv("RESELL_SEARCH_BACKEND", raising=False)
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    step = next_step(conn, sku)
    assert step.step is Step.COMP_RESEARCH
    assert step.actor is Actor.OPERATOR
    assert "paste" in step.detail


def test_the_agent_step_has_a_runner(tmp_path):
    """An agent step with no runner raises 'no runner for ...' mid-advance."""
    from resell.orchestrator import StageRunner

    assert hasattr(StageRunner, f"_{Step.COMP_RESEARCH}")


def test_reviewing_the_comparables_stays_the_operators(tmp_path, monkeypatch):
    """Discovering listings is work; deciding which are the same sort of thing is
    the judgement this design exists to keep with a person. Automating discovery
    must not quietly automate acceptance too."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    sp.record_comp_observation(conn, CompObservation(
        comp_id="c1", marketplace="ebay.com", external_id="1",
        price_kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
        price_cents=25000, observed_at=NOW, condition_band=ConditionBand.UNKNOWN,
    ))
    sp.record_comp_candidate(
        conn, sku=sku, comp_id="c1", proposed_comparability="same_family_variant",
        item_citations=("1",), comp_citations=("title",), rationale="same model",
    )
    step = next_step(conn, sku)
    assert step.step is Step.REVIEW_COMPS
    assert step.actor is Actor.OPERATOR


def test_a_search_budget_stop_is_a_message_not_a_crash(tmp_path, monkeypatch):
    """`advance` renders an exception as an error line, which reads as a fault.
    Running out of the lookups this item was allotted is the guard working."""
    from resell.orchestrator import StageRunner
    from resell.reasoning.budget import BudgetExceeded

    monkeypatch.setattr(
        "resell.reasoning.comp_loop.run_comp_round",
        lambda *a, **k: (_ for _ in ()).throw(BudgetExceeded("6 of 6 lookups used")),
    )
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    monkeypatch.setenv("BRAVE_API_KEY", "test")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    message = StageRunner()._comp_research(conn, gateway, sku)
    assert "budget" in message


def test_a_budget_stop_is_not_reported_as_a_failure(tmp_path, monkeypatch):
    """`advance` flagged "ran but the item still needs it" whenever a step left
    the item in place -- true of a crash and equally true of a step that stopped
    on its budget. The UI flashed both red, which teaches an operator that the red
    lines are noise; that is expensive the first time one of them is real."""
    from resell.orchestrator import RunReport, advance

    class BudgetedRunner:
        def run(self, conn, gateway, sku, step):
            return "stopped on this item's search budget: 3 of 3 calls made"

    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    report = advance(conn, gateway, sku, runner=BudgetedRunner(), max_steps=3)

    assert report.errors == []
    assert any("search budget" in h for h in report.halts)
    assert report.stopped_at.step is Step.COMP_RESEARCH


def test_a_step_that_genuinely_stalls_is_still_an_error(tmp_path, monkeypatch):
    """The guard must keep catching a mis-sequenced step that silently does
    nothing, which is what it was written for."""
    from resell.orchestrator import advance

    class UselessRunner:
        def run(self, conn, gateway, sku, step):
            return "did nothing at all"

    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    report = advance(conn, gateway, sku, runner=UselessRunner(), max_steps=3)

    assert report.halts == []
    assert any("still needs it" in e for e in report.errors)


# --- a spent budget ends the stage; it does not re-arm it -------------------------


def spend_comp_lookups(conn, sku, searches=6):
    """Burn the pricing search allowance the way real rounds do.

    This used to burn a `comp_plan` model budget, which is how the stage ran out
    when planning was a model call. Planning is deterministic now and `comp_plan`
    no longer exists, so counting it counted zero forever -- which is one of the
    two reasons MP-000061 could be told to "look again" four times.
    """
    for n in range(searches):
        conn.execute(
            "INSERT INTO research_lookup (sku, scope, provider, query, motivation, "
            "evidence_ids, result_count, performed_at) "
            "VALUES (?, 'pricing', 'brave', ?, 'comp research', '[]', 0, ?)",
            (sku, f"a spent search {n}", db.now_iso()),
        )
    conn.commit()


def concluded_completely(conn, sku, *, usable=0):
    """A round that closed the stage with its retrieval intact."""
    from resell.orchestrator import COMP_RESEARCH_CONCLUDED

    db.log_event(conn, COMP_RESEARCH_CONCLUDED, {
        "reason": "4 search(es) found nothing usable", "searches": 4,
        "candidates": 0, "claims": 24, "usable": usable, "sufficient": False,
        "retrieval_complete": True,
    }, item_id=sku)
    conn.commit()


def concluded_incompletely(conn, sku):
    """A round that closed without its searches ever running."""
    from resell.orchestrator import COMP_RESEARCH_CONCLUDED

    db.log_event(conn, COMP_RESEARCH_CONCLUDED, {
        "reason": "no search backend was configured", "searches": 0,
        "candidates": 0, "claims": 0, "usable": 0, "sufficient": False,
        "retrieval_complete": False,
    }, item_id=sku)
    conn.commit()


def test_an_exhausted_comp_budget_does_not_send_the_item_back_to_searching(tmp_path, monkeypatch):
    """MP-000011's deadlock. `next_step` returned comp_research because the item
    had no contributing comps; the runner refused because the budget was gone; the
    item did not move; the card offered "carry on", which re-entered the same step.
    Forever."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_lookups(conn, sku)

    first = next_step(conn, sku)
    assert first.step is Step.COMP_RESEARCH      # one last visit, to close it out
    assert first.actor is Actor.AGENT
    assert "spent" in first.detail


def test_carrying_on_from_an_exhausted_budget_advances(tmp_path, monkeypatch):
    """The regression proper: comp_research consumes all 3 planning calls, the
    operator carries on, and the workflow moves rather than searching again."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    monkeypatch.setenv("BRAVE_API_KEY", "test")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_lookups(conn, sku)

    searched = []
    monkeypatch.setattr(
        "resell.reasoning.comp_loop.run_comp_round",
        lambda *a, **k: searched.append(1),
    )

    report = advance(conn, gateway, sku, max_steps=4)

    assert searched == [], "a prohibited search was attempted"
    assert report.stopped_at.step is Step.PRICE_WITHOUT_COMPS
    assert report.stopped_at.actor is Actor.OPERATOR
    assert any("comp research finished" in line for line in report.ran)


def test_the_conclusion_is_recorded_explicitly(tmp_path, monkeypatch):
    """"Insufficient evidence" must be a recorded fact, not an absence that looks
    the same as never having tried."""
    from resell.orchestrator import COMP_RESEARCH_CONCLUDED, comp_research_concluded

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_lookups(conn, sku)
    advance(conn, gateway, sku, max_steps=4)

    assert comp_research_concluded(conn, sku)
    row = conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND kind = ?",
        (sku, COMP_RESEARCH_CONCLUDED),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["sufficient"] is False
    assert payload["claims"] == 0
    assert "spent" in payload["reason"]


def test_the_stage_is_concluded_once_however_often_it_is_visited(tmp_path, monkeypatch):
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_lookups(conn, sku)
    for _ in range(3):
        advance(conn, gateway, sku, max_steps=4)

    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE item_id = ? AND kind = 'comp_research_concluded'",
        (sku,),
    ).fetchone()[0]
    assert n == 1


def test_an_item_with_budget_left_still_searches(tmp_path, monkeypatch):
    """The guard must not turn into a reason never to research anything."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_lookups(conn, sku, searches=1)

    step = next_step(conn, sku)
    assert step.step is Step.COMP_RESEARCH
    assert step.actor is Actor.AGENT
    assert "searching the marketplaces" in step.detail


def test_comps_that_did_arrive_still_take_precedence(tmp_path, monkeypatch):
    """An exhausted budget with usable comparables is not the blocked case: the
    item prices normally."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_lookups(conn, sku)
    sp.record_comp_observation(conn, CompObservation(
        comp_id="c1", marketplace="ebay.com", external_id="1",
        price_kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
        price_cents=25000, observed_at=NOW, condition_band=ConditionBand.UNKNOWN,
    ))
    sp.record_comp_claim(conn, CompClaim(
        claim_id="cl1", sku=sku, comp_id="c1",
        comparability=Comparability.SAME_FAMILY_VARIANT,
        item_citations=("1",), comp_citations=("title",),
    ), identity_resolution="searched_not_found")

    step = next_step(conn, sku)
    assert step.step is not Step.PRICE_WITHOUT_COMPS
    assert step.step is not Step.COMP_RESEARCH


def test_a_grant_lets_research_resume_when_it_never_actually_searched(tmp_path, monkeypatch):
    """The conclusion records what happened; it must not become a permanent ban.
    Reopening is the grant, because that is the thing with a button behind it --
    and this is the case it is for: the searches never ran."""
    from resell.orchestrator import grant_more_research

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    concluded_incompletely(conn, sku)
    assert next_step(conn, sku).step is Step.PRICE_WITHOUT_COMPS

    grant_more_research(conn, sku)
    resumed = next_step(conn, sku)
    assert resumed.step is Step.COMP_RESEARCH
    assert resumed.actor is Actor.AGENT
    assert "searching the marketplaces" in resumed.detail


def test_a_grant_after_a_complete_round_cannot_search_again(tmp_path, monkeypatch):
    """MP-000061 was granted three times. Pricing asks a fixed list of queries, so
    every extra round asked the same four questions of the same index and logged
    twenty-four lines of "already recorded" -- twelve Brave calls that could not
    have changed anything."""
    from resell.orchestrator import comp_research_exhausted, grant_more_research

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    concluded_completely(conn, sku)

    grant_more_research(conn, sku)
    spent, why = comp_research_exhausted(conn, sku)
    assert spent
    assert "already run" in why
    # The routing still pays one visit to close the stage out again -- what it must
    # not do is search, and the card says so rather than promising a hunt.
    step = next_step(conn, sku)
    assert "spent" in step.detail
    assert "searching the marketplaces" not in step.detail


def test_the_two_kinds_of_empty_are_told_apart(tmp_path, monkeypatch):
    """"We looked and there is nothing" closes the stage; "we could not look" must
    not. Only the first makes looking again pointless."""
    from resell.orchestrator import comp_research_exhausted

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    concluded_incompletely(conn, sku)
    assert comp_research_exhausted(conn, sku)[0] is False

    conn2, gateway2, sku2 = priced_and_unclaimed(tmp_path / "b")
    concluded_completely(conn2, sku2)
    assert comp_research_exhausted(conn2, sku2)[0] is True


def test_raising_the_env_budget_does_not_reopen_a_concluded_item(tmp_path, monkeypatch):
    """Two mechanisms that both reopen is how the loop came back: a grant would
    reopen, the round would find nothing, the conclusion would land, and a
    process-wide budget still showing headroom would reopen it again. The
    environment sets the base allowance; the grant reopens one item."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_lookups(conn, sku)
    advance(conn, gateway, sku, max_steps=4)

    monkeypatch.setenv("RESELL_BUDGET_COMP_RESEARCH_MAX_CALLS", "10")
    assert next_step(conn, sku).step is Step.PRICE_WITHOUT_COMPS


def test_a_granted_attempt_that_finds_nothing_concludes_again(tmp_path, monkeypatch):
    """The reported loop. "Let it look again" bought an attempt, the attempt found
    nothing, and the item returned to a card offering another attempt -- which is
    the same loop with a purchase in the middle."""
    from resell.orchestrator import grant_more_research

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    monkeypatch.setenv("BRAVE_API_KEY", "test")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    # Incomplete, so the grant genuinely buys an attempt. A grant after a *complete*
    # round buys nothing and is refused before it runs -- that is
    # `test_a_grant_after_a_complete_round_cannot_search_again`.
    concluded_incompletely(conn, sku)
    grant_more_research(conn, sku)
    assert next_step(conn, sku).step is Step.COMP_RESEARCH

    # the granted round runs and turns up nothing reviewable
    monkeypatch.setattr(
        "resell.reasoning.comp_loop.run_comp_round",
        lambda *a, **k: _EmptyRound(),
    )
    report = advance(conn, gateway, sku, max_steps=4)

    assert report.stopped_at.step is Step.PRICE_WITHOUT_COMPS
    assert any("found nothing usable" in line for line in report.ran)


class _EmptyRound:
    """A round that completed and produced nothing to review.

    `judging_complete` is the difference between this and a round that broke:
    every listing it retrieved got a verdict, and the verdict was that none of
    them help. That is a statement about the market, and it is the only thing
    that may route to "decide a price without comparables"."""

    performed = ["a query"]
    comps_recorded = 0
    stopped = None
    stop_reason = ""
    notes: list = []
    unjudged: list = []
    incomplete_reason = ""
    judging_complete = True


# --- the two exits that actually work ---------------------------------------------


def test_a_grant_reopens_research_for_one_item_only(tmp_path, monkeypatch):
    """The env vars are process-wide and need a restart, which turns "this one is
    worth another look" into an operations task -- and a global raise silently
    applies to every item after it, which is how a budget stops being one."""
    from resell.orchestrator import grant_more_research

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    other = gateway.ingest_item(purchase_cost_cents=500).sku
    spend_comp_lookups(conn, sku)
    spend_comp_lookups(conn, other)

    grant_more_research(conn, sku)

    assert not comp_research_exhausted(conn, sku)[0]
    assert comp_research_exhausted(conn, other)[0], "the grant leaked to another item"


def test_grants_accumulate(tmp_path):
    from resell.orchestrator import granted_research, grant_more_research

    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    grant_more_research(conn, sku)
    grant_more_research(conn, sku)
    calls, lookups = granted_research(conn, sku)
    assert (calls, lookups) == (6, 6)


def test_the_guard_and_the_predicate_read_the_same_budget(tmp_path):
    """They disagreed once and it deadlocked the UI. Both now come from
    `comp_budgets_for`, so a grant that reopens the step also lets it spend."""
    from resell.orchestrator import comp_budgets_for, grant_more_research

    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    before, _ = comp_budgets_for(conn, sku)
    grant_more_research(conn, sku, calls=3)
    after, _ = comp_budgets_for(conn, sku)
    assert after.max_calls == before.max_calls + 3
    assert after.max_cost_micros > before.max_cost_micros


def test_an_operator_price_is_a_real_approval(tmp_path):
    """`propose_listing` refuses a price with no matching approval, so a number
    typed into a box has to become a proposal or it can never reach a listing.
    Routing around that would put an unapproved price on eBay."""
    import uuid
    from datetime import UTC, datetime

    from resell.pricing.estimate import PriceQualifier
    from resell.pricing.lifecycle import PriceProposal, PriceReason

    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    proposal = PriceProposal(
        proposal_id=f"price_{uuid.uuid4().hex[:12]}", sku=sku,
        reason=PriceReason("initial"), price_cents=45000,
        created_at=datetime.now(UTC),
        qualifiers=(PriceQualifier.OPERATOR_JUDGEMENT,),
        floor_ok=True, rationale="operator set it",
    )
    sp.record_proposal(conn, proposal)
    sp.approve_proposal(conn, proposal)

    assert sp.approved_price_cents(conn, sku) == 45000


def test_an_operator_price_carries_no_borrowed_evidence(tmp_path):
    """The fields a recommendation would fill stay empty and the qualifier says
    why. An empty band and a band nobody computed look identical afterwards."""
    import uuid
    from datetime import UTC, datetime

    from resell.pricing.estimate import PriceQualifier
    from resell.pricing.lifecycle import PriceProposal, PriceReason

    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    proposal = PriceProposal(
        proposal_id=f"price_{uuid.uuid4().hex[:12]}", sku=sku,
        reason=PriceReason("initial"), price_cents=45000,
        created_at=datetime.now(UTC),
        qualifiers=(PriceQualifier.OPERATOR_JUDGEMENT,),
        floor_ok=True, rationale="operator set it",
    )
    sp.record_proposal(conn, proposal)
    stored = sp.load_proposal(conn, proposal.proposal_id)

    assert PriceQualifier.OPERATOR_JUDGEMENT in stored.qualifiers
    assert stored.comp_set_id is None
    assert stored.band_central_cents is None
    assert stored.basis is None


def test_a_priced_item_leaves_the_blocked_step(tmp_path, monkeypatch):
    """The point of the whole exercise: the item moves."""
    import uuid
    from datetime import UTC, datetime

    from resell.pricing.estimate import PriceQualifier
    from resell.pricing.lifecycle import PriceProposal, PriceReason

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_lookups(conn, sku)
    advance(conn, gateway, sku, max_steps=4)
    assert next_step(conn, sku).step is Step.PRICE_WITHOUT_COMPS

    proposal = PriceProposal(
        proposal_id=f"price_{uuid.uuid4().hex[:12]}", sku=sku,
        reason=PriceReason("initial"), price_cents=45000,
        created_at=datetime.now(UTC),
        qualifiers=(PriceQualifier.OPERATOR_JUDGEMENT,),
        floor_ok=True, rationale="operator set it",
    )
    sp.record_proposal(conn, proposal)
    sp.approve_proposal(conn, proposal)

    moved = next_step(conn, sku)
    assert moved.step is not Step.PRICE_WITHOUT_COMPS
    assert moved.step is not Step.COMP_RESEARCH


# --- the run reaches a decision without asking to be nudged --------------------


def test_the_bound_clears_the_whole_agent_chain(tmp_path):
    """MP-000062 ran eight steps, stopped one short of the comparables, and asked
    its owner to press "Carry on" for a stop that meant nothing.

    The chain is what it is; the bound has to clear it. Asserted against the real
    step sequence rather than a number, so adding a stage fails here instead of
    surfacing as an unexplained button."""
    from resell.orchestrator import MAX_AGENT_STEPS

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    runner = FakeRunner(gateway)
    report = advance(conn, gateway, sku, runner=runner)

    assert not report.exhausted, (
        f"the run hit its {MAX_AGENT_STEPS}-step bound after {len(runner.ran)} "
        f"steps: {[str(s) for s in runner.ran]}"
    )
    assert next_step(conn, sku).actor is Actor.OPERATOR, (
        "a run should stop because a person is needed, not because it ran out of "
        "permission to keep going"
    )


def test_the_bound_still_bounds(tmp_path):
    """It is a spin guard, so it must still stop something that never settles."""
    class Spinner:
        ran = []

        def run(self, conn, gateway, sku, step):
            return "did nothing at all"

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    report = advance(conn, gateway, sku, runner=Spinner(), max_steps=3)
    assert report.errors or report.exhausted


def test_reaching_the_bound_is_recorded_rather_than_silent(tmp_path):
    """Nothing anywhere said the bound had been reached, so it arrived at the
    operator as a button with no reason behind it."""
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    # Every step progresses; there are simply more of them than the bound allows.
    report = advance(conn, gateway, sku, runner=FakeRunner(gateway), max_steps=2)
    assert report.exhausted is True
    assert report.errors == [], "the bound is not a fault of the item's"


# --- a rejected sample is not a verdict ---------------------------------------


def test_a_stage_that_produces_nothing_usable_is_tried_again(tmp_path):
    """MP-000063's aspect mapping proposed `Material: Wood` for a crocheted wool
    ball. The citation gate correctly discarded it -- and every other candidate
    with it -- so the item was exactly where it started and the run died. Its
    owner pressed Retry, the same call proposed `Wool`, and it went through.

    The provider answered and nothing raised; what failed was validation, and the
    next sample is drawn fresh. That retry was ours to make."""
    class OnceUseless:
        """Produces nothing the first time, then works -- like a resampled call."""

        def __init__(self, gateway):
            self.gateway, self.calls, self.ran = gateway, 0, []

        def run(self, conn, gateway, sku, step):
            self.ran.append(step)
            if step is Step.START_IDENTIFICATION:
                gateway.begin_identification(sku)
                return "started"
            if step is Step.OBSERVE:
                self.calls += 1
                if self.calls == 1:
                    return "0 observation(s)"      # ran clean, nothing survived
                with_observation(conn, sku)
                return "1 observation(s)"
            if step is Step.SUGGEST_CATEGORY:
                gateway.propose_identification(sku, category_id="111694")
                return "category 111694"
            return "done"

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    runner = OnceUseless(gateway)
    report = advance(conn, gateway, sku, runner=runner, max_steps=3)

    assert runner.calls == 2, "the useless sample should have been drawn again"
    assert report.errors == [], f"the retry fixed it: {report.errors}"
    assert any("nothing it produced survived validation" in r for r in report.retried)


def test_a_stage_useless_twice_still_stops_the_run(tmp_path):
    """The anti-spin guard survives: a retry is one more sample, not a licence to
    keep drawing."""
    class AlwaysUseless:
        def __init__(self, gateway):
            self.gateway, self.calls = gateway, 0

        def run(self, conn, gateway, sku, step):
            if step is Step.START_IDENTIFICATION:
                gateway.begin_identification(sku)
                return "started"
            self.calls += 1
            return "0 observation(s)"

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    runner = AlwaysUseless(gateway)
    report = advance(conn, gateway, sku, runner=runner, max_steps=6)

    assert runner.calls == 2, "twice, not forever"
    assert any("still needs it" in e for e in report.errors)


def test_an_ordinary_halt_is_not_retried(tmp_path):
    """A budget reached or a backend absent is a guard, not a bad sample. Drawing
    again would meet the same guard and spend money to find out."""
    from resell.orchestrator import STOPPED_MARKER

    class Halting:
        def __init__(self, gateway):
            self.gateway, self.calls = gateway, 0

        def run(self, conn, gateway, sku, step):
            if step is Step.START_IDENTIFICATION:
                gateway.begin_identification(sku)
                return "started"
            self.calls += 1
            return f"{STOPPED_MARKER} this item's search budget"

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    runner = Halting(gateway)
    report = advance(conn, gateway, sku, runner=runner, max_steps=6)

    assert runner.calls == 1, "a guard is an answer; asking again costs money"
    assert report.halts and not report.errors
