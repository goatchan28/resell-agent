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


def with_observation(conn, sku):
    conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
        "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
        (sku, "vision_observation", "fake/m", json.dumps({"claim": "a red speaker"}),
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
        Step.MAP_ASPECTS, Step.GRADE_CONDITION, Step.DRAFT,
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


def test_a_resolved_identity_is_not_rubber_stamped(tmp_path):
    """Stopping on an identification the agent can defend would be asking the
    operator to approve work that already carries its own evidence."""
    from resell.orchestrator import identity_needs_confirming

    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    needs, why = identity_needs_confirming(conn, sku)
    assert needs and "could not pin down" in why

    conn.execute(
        "INSERT INTO product_match (sku, candidate_ref, is_match, strength, "
        "source_authority, donation_scope, created_at, rationale, "
        "item_evidence, candidate_evidence) VALUES (?,?,1,?,?,?,?,?,?,?)",
        (sku, "cand-1", "identifier_verified", "manufacturer", "attributes",
         db.now_iso(), "matched", "[1]", "[2]"),
    )
    conn.commit()
    needs, why = identity_needs_confirming(conn, sku)
    assert not needs
    assert "resolved this to a specific product" in why


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
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)

    class Broken:
        def run(self, *a):
            raise RuntimeError("the model refused")

    report = advance(conn, gateway, sku, runner=Broken())
    assert report.errors == ["start_identification: the model refused"]
    assert not report.progressed


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


def test_research_is_skipped_when_there_is_nothing_to_search_for(tmp_path):
    """A brand alone returns the catalogue, not this object."""
    from resell.orchestrator import research_warranted

    conn, gateway, sku = identified(tmp_path)
    warranted, why = research_warranted(conn, sku)
    assert not warranted
    assert "nothing distinctive enough" in why


def test_a_serial_number_is_not_something_to_search_for(tmp_path):
    """It identifies one physical unit. A lookup on it cannot succeed."""
    from resell.orchestrator import identifying_evidence

    conn, gateway, sku = identified(tmp_path)
    with_identifier(conn, sku, scheme="serial", value="FK4HQR32390")
    assert identifying_evidence(conn, sku) == ()


def test_a_brand_and_model_pair_is_enough_without_any_code(tmp_path):
    from resell.orchestrator import identifying_evidence

    conn, gateway, sku = identified(tmp_path)
    gateway.propose_identification(sku, brand="Bowflex", model="SelectTech 552")
    assert any("Bowflex SelectTech 552" in e for e in identifying_evidence(conn, sku))


def test_research_is_skipped_once_identity_is_resolved(tmp_path):
    """An exact catalogue match leaves nothing to look up."""
    from resell.orchestrator import research_warranted
    from resell.pricing.comps import Comparability

    conn, gateway, sku = identified(tmp_path)
    with_identifier(conn, sku)
    conn.execute(
        "INSERT INTO product_match (sku, candidate_ref, strength, source_authority, "
        "rationale, item_evidence, candidate_evidence, is_match, donation_scope, "
        "created_at) VALUES (?,?,?,?,?,?,?,1,?,?)",
        (sku, "cand-1", "identifier_verified", "manufacturer", "matched",
         "[]", "[]", "attributes", db.now_iso()),
    )
    conn.commit()
    warranted, why = research_warranted(conn, sku)
    assert not warranted
    assert "already resolved" in why


def test_research_is_not_repeated_against_an_unchanged_record(tmp_path):
    """Re-planning produces the same plan and charges for it again."""
    from resell.orchestrator import research_warranted

    conn, gateway, sku = identified(tmp_path)
    with_identifier(conn, sku)
    conn.execute(
        "INSERT INTO model_call (sku, purpose, provider, model, called_at, status, "
        "cost_micros) VALUES (?,?,?,?,?,?,?)",
        (sku, "research_plan", "anthropic", "m", db.now_iso(), "completed", 1000),
    )
    conn.commit()
    warranted, why = research_warranted(conn, sku)
    assert not warranted
    assert "already researched once" in why


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
    conn, gateway, sku = identified(tmp_path)
    with_identifier(conn, sku)
    assert next_step(conn, sku).actor is Actor.AGENT
    from resell.orchestrator import StageRunner

    assert hasattr(StageRunner, "_research_identity")


