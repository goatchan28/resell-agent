"""A badly-encoded tool call should cost what it broke, not the whole round.

MP-000005's planning stage produced a good plan twice and neither reached a lookup.
The arguments came back with the first key parsed as structure and the entire
remainder of the document as its string value:

    {"assessment": "{\\"proposed_mode\\": ...}, \\"lookups\\": [...]}"}

`assessment` was a `str` where a dict belonged, so nothing was read; the resulting
emptiness was then reported as the planner deciding research was unnecessary, and a
`research_negative` was written into evidence saying so. Two separate faults: an
unrecovered encoding, and a parse failure misfiled as a finding.
"""

from __future__ import annotations

import json

from resell import db
from resell.domain import FeeModel
from resell.gateway import Gateway
from resell.reasoning.tools import (
    parse_draft_tool_input,
    parse_extract_tool_input,
    parse_map_tool_input,
    parse_match_tool_input,
    parse_plan_tool_input,
    unwrap_tool_input,
)

# The real arguments, in the shape the API actually returned them.
GOOD_PLAN = {
    "assessment": {
        "sufficient": False,
        "proposed_mode": "exact_product",
        "rationale": "A3211 is legible; confirm it against the maker",
    },
    "lookups": [
        {"query": "Beats Pill A3211 specifications", "source_kind": "manufacturer",
         "motivation": "confirm the model number", "evidence_ids": [96, 102]},
        {"query": "FCC ID BCGA3211", "source_kind": "reference",
         "motivation": "cross-check the identity", "evidence_ids": [96, 103]},
    ],
}


def split_at_first_key(payload: dict) -> dict:
    """Re-create the failure exactly: parse the first key, stringify the rest."""
    document = json.dumps(payload)
    first = next(iter(payload))
    prefix = f'{{{json.dumps(first)}: '
    assert document.startswith(prefix)
    return {first: document[len(prefix):]}


IDS = set(range(88, 110))


def plan_from(payload):
    return parse_plan_tool_input(payload, valid_evidence_ids=IDS, already_searched=set())


# --- the shape that actually occurred --------------------------------------------


def test_the_split_encoding_is_reconstructed():
    plan = plan_from(split_at_first_key(GOOD_PLAN))
    assert plan.usable
    assert plan.assessment_read
    assert plan.proposed_mode == "exact_product"
    assert [lookup.query for lookup in plan.lookups] == [
        "Beats Pill A3211 specifications", "FCC ID BCGA3211"
    ]


def test_the_string_alone_does_not_parse():
    """Why the first recovery attempt was not enough: the value is the remainder of
    a document, not a serialised object."""
    broken = split_at_first_key(GOOD_PLAN)
    try:
        json.loads(broken["assessment"])
    except ValueError:
        return
    raise AssertionError("expected the bare string to be unparseable")


def test_the_reconstruction_is_reported_not_silent():
    """It is a provider-shaped defect worth seeing in the trace, not something to
    paper over."""
    plan = plan_from(split_at_first_key(GOOD_PLAN))
    assert any("reconstructed it" in note for note in plan.malformed)


def test_citations_still_face_the_same_validation():
    """Recovery restores the arguments; it does not exempt them. A lookup citing
    nothing real is still dropped."""
    payload = json.loads(json.dumps(GOOD_PLAN))
    payload["lookups"][0]["evidence_ids"] = [9999]
    plan = plan_from(split_at_first_key(payload))
    assert [lookup.query for lookup in plan.lookups] == ["FCC ID BCGA3211"]
    assert any("9999" in note for note in plan.malformed)


# --- the plainer variant, and the limits ------------------------------------------


def test_a_wholly_serialised_argument_object_is_recovered():
    plan = plan_from({"assessment": json.dumps(GOOD_PLAN)})
    assert plan.usable
    assert len(plan.lookups) == 2


def test_a_well_formed_payload_is_untouched():
    plan = plan_from(GOOD_PLAN)
    assert plan.usable
    assert plan.malformed == []


def test_an_assessment_serialised_on_its_own_is_recovered():
    payload = {"assessment": json.dumps(GOOD_PLAN["assessment"]),
               "lookups": GOOD_PLAN["lookups"]}
    plan = plan_from(payload)
    assert plan.assessment_read
    assert plan.proposed_mode == "exact_product"
    assert len(plan.lookups) == 2


def test_a_string_field_carrying_incidental_json_is_left_alone():
    """Recovery only fires when the parse yields one of the keys being looked for,
    so an ordinary string that happens to contain JSON is not mistaken for the
    arguments."""
    payload = {
        "assessment": {"sufficient": True, "proposed_mode": "described_object",
                       "rationale": 'the label reads {"batch": 4}'},
        "lookups": [],
    }
    plan = plan_from(payload)
    assert plan.sufficient
    assert plan.malformed == []


