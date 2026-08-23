"""Repairing a refused draft instead of throwing it away.

The reviewer refuses a whole draft over one phrase. MP-000009's copy was accurate
apart from a title reading "5-45 lb" -- a range the record does not contain, since
only the 45 lb maximum was ever observed. Discarding the rest and regenerating
loses good copy and tends to reintroduce the same invention somewhere else.

So the refusal is fed back verbatim, once, and what was not challenged is kept.
The part that does not soften is the reviewer: it runs again, unchanged, on the
result. A repair that invents something new is refused exactly as the first draft
was. `preserved_ratio` exists to make the difference between a repair and a
regeneration visible, because both produce a passing draft and only one of them
did what was asked.
"""

from __future__ import annotations

import json

import pytest

from resell import db
from resell.domain import FeeModel
from resell.gateway import Gateway
from resell.reasoning.drafting import (
    DraftOutcome,
    DraftingError,
    RepairOutcome,
    preserved_ratio,
    repair_draft,
)
from resell.reasoning.listing import DraftClaim, DraftReview, ListingDraft
from resell.reasoning.stages import StageResult, Usage, repair_stage

TITLE = "Bowflex SelectTech 552 Adjustable Dumbbell"


def fixture(tmp_path):
    conn = db.connect(tmp_path / "repair.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    for claim in ("Bowflex SelectTech 552", "the dial's highest setting reads 45"):
        conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
            "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
            (sku, "vision_observation", "fake/m", json.dumps({"claim": claim}),
             db.now_iso(), "visual_observation"),
        )
    conn.commit()
    return conn, gateway, sku