def test_an_item_with_no_identifier_goes_straight_on_to_mapping(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with_photo(conn, gateway, sku)
    gateway.begin_identification(sku)
    with_observation(conn, sku)
    gateway.propose_identification(sku, category_id="111694", title="A speaker")
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


def test_the_columns_make_research_warranted_on_a_branded_line(tmp_path):
    """The end of it: an item like MP-000009 becomes researchable."""
    from resell.orchestrator import identifying_evidence, research_warranted

    conn, gateway, sku = identified(tmp_path)
    gateway.propose_identification(sku, brand="Bowflex", model="SelectTech 552")
    assert any("Bowflex SelectTech 552" in e for e in identifying_evidence(conn, sku))
    warranted, _ = research_warranted(conn, sku)
    assert warranted


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


def spend_comp_plan(conn, sku, calls=3):
    """Burn the planning budget the way three real rounds did."""
    for _ in range(calls):
        conn.execute(
            "INSERT INTO model_call (sku, purpose, provider, model, status, "
            "called_at, estimated_cost_micros) VALUES (?,?,?,?,?,?,?)",
            (sku, "comp_plan", "fake", "m", "completed", db.now_iso(), 1000),
        )
    conn.commit()


def test_an_exhausted_comp_budget_does_not_send_the_item_back_to_searching(tmp_path, monkeypatch):
    """MP-000011's deadlock. `next_step` returned comp_research because the item
    had no contributing comps; the runner refused because the budget was gone; the
    item did not move; the card offered "carry on", which re-entered the same step.
    Forever."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_plan(conn, sku)

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
    spend_comp_plan(conn, sku)

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
    spend_comp_plan(conn, sku)
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
    spend_comp_plan(conn, sku)
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
    spend_comp_plan(conn, sku, calls=1)

    step = next_step(conn, sku)
    assert step.step is Step.COMP_RESEARCH
    assert step.actor is Actor.AGENT
    assert "searching the marketplaces" in step.detail


def test_comps_that_did_arrive_still_take_precedence(tmp_path, monkeypatch):
    """An exhausted budget with usable comparables is not the blocked case: the
    item prices normally."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_plan(conn, sku)
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


def test_a_grant_lets_research_resume(tmp_path, monkeypatch):
    """The conclusion records what happened; it must not become a permanent ban.
    Reopening is the grant, because that is the thing with a button behind it."""
    from resell.orchestrator import grant_more_research

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_plan(conn, sku)
    advance(conn, gateway, sku, max_steps=4)
    assert next_step(conn, sku).step is Step.PRICE_WITHOUT_COMPS

    grant_more_research(conn, sku)
    resumed = next_step(conn, sku)
    assert resumed.step is Step.COMP_RESEARCH
    assert resumed.actor is Actor.AGENT
    assert "searching the marketplaces" in resumed.detail


def test_raising_the_env_budget_does_not_reopen_a_concluded_item(tmp_path, monkeypatch):
    """Two mechanisms that both reopen is how the loop came back: a grant would
    reopen, the round would find nothing, the conclusion would land, and a
    process-wide budget still showing headroom would reopen it again. The
    environment sets the base allowance; the grant reopens one item."""
    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    spend_comp_plan(conn, sku)
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
    spend_comp_plan(conn, sku)
    advance(conn, gateway, sku, max_steps=4)
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
    """A round that completed and produced nothing to review."""

    performed = ["a query"]
    comps_recorded = 0
    stopped = None
    stop_reason = ""
    notes: list = []


# --- the two exits that actually work ---------------------------------------------


def test_a_grant_reopens_research_for_one_item_only(tmp_path, monkeypatch):
    """The env vars are process-wide and need a restart, which turns "this one is
    worth another look" into an operations task -- and a global raise silently
    applies to every item after it, which is how a budget stops being one."""
    from resell.orchestrator import grant_more_research

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    conn, gateway, sku = priced_and_unclaimed(tmp_path)
    other = gateway.ingest_item(purchase_cost_cents=500).sku
    spend_comp_plan(conn, sku)
    spend_comp_plan(conn, other)

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
    spend_comp_plan(conn, sku)
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