def test_an_unparseable_string_payload_is_still_reported():
    plan = plan_from("not json at all")
    assert not plan.usable
    assert "unparseable string" in plan.malformed[0]


def test_a_genuinely_empty_response_is_not_invented_into_a_plan():
    plan = plan_from({})
    assert not plan.usable
    assert plan.lookups == []


# --- the same recovery on the other stages -----------------------------------------


def test_the_matcher_recovers_the_same_encoding():
    payload = {"assessment": {"any_match": True, "rationale": "the code matches"},
               "claims": [{"candidate_ref": "cand-1", "is_match": True,
                           "strength": "identifier_asserted", "rationale": "A3211 on both",
                           "item_evidence": [96], "candidate_evidence": [200]}]}
    proposal = parse_match_tool_input(
        split_at_first_key(payload),
        valid_item_evidence={96}, valid_candidate_evidence={200},
    )
    assert len(proposal.claims) == 1
    assert proposal.claims[0].candidate_ref == "cand-1"


def test_the_mapper_recovers_the_same_encoding():
    payload = {"aspects": [{"aspect_name": "Brand", "candidates": [
        {"value": "Beats by Dr. Dre", "evidence_ids": [88]}]}]}
    proposal = parse_map_tool_input(
        split_at_first_key(payload), valid_evidence_ids={88}
    )
    assert "Brand" in proposal.candidates_by_aspect


def test_the_drafter_recovers_the_same_encoding():
    payload = {"title": "Beats Pill Red", "description": "A speaker.",
               "claims": [{"text": "Red", "evidence_ids": [89]}]}
    draft = parse_draft_tool_input(
        split_at_first_key(payload), valid_evidence_ids={89}
    )
    assert draft.title == "Beats Pill Red"


def test_the_extractor_recovers_the_same_encoding():
    page = "Colourway: Statement Red. Model number A3211."
    payload = {"product_title": "Beats Pill", "facts": [
        {"claim": "Colourway: Statement Red", "domain": "identity",
         "excerpt": "Colourway: Statement Red"}]}
    extracted = parse_extract_tool_input(split_at_first_key(payload), page_text=page)
    assert len(extracted.facts) == 1


def test_unwrap_leaves_an_unrelated_dict_alone():
    notes: list[str] = []
    payload = {"something": "else"}
    assert unwrap_tool_input(payload, {"aspects"}, notes) is payload
    assert notes == []


# --- a parse failure is not a finding ---------------------------------------------


def fixture(tmp_path):
    conn = db.connect(tmp_path / "plan.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    from resell.reasoning.schema import Basis, Observation

    gateway.record_observation(
        sku, Observation(claim="panel reads A3211", basis=Basis.TEXT_READ,
                         photo_positions=(1,)),
    )
    return conn, gateway, sku


class Planner:
    provider, model = "fake", "m"

    def __init__(self, tool_input):
        self._tool_input = tool_input

    def estimate_input_tokens(self, request):
        return 1000

    def rates(self):
        from resell.reasoning.budget import ModelRates

        return ModelRates()

    def run(self, request):
        from resell.reasoning.stages import StageResult, Usage

        return StageResult(
            tool_input=self._tool_input, usage=Usage(1000, 200, {}), latency_ms=5,
            provider="fake", model="m", stop_reason="tool_use", raw_response={},
        )


def round_with(conn, gateway, sku, tool_input):
    from resell.reasoning.budget import LookupBudget, StageBudget
    from resell.reasoning.research_loop import run_round

    class NoRetrieval:
        provider = "none"

        def search(self, query):
            return []

        def cost_micros_per_lookup(self):
            return 0

    return run_round(
        conn, gateway, sku, model_adapter=Planner(tool_input),
        research_adapter=NoRetrieval(),
        stage_budget=StageBudget(max_calls=9, max_cost_micros=9_000_000),
        lookup_budget=LookupBudget(max_lookups=4),
    )


def test_an_unusable_plan_records_no_negative_finding(tmp_path):
    """A research_negative is the audit trail for choosing not to search. Filing a
    parse failure there puts a claim in the evidence log that nobody made -- and
    evidence is append-only, so it stays for the life of the item while
    `identity_resolution` derives the opposite answer from `research_lookup`."""
    conn, gateway, sku = fixture(tmp_path)
    outcome = round_with(conn, gateway, sku, {"assessment": "}}not json{{"})
    assert outcome.stopped == "plan_unusable"
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ? AND kind = 'research_negative'",
        (sku,),
    ).fetchone()[0] == 0