class FakeAdapter:
    """Returns a scripted draft. The point is the plumbing around it."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def rates(self):
        from resell.reasoning.budget import ModelRates, RateBasis

        return ModelRates(1000, 5000, RateBasis.CONFIGURED, "test")

    def estimate_input_tokens(self, request) -> int:
        return len(request.instruction) // 4

    def run(self, request):
        self.requests.append(request)
        return StageResult(
            tool_input=self.payload, usage=Usage(100, 50), latency_ms=5,
            provider=self.provider, model=self.model,
        )


def refused(problem="the title states value(s) the record does not contain: 5-45"):
    return DraftOutcome(
        draft=ListingDraft(
            title="Bowflex SelectTech 552 Dumbbell 5-45 lb",
            description="Bowflex SelectTech 552. The dial's highest setting reads 45.",
            marketing_copy="A whole rack in one hand.",
            claims=(DraftClaim(text="highest setting reads 45", evidence_ids=(2,)),),
        ),
        review=DraftReview(problems=(problem,)),
        request=None, result=None, call_id=None,
    )


# --- the repair runs, and the reviewer still runs on what it produced -------------


def test_a_repair_that_removes_the_refused_phrase_passes(tmp_path):
    """End to end, with the real reviewer. This is the case the feature exists for."""
    conn, gateway, sku = fixture(tmp_path)
    adapter = FakeAdapter({
        "title": TITLE,
        "description": "Bowflex SelectTech 552. The dial's highest setting reads 45.",
        "marketing_copy": "A whole rack in one hand.",
        "claims": [{"text": "the dial's highest setting reads 45", "evidence_ids": [2]}],
    })
    attempt = repair_draft(
        conn, sku, refused(), aspects={"Brand": ["Bowflex"]},
        condition_id="USED_EXCELLENT", adapter=adapter,
    )
    assert attempt.outcome.review.ok, attempt.outcome.review.problems
    assert "5-45" not in attempt.outcome.draft.title


def test_a_repair_that_invents_something_new_is_still_refused(tmp_path):
    """The half of the instruction that does not bend: unsupported claims never
    reach a publishable draft, however much good copy came with them."""
    conn, gateway, sku = fixture(tmp_path)
    adapter = FakeAdapter({
        # A different invented number, in the same place as the one just refused.
        "title": "Bowflex SelectTech 552 Dumbbell 90 lb",
        "description": "Bowflex SelectTech 552.",
        "marketing_copy": "",
        "claims": [],
    })
    attempt = repair_draft(
        conn, sku, refused(), aspects={"Brand": ["Bowflex"]},
        condition_id="USED_EXCELLENT", adapter=adapter,
    )
    assert not attempt.outcome.review.ok
    assert any("90" in p for p in attempt.outcome.review.problems)


def test_the_repair_is_charged_to_its_own_stage(tmp_path):
    """Otherwise a repair looks like a second draft, and the cost of refusals
    becomes invisible in the per-item breakdown."""
    from resell.reasoning.ledger import stage_costs

    conn, gateway, sku = fixture(tmp_path)
    repair_draft(
        conn, sku, refused(), aspects={}, condition_id="USED_EXCELLENT",
        adapter=FakeAdapter({"title": TITLE, "description": "Bowflex SelectTech 552.",
                             "claims": []}),
    )
    assert any(row["purpose"] == "draft_repair" for row in stage_costs(conn, sku))


def test_the_previous_draft_is_carried_on_the_outcome(tmp_path):
    """So the caller can report what was kept without holding it separately."""
    conn, gateway, sku = fixture(tmp_path)
    before = refused()
    attempt = repair_draft(
        conn, sku, before, aspects={}, condition_id="USED_EXCELLENT",
        adapter=FakeAdapter({"title": TITLE, "description": "Bowflex SelectTech 552.",
                             "claims": []}),
    )
    assert attempt.previous is before.draft
    assert attempt.repaired == before.review.problems


def test_the_prompt_carries_the_previous_copy_and_the_exact_complaint(tmp_path):
    """A repair prompt that only says "it was refused" is a regeneration prompt."""
    conn, gateway, sku = fixture(tmp_path)
    adapter = FakeAdapter({"title": TITLE, "description": "Bowflex SelectTech 552.",
                           "claims": []})
    repair_draft(conn, sku, refused(), aspects={}, condition_id="USED_EXCELLENT",
                 adapter=adapter)
    sent = adapter.requests[0].instruction
    assert "5-45" in sent
    assert "Bowflex SelectTech 552 Dumbbell 5-45 lb" in sent
    assert "A whole rack in one hand." in sent


def test_a_repair_that_keeps_everything_scores_as_kept():
    from resell.reasoning.drafting import preserved_ratio
    from resell.reasoning.listing import ListingDraft

    before = ListingDraft(description="One. Two. Three. Four.")
    after = ListingDraft(description="One. Two. Three. Four.")
    assert preserved_ratio(before, after) == 1.0


def test_dropping_the_offending_sentence_keeps_the_rest():
    """The intended shape of a repair: one sentence removed, three untouched."""
    from resell.reasoning.drafting import preserved_ratio
    from resell.reasoning.listing import ListingDraft

    before = ListingDraft(description="One. Two. Three. Four.")
    after = ListingDraft(description="One. Two. Four.")
    assert preserved_ratio(before, after) == 0.75


def test_a_wholesale_rewrite_is_visible_as_one():
    """A model that regenerated from scratch would otherwise produce a passing
    draft while discarding accurate copy, and nothing would say so."""
    from resell.reasoning.drafting import RepairOutcome, preserved_ratio
    from resell.reasoning.listing import ListingDraft

    before = ListingDraft(description="One. Two. Three. Four.")
    after = ListingDraft(description="Entirely different prose about the thing.")
    ratio = preserved_ratio(before, after)
    assert ratio == 0.0
    outcome = RepairOutcome(outcome=None, previous=before, preserved_ratio=ratio)
    assert outcome.looks_like_a_rewrite


def test_a_mostly_preserved_repair_is_not_flagged_as_a_rewrite():
    from resell.reasoning.drafting import RepairOutcome, preserved_ratio
    from resell.reasoning.listing import ListingDraft

    before = ListingDraft(description="One. Two. Three. Four.")
    after = ListingDraft(description="One. Two. Three.")
    outcome = RepairOutcome(
        outcome=None, previous=before, preserved_ratio=preserved_ratio(before, after)
    )
    assert not outcome.looks_like_a_rewrite


def test_repairing_a_draft_that_passed_is_refused(tmp_path):
    """There is nothing to repair, and spending a call to find that out is waste."""
    import pytest as _pytest

    from resell.reasoning.drafting import DraftOutcome, DraftingError, repair_draft
    from resell.reasoning.listing import DraftReview, ListingDraft

    conn, gateway, sku = fixture(tmp_path)
    outcome = DraftOutcome(
        draft=ListingDraft(title="t", description="d"), review=DraftReview(),
        request=None, result=None, call_id=1,
    )
    with _pytest.raises(DraftingError, match="passed review"):
        repair_draft(conn, sku, outcome, aspects={}, condition_id="USED_GOOD")


def test_the_repair_stage_names_the_exact_complaints():
    """Feeding back 'it was refused' teaches nothing; feeding back which phrase
    was refused is what makes a targeted fix possible."""
    from resell.reasoning.stages import repair_stage

    request = repair_stage(
        previous_title="Bowflex SelectTech 5-45 lb",
        previous_description="Some copy.",
        previous_claims="- weight  [cites nothing]",
        previous_marketing="",
        problems="- the title states value(s) the record does not contain: 5-45",
        aspects="- Item Weight: 45 lb",
        observations="[198] the maximum setting visible is 45",
        condition="USED_EXCELLENT",
    )
    assert "does not contain: 5-45" in request.instruction
    assert "Bowflex SelectTech 5-45 lb" in request.instruction
    assert "Change only what was named" in request.system_prompt
    assert "same claim in different words" in request.system_prompt


# --- what counts as "the record" ---------------------------------------------------


def test_an_aspect_name_is_part_of_the_record():
    """MP-000009's repair was refused for stating "65". The record holds an aspect
    called "California Prop 65 Warning" whose value is the single word WARNING, and
    only values were being shown to the reviewer -- so a correctly mapped aspect
    read as an invented specification."""
    from resell.reasoning.drafting import _supported_text
    from resell.reasoning.listing import ListingDraft, review_draft

    aspects = {"California Prop 65 Warning": ["WARNING"], "Brand": ["Bowflex"]}
    review = review_draft(
        ListingDraft(title="Bowflex Dumbbells",
                     description="Carries a California Prop 65 warning."),
        supported_text=_supported_text("A pair of dumbbells.", aspects),
        valid_evidence_ids={1}, available_support=frozenset(),
    )
    assert review.ok, review.problems


def test_both_callers_read_the_record_the_same_way():
    """They built this string separately and drifted once already. A reviewer that
    is stricter on a repair than on the draft refuses correct repairs, which is
    invisible from the outside -- it just looks like the model failing."""
    import inspect

    from resell.reasoning import drafting

    source = inspect.getsource(drafting)
    assert source.count("_supported_text(") == 3   # the definition and two callers
    assert "supported_text=_supported_text(" in source


# --- a refusal must not cost the operator the listing ------------------------------


def test_the_agent_gets_two_attempts_not_one():
    """MP-000018's single retry reproduced the same refused phrase and the run
    died with an empty title. Each attempt is told what the last was refused for,
    so a second is a different question rather than the same one repeated."""
    from resell.orchestrator import DRAFT_REPAIR_ATTEMPTS

    assert DRAFT_REPAIR_ATTEMPTS >= 2


def test_a_refusal_is_a_stop_and_not_a_crash():
    """`RuntimeError` read as a fault; this is the reviewer working."""
    import inspect

    from resell import orchestrator

    source = inspect.getsource(orchestrator.StageRunner._draft)
    assert "DraftRefused" in source
    assert "raise RuntimeError" not in source


def test_a_refused_draft_is_still_not_stored():
    """The half that does not bend. Nothing re-checks claims at publish, so
    `store_draft` is the only gate there is."""
    import inspect

    from resell import orchestrator

    source = inspect.getsource(orchestrator.StageRunner._draft)
    refusal = source.index("DraftRefused")
    store = source.index("store_draft(conn, gateway, sku, outcome)")
    assert refusal < store, "the refusal must return before anything is stored"


def test_the_refused_copy_is_kept_for_the_operator(tmp_path):
    """Throwing the words away is what left operators writing listings from
    scratch over a single phrase."""
    import json

    from resell.orchestrator import _record_refused_draft
    from resell.reasoning.drafting import DraftOutcome
    from resell.reasoning.listing import DraftReview, ListingDraft

    conn, gateway, sku = fixture(tmp_path)
    outcome = DraftOutcome(
        draft=ListingDraft(title="A vintage paperback", description="Lovely copy."),
        review=DraftReview(problems=["'vintage' requires age evidence"]),
        request=None, result=None, call_id=None,
    )
    _record_refused_draft(conn, sku, outcome)

    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND kind = 'draft_refused'",
        (sku,),
    ).fetchone()["payload"])
    assert payload["title"] == "A vintage paperback"
    assert payload["description"] == "Lovely copy."
    assert "requires age evidence" in payload["problems"][0]


def test_the_correction_form_offers_it_back(tmp_path):
    """So the operator edits the phrase the reviewer named rather than composing a
    listing from nothing."""
    from resell import views
    from resell.orchestrator import _record_refused_draft
    from resell.reasoning.drafting import DraftOutcome
    from resell.reasoning.listing import DraftReview, ListingDraft

    conn, gateway, sku = fixture(tmp_path)
    # The form is the identification's editable view, so there has to be one.
    gateway.propose_identification(
        sku, category_id="261186", condition_id="USED_GOOD",
        aspects={"Author": ["Sandra Cisneros"]},
    )
    _record_refused_draft(conn, sku, DraftOutcome(
        draft=ListingDraft(title="A vintage paperback", description="Lovely copy."),
        review=DraftReview(problems=["'vintage' requires age evidence"]),
        request=None, result=None, call_id=None,
    ))

    form = views.correction_form(conn, sku)
    fields = {f.name: f for f in form.fields}
    assert fields["title"].value == "A vintage paperback"
    assert fields["description"].value == "Lovely copy."
    assert "the reviewer stopped it" in fields["title"].help_text
