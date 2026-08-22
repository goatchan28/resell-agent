"""Read models. What the CLI and the UI both depend on being true.

These test `views` directly rather than through either front end, because the
point of the module is that neither front end is where the answer comes from. A
formatting change should not be able to break these, and a change to what an item
*is* should not be able to pass them.
"""

from __future__ import annotations

import json

import pytest

from resell import db, views
from resell.domain import FeeModel
from resell.gateway import Gateway, Rejected, now_iso


def fixture(tmp_path, *, photos: int = 1):
    conn = db.connect(tmp_path / "views.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(
        purchase_cost_cents=2500, acquisition_intent="resale", notes="fixture"
    ).sku
    for index in range(photos):
        gateway.attach_photo(
            sku,
            source_path=f"/photo-{index}.jpg",
            content_sha256=chr(ord("a") + index) * 64,
            image_format="jpeg",
            size_bytes=1000 + index,
            validation_errors=None,
        )
    gateway.begin_identification(sku)
    return conn, gateway, sku


def detail(conn, gateway, sku):
    return views.item_detail(
        conn, gateway, sku, marketplace="EBAY_US", environment="sandbox"
    )


# --- item list -----------------------------------------------------------------


def test_an_item_appears_in_the_list_with_its_counts(tmp_path):
    conn, gateway, sku = fixture(tmp_path, photos=2)
    gateway.ask_operator(sku, question="What size?", why_it_matters="", blocking=True)
    [summary] = views.item_summaries(conn)
    assert summary.sku == sku
    assert summary.photo_count == 2
    assert summary.open_question_count == 1
    assert summary.blocking_question_count == 1


def test_the_list_carries_the_current_title_not_a_superseded_one(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(sku, title="First")
    gateway.propose_identification(sku, title="Second")
    [summary] = views.item_summaries(conn)
    assert summary.title == "Second"


def test_abandoned_items_are_listed_by_default(tmp_path):
    """SKUs are never reused, so a gap in the sequence is a question worth
    being able to answer."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.abandon(sku, reason="not worth listing")
    assert [s.sku for s in views.item_summaries(conn)] == [sku]
    assert views.item_summaries(conn, active_only=True) == []


def test_a_state_filter_selects_only_that_state(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    assert [s.sku for s in views.item_summaries(conn, states=("identifying",))] == [sku]
    assert views.item_summaries(conn, states=("listed",)) == []


# --- questions -----------------------------------------------------------------


def test_both_blocking_and_optional_questions_are_returned(tmp_path):
    """Non-blocking questions were being recorded and displayed nowhere."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Blocking?", why_it_matters="", blocking=True)
    gateway.ask_operator(sku, question="Optional?", why_it_matters="", blocking=False)
    questions = views.open_questions(conn, sku=sku)
    assert [q.blocking for q in questions] == [True, False]
    assert [q.blocking for q in views.open_questions(conn, blocking_only=True)] == [True]


def test_an_answered_question_leaves_the_queue(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    accepted = gateway.ask_operator(
        sku, question="What size?", why_it_matters="", blocking=True
    )
    question_id = views.open_questions(conn, sku=sku)[0].id
    gateway.answer_question(question_id, "40R", operator=True)
    assert views.open_questions(conn, sku=sku) == []
    assert accepted.sku == sku


def test_a_questions_allowed_values_are_returned_for_the_answer_form(tmp_path):
    """The same list Gateway.answer_question validates against, so a picker and a
    printed hint cannot disagree with each other."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Which size?", why_it_matters="", blocking=True)
    conn.execute(
        "UPDATE open_question SET aspect_name = 'Size', allowed_values_json = ? "
        "WHERE sku = ?",
        (json.dumps(["38R", "40R", "42R"]), sku),
    )
    [question] = views.open_questions(conn, sku=sku)
    assert question.allowed_values == ("38R", "40R", "42R")
    assert question.aspect_name == "Size"


def test_a_question_about_an_aspect_since_resolved_offers_that_value(tmp_path):
    """Being asked again about something already decided teaches people to skip
    the prompt, so the answer already on the record is offered."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Which type?", why_it_matters="", blocking=True)
    conn.execute("UPDATE open_question SET aspect_name = 'Type' WHERE sku = ?", (sku,))
    gateway.propose_identification(sku, title="Jacket", aspects={"Type": ["Suit Jacket"]})
    [question] = views.open_questions(conn, sku=sku)
    assert question.resolved_value == ("Suit Jacket",)
    assert question.suggested_answer == "Suit Jacket"