def test_an_unusable_plan_declares_no_mode(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    outcome = round_with(conn, gateway, sku, {"assessment": "}}not json{{"})
    assert outcome.mode is None


def test_an_unusable_plan_says_so_rather_than_claiming_sufficiency(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    outcome = round_with(conn, gateway, sku, {"assessment": "}}not json{{"})
    assert outcome.stopped != "sufficient"
    assert "could not be read" in outcome.stop_reason


def test_a_genuine_sufficiency_decision_still_records_a_negative(tmp_path):
    """The distinction has to cut both ways, or the fix has just moved the bug."""
    conn, gateway, sku = fixture(tmp_path)
    outcome = round_with(conn, gateway, sku, {
        "assessment": {"sufficient": True, "proposed_mode": "described_object",
                       "rationale": "no discoverable brand, thorough examination"},
        "lookups": [],
    })
    assert outcome.stopped == "sufficient"
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ? AND kind = 'research_negative'",
        (sku,),
    ).fetchone()[0] == 1


def test_the_recovered_plan_reaches_retrieval(tmp_path):
    """The end of it: the arguments that were being thrown away now produce lookups."""
    conn, gateway, sku = fixture(tmp_path)
    payload = json.loads(json.dumps(GOOD_PLAN))
    observation = conn.execute(
        "SELECT id FROM evidence WHERE sku = ? ORDER BY id", (sku,)
    ).fetchone()[0]
    for lookup in payload["lookups"]:
        lookup["evidence_ids"] = [observation]

    outcome = round_with(conn, gateway, sku, split_at_first_key(payload))
    assert outcome.stopped is None or outcome.stopped == "searched_not_found"
    assert outcome.plan is not None
    assert len(outcome.plan.lookups) == 2


# --- the XML variant: arguments run together inside one string --------------------


DRAFT_RUN_TOGETHER = {
    "title": "Bowflex SelectTech Adjustable Dumbbells, Pair with Cradle Stands",
    "description": (
        "Pair of Bowflex adjustable dumbbells with a rotating selector dial."
        "</parameter>\n"
        '<parameter name="marketing_copy">Swap an entire rack for one compact pair.'
        "</parameter>\n"
        '<parameter name="claims">'
        '[{"text": "the dial reads 45 at its highest", "evidence_ids": [198]}]'
    ),
}


def test_run_together_parameters_are_split_back_out():
    """MP-000009: `title` arrived clean and `description` swallowed the rest."""
    malformed = []
    out = unwrap_tool_input(
        DRAFT_RUN_TOGETHER, {"title", "description", "claims"}, malformed
    )
    assert out["description"].endswith("selector dial.")
    assert out["marketing_copy"] == "Swap an entire rack for one compact pair."
    assert out["claims"] == [
        {"text": "the dial reads 45 at its highest", "evidence_ids": [198]}
    ]
    assert "split them back out" in malformed[0]


def test_the_markup_never_survives_into_the_description():
    """It reached a stored draft once. A buyer would have read it."""
    out = unwrap_tool_input(
        DRAFT_RUN_TOGETHER, {"title", "description", "claims"}, []
    )
    assert "<parameter" not in out["description"]
    assert "</parameter>" not in out["description"]


def test_the_citations_survive_rather_than_vanishing():
    """The worse half of that failure: `claims` was empty, so a draft whose whole
    premise is that every assertion cites evidence was stored citing nothing, and
    the review that checks citations had nothing to object to."""
    from resell.reasoning.tools import parse_draft_tool_input

    draft = parse_draft_tool_input(DRAFT_RUN_TOGETHER, valid_evidence_ids={198})
    assert [c.evidence_ids for c in draft.claims] == [(198,)]


def test_a_closing_tag_named_after_the_key_is_accepted_too():
    """Observed both ways on consecutive calls: `</parameter>` and `</description>`."""
    payload = {
        "title": "A title",
        "description": (
            "Body text.</description>\n"
            '<parameter name="marketing_copy">Copy.'
        ),
    }
    out = unwrap_tool_input(payload, {"title", "description", "claims"}, [])
    assert out["description"] == "Body text."
    assert out["marketing_copy"] == "Copy."


def test_ordinary_prose_is_left_exactly_as_written():
    payload = {"title": "A title", "description": "A description mentioning no markup."}
    assert unwrap_tool_input(payload, {"title", "description"}, []) == payload


def test_an_unparseable_recovered_array_is_kept_as_text_not_dropped():
    """Losing it silently is how the original fault stayed invisible."""
    payload = {
        "title": "A title",
        "description": 'Body.</parameter>\n<parameter name="claims">[{"text": broken',
    }
    out = unwrap_tool_input(payload, {"title", "description", "claims"}, [])
    assert out["claims"].startswith('[{"text"')