def test_a_question_about_an_unresolved_aspect_suggests_nothing(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Which type?", why_it_matters="", blocking=True)
    conn.execute("UPDATE open_question SET aspect_name = 'Type' WHERE sku = ?", (sku,))
    gateway.propose_identification(sku, title="Jacket", aspects={"Fit": ["Slim"]})
    [question] = views.open_questions(conn, sku=sku)
    assert question.suggested_answer is None


def test_another_items_questions_are_not_returned(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    other = gateway.ingest_item(purchase_cost_cents=100).sku
    gateway.ask_operator(other, question="Anything?", why_it_matters="", blocking=True)
    assert views.open_questions(conn, sku=sku) == []
    assert len(views.open_questions(conn)) == 1


# --- item detail ---------------------------------------------------------------


def test_an_unknown_sku_is_rejected_rather_than_returning_an_empty_item(tmp_path):
    conn, gateway, _ = fixture(tmp_path)
    with pytest.raises(Rejected):
        detail(conn, gateway, "MP-999999")


def test_detail_reports_valid_photos_against_the_total_attached(tmp_path):
    conn, gateway, sku = fixture(tmp_path, photos=1)
    gateway.attach_photo(
        sku, source_path="/bad.gif", content_sha256="f" * 64,
        image_format="gif", size_bytes=10, validation_errors=["gif is not accepted"],
    )
    item = detail(conn, gateway, sku)
    assert len(item.photos) == 1
    assert item.photo_count_total == 2
    assert item.invalid_photo_count == 1


def test_detail_splits_blocking_from_optional_questions(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Blocking?", why_it_matters="", blocking=True)
    gateway.ask_operator(sku, question="Optional?", why_it_matters="", blocking=False)
    item = detail(conn, gateway, sku)
    assert len(item.blocking_questions) == 1
    assert len(item.optional_questions) == 1


def test_identification_names_the_fields_still_needed_to_price(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(sku, title="Jacket")
    item = detail(conn, gateway, sku)
    assert item.identification.missing_to_price == ("category_id", "condition_id")


def test_a_complete_identification_is_missing_nothing(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(
        sku, title="Jacket", category_id="3001", condition_id="NEW"
    )
    assert detail(conn, gateway, sku).identification.missing_to_price == ()


def test_superseded_identifications_are_reported_as_recoverable(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(sku, title="First")
    gateway.propose_identification(sku, title="Second")
    item = detail(conn, gateway, sku)
    assert item.identification.title == "Second"
    assert [row.title for row in item.superseded] == ["First"]


def test_aspects_come_back_as_a_dict_not_a_json_string(tmp_path):
    """Every caller parsed it; one of them was going to forget."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(sku, title="Jacket", aspects={"Fit": ["Slim"]})
    assert detail(conn, gateway, sku).identification.aspects == {"Fit": ["Slim"]}


def test_an_item_with_no_listing_has_no_proposal_hash(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    item = detail(conn, gateway, sku)
    assert item.listing is None
    assert item.proposal_hash is None
    assert item.live_approval_hash is None
    assert item.approval_matches_proposal is False


def test_evidence_records_report_whether_they_reach_the_model(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, recorded_at) "
        "VALUES (?, 'note', 'operator', '{}', 0, ?)",
        (sku, now_iso()),
    )
    [record] = detail(conn, gateway, sku).evidence
    assert record.send_to_model is False


# --- listing draft -------------------------------------------------------------


def test_the_draft_view_reports_no_drift_before_anything_is_proposed(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(sku, title="Jacket", description="Nice jacket.")
    draft = views.listing_draft(detail(conn, gateway, sku))
    assert draft.identification_title == "Jacket"
    assert draft.proposed_title is None
    assert draft.content_differs_from_proposal is False
    assert draft.title_length == len("Jacket")


# --- aspect form ---------------------------------------------------------------


class FakeSpec:
    """Stands in for the Publisher's aspect spec. Only the read side is used."""

    def __init__(self, name, *, required=True, allowed=(), max_length=None):
        self.name = name
        self.required = required
        self.mode = "FREE_TEXT" if not allowed else "SELECTION_ONLY"
        self.cardinality = "SINGLE"
        self.data_type = "STRING"
        self.max_length = max_length
        self.allowed_values = list(allowed)
        self.selection_only = bool(allowed)

    def unknown_values(self, values):
        if not self.allowed_values:
            return []
        return [v for v in values if v not in self.allowed_values]


def test_the_aspect_form_marks_a_required_aspect_with_no_value_as_missing():
    rows = views.aspect_rows([FakeSpec("Size", allowed=("38R", "40R"))], {})
    assert rows[0].status == "missing"
    assert rows[0].current == ()


def test_the_aspect_form_marks_a_filled_aspect_as_set():
    rows = views.aspect_rows(
        [FakeSpec("Size", allowed=("38R", "40R"))], {"Size": ["40R"]}
    )
    assert rows[0].status == "set"
    assert rows[0].unknown_current == ()


def test_a_current_value_outside_ebays_list_is_reported_rather_than_hidden():
    rows = views.aspect_rows(
        [FakeSpec("Size", allowed=("38R", "40R"))], {"Size": ["99R"]}
    )
    assert rows[0].unknown_current == ("99R",)


def test_an_optional_aspect_with_no_value_is_not_missing():
    rows = views.aspect_rows([FakeSpec("Fit", required=False)], {})
    assert rows[0].status == "unset"
    assert rows[0].free_text is True


def test_the_form_names_every_outstanding_required_aspect():
    form = views.AspectForm(
        category_id="3001",
        marketplace="EBAY_US",
        rows=views.aspect_rows(
            [
                FakeSpec("Size", allowed=("40R",)),
                FakeSpec("Type", allowed=("Suit Jacket",)),
                FakeSpec("Fit", required=False),
            ],
            {"Size": ["40R"]},
        ),
    )
    assert form.outstanding == ("Type",)
    assert form.required_count == 2


# --- pricing -------------------------------------------------------------------


def test_the_pricing_defaults_come_from_the_identification(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(
        sku, title="Jacket", category_id="3001", condition_id="USED_GOOD"
    )
    request = views.default_pricing_request(conn, sku, marketplace="EBAY_US")
    assert request.category_id == "3001"
    # USED_GOOD is condition id 5000, which bands as used_good.
    assert request.condition_band == "used_good"


def test_an_unrecognised_condition_enum_bands_as_unknown_rather_than_guessing(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(sku, title="Jacket", condition_id="INVENTED")
    request = views.default_pricing_request(conn, sku, marketplace="EBAY_US")
    assert request.condition_band == "unknown"


def test_an_item_with_no_comps_is_unpriceable_and_offers_no_strategies(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    view = views.pricing_view(
        conn, sku, views.PricingRequest(), marketplace="EBAY_US"
    )
    assert view.unpriceable
    assert view.strategies == ()
    assert view.comp_count == 0


def test_a_priced_item_reports_three_strategies_with_one_default(tmp_path):
    from datetime import datetime, timezone

    from resell import store_pricing as sp
    from resell.pricing.comps import (
        CompBasis,
        CompClaim,
        CompObservation,
        Comparability,
        ConditionBand,
        PriceKind,
    )

    conn, gateway, sku = fixture(tmp_path)
    gateway.propose_identification(
        sku, title="Jacket", category_id="3001", condition_id="USED_GOOD"
    )
    for index, cents in enumerate((9000, 10000, 11000)):
        observation = CompObservation(
            comp_id=f"comp_{index}",
            marketplace="EBAY_US",
            external_id=f"ext-{index}",
            price_kind=PriceKind.REALIZED,
            basis=CompBasis.SOLD_SIMILAR,
            price_cents=cents,
            observed_at=datetime.now(timezone.utc),
            condition_band=ConditionBand.USED_GOOD,
            shipping_cents=0,
        )
        sp.record_comp_observation(conn, observation)
        sp.record_comp_claim(
            conn,
            CompClaim(
                claim_id=f"claim_{index}",
                sku=sku,
                comp_id=observation.comp_id,
                comparability=Comparability.SAME_FAMILY_VARIANT,
                item_citations=("ev_1",),
                comp_citations=("title",),
            ),
            identity_resolution="unresolved",
        )

    view = views.pricing_view(
        conn, sku,
        views.PricingRequest(condition_band="used_good"),
        marketplace="EBAY_US",
    )
    assert not view.unpriceable
    assert view.comp_count == 3
    assert len(view.strategies) == 3
    assert sum(1 for s in view.strategies if s.is_default) == 1
    assert view.band_central_cents == 10000


def test_the_pricing_view_records_nothing(tmp_path):
    """A page view must not mint a proposal or freeze a comp set."""
    conn, gateway, sku = fixture(tmp_path)
    before = conn.execute("SELECT COUNT(*) FROM price_proposal").fetchone()[0]
    views.pricing_view(conn, sku, views.PricingRequest(), marketplace="EBAY_US")
    views.pricing_view(conn, sku, views.PricingRequest(), marketplace="EBAY_US")
    after = conn.execute("SELECT COUNT(*) FROM price_proposal").fetchone()[0]
    assert before == after == 0
