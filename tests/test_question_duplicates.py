"""One outstanding question per aspect, however many times the mapper runs.

`map-aspects --apply` opens a blocking question per unresolved required aspect,
and re-running it is the normal way to work: map, answer, map again. Nothing
deduplicated, so three runs left three identical questions about Model -- and
answering one of them left the item in needs_info behind the other two, which is
the part that actually cost something.

Both halves are tested here: not opening the duplicate, and settling the ones
already open from before the check existed.
"""

from __future__ import annotations

import pytest

from resell import db, views
from resell.domain import FeeModel, ItemState
from resell.gateway import Gateway, Rejected, unresolved_blocking_questions


def fixture(tmp_path):
    conn = db.connect(tmp_path / "questions.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(
        sku, title="Beats Pill speaker", category_id="111694", condition_id="USED_GOOD"
    )
    return conn, gateway, sku


GAP = "Nothing observed supports a value for Model. Can you supply it?"


def ask_model_gap(gateway, sku, question=GAP):
    return gateway.ask_operator(
        sku, question=question,
        why_it_matters="required aspect Model is unsupported",
        aspect_name="Model",
    )


def open_ids(conn, sku):
    return [q.id for q in views.open_questions(conn, sku=sku)]


# --- not opening the duplicate -------------------------------------------------


def test_asking_the_same_gap_twice_opens_one_question(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    first = ask_model_gap(gateway, sku)
    second = ask_model_gap(gateway, sku)
    assert len(open_ids(conn, sku)) == 1
    assert second.data["already_open"] is True
    assert second.data["question_id"] == open_ids(conn, sku)[0]
    assert first.data.get("already_open") is None


def test_re_running_the_mapper_many_times_still_leaves_one(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    for _ in range(5):
        ask_model_gap(gateway, sku)
    assert len(open_ids(conn, sku)) == 1


def test_a_reworded_question_about_the_same_aspect_is_still_a_duplicate(tmp_path):
    """The aspect is what the question is about; the wording is incidental."""
    conn, gateway, sku = fixture(tmp_path)
    ask_model_gap(gateway, sku)
    ask_model_gap(gateway, sku, question="Where does the model number appear?")
    assert len(open_ids(conn, sku)) == 1


def test_different_aspects_each_get_their_own_question(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    ask_model_gap(gateway, sku)
    gateway.ask_operator(
        sku, question="Which connectivity?", why_it_matters="", aspect_name="Connectivity"
    )
    assert len(open_ids(conn, sku)) == 2


def test_an_answered_question_does_not_block_asking_again(tmp_path):
    """The aspect can come unresolved again after a re-identification, and a met
    request is not an outstanding one."""
    conn, gateway, sku = fixture(tmp_path)
    ask_model_gap(gateway, sku)
    gateway.answer_question(open_ids(conn, sku)[0], "Pill", operator=True)
    ask_model_gap(gateway, sku)
    assert len(open_ids(conn, sku)) == 1


def test_two_free_form_questions_in_different_words_are_both_kept(tmp_path):
    """No aspect to key on, so only identical text is collapsed. An operator who
    asked two different things deliberately gets two questions."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Does it power on?", why_it_matters="")
    gateway.ask_operator(sku, question="Is the cable included?", why_it_matters="")
    assert len(open_ids(conn, sku)) == 2


def test_the_identical_free_form_question_is_collapsed(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Does it power on?", why_it_matters="")
    gateway.ask_operator(sku, question="Does it power on?", why_it_matters="")
    assert len(open_ids(conn, sku)) == 1


def test_an_empty_question_is_still_refused(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    with pytest.raises(Rejected):
        gateway.ask_operator(sku, question="   ", why_it_matters="")


# --- settling the duplicates already open --------------------------------------


def duplicate_pair(conn, gateway, sku):
    """Two open questions about Model, as the record has them today.

    Written straight to the table because `ask_operator` now refuses to create
    this state -- which is the point, but the rows exist in databases that predate
    the check and answering one of them has to work.
    """
    ask_model_gap(gateway, sku)
    conn.execute(
        "INSERT INTO open_question (sku, question, why_it_matters, blocking, "
        "asked_at, aspect_name) VALUES (?, ?, ?, 1, ?, 'Model')",
        (sku, GAP, "required aspect Model is unsupported", db.now_iso()),
    )
    ids = open_ids(conn, sku)
    assert len(ids) == 2
    return ids


def test_answering_one_duplicate_settles_the_other(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    first, second = duplicate_pair(conn, gateway, sku)
    accepted = gateway.answer_question(first, "Pill", operator=True)
    assert open_ids(conn, sku) == []
    assert accepted.data["duplicates_settled"] == [second]


def test_the_settled_duplicate_records_where_its_answer_came_from(tmp_path):
    """It disappears from the queue, so the record has to say why."""
    conn, gateway, sku = fixture(tmp_path)
    first, second = duplicate_pair(conn, gateway, sku)
    gateway.answer_question(first, "Pill", operator=True)
    answer = conn.execute(
        "SELECT answer FROM open_question WHERE id = ?", (second,)
    ).fetchone()[0]
    assert "Pill" in answer
    assert f"settled by the answer to question {first}" in answer


def test_settling_the_duplicate_releases_the_item_from_needs_info(tmp_path):
    """The trap this pairs with preventing: the item was held in needs_info by a
    question that had in fact been answered."""
    conn, gateway, sku = fixture(tmp_path)
    duplicate_pair(conn, gateway, sku)
    assert conn.execute(
        "SELECT state FROM item WHERE sku = ?", (sku,)
    ).fetchone()[0] == str(ItemState.NEEDS_INFO)

    first = open_ids(conn, sku)[0]
    accepted = gateway.answer_question(first, "Pill", operator=True)
    assert accepted.to_state == ItemState.IDENTIFYING
    assert unresolved_blocking_questions(conn, sku) == []


def test_only_one_evidence_row_is_written_for_one_operator_statement(tmp_path):
    """The operator said it once. Two evidence rows would double-count it as
    support, and the second would be a statement they never made."""
    conn, gateway, sku = fixture(tmp_path)
    first, _ = duplicate_pair(conn, gateway, sku)
    gateway.answer_question(first, "Pill", operator=True)
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ? AND kind = 'operator_answer'",
        (sku,),
    ).fetchone()[0] == 1


def test_a_question_about_another_aspect_is_not_settled(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    ask_model_gap(gateway, sku)
    gateway.ask_operator(
        sku, question="Which connectivity?", why_it_matters="", aspect_name="Connectivity"
    )
    model_id = next(
        q.id for q in views.open_questions(conn, sku=sku) if q.aspect_name == "Model"
    )
    gateway.answer_question(model_id, "Pill", operator=True)
    remaining = views.open_questions(conn, sku=sku)
    assert [q.aspect_name for q in remaining] == ["Connectivity"]


# --- the candidate id behind it all --------------------------------------------


def test_answering_the_same_aspect_and_value_twice_does_not_crash(tmp_path):
    """`INSERT OR IGNORE` then `cursor.lastrowid` returns the last rowid inserted
    on *any* table when the insert was ignored, so the id going into the foreign
    key was an evidence id. This is what actually broke on MP-000005: the second
    answer raised after its UPDATE had committed, leaving the question answered and
    the item held in needs_info with nothing outstanding."""
    conn, gateway, sku = fixture(tmp_path)
    ask_model_gap(gateway, sku)
    first = open_ids(conn, sku)[0]
    gateway.answer_question(first, "Pill", operator=True)

    # A second, independent question about the same aspect, answered identically.
    gateway.ask_operator(
        sku, question="Confirm the model, please", why_it_matters="",
        aspect_name="Model",
    )
    second = open_ids(conn, sku)[0]
    gateway.answer_question(second, "Pill", operator=True)   # used to raise

    candidates = conn.execute(
        "SELECT id FROM aspect_candidate WHERE aspect_name = 'Model' AND value = 'Pill'"
    ).fetchall()
    assert len(candidates) == 1


def test_both_answers_cite_the_same_candidate(tmp_path):
    """Two statements, one candidate, two citations on it -- and each citation
    pointing at the candidate it belongs to. A stale id here would file the second
    citation against whatever candidate happened to hold that rowid."""
    conn, gateway, sku = fixture(tmp_path)
    ask_model_gap(gateway, sku)
    gateway.answer_question(open_ids(conn, sku)[0], "Pill", operator=True)
    gateway.ask_operator(
        sku, question="Confirm the model", why_it_matters="", aspect_name="Model"
    )
    gateway.answer_question(open_ids(conn, sku)[0], "Pill", operator=True)

    candidate_id = conn.execute(
        "SELECT id FROM aspect_candidate WHERE aspect_name = 'Model'"
    ).fetchone()[0]
    linked = conn.execute(
        "SELECT evidence_id FROM aspect_candidate_evidence WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchall()
    assert len(linked) == 2
    for row in linked:
        kind = conn.execute(
            "SELECT kind FROM evidence WHERE id = ?", (row["evidence_id"],)
        ).fetchone()[0]
        assert kind == "operator_answer"


def test_the_answer_still_resolves_through_reasoning_afterwards(tmp_path):
    """The seam the whole loop exists for, checked after the id fix."""
    from resell.reasoning.mapping import load_operator_candidates

    conn, gateway, sku = fixture(tmp_path)
    ask_model_gap(gateway, sku)
    gateway.answer_question(open_ids(conn, sku)[0], "Pill", operator=True)
    loaded = load_operator_candidates(conn, sku)
    assert loaded["Model"][0].value == "Pill"
    assert loaded["Model"][0].has_adjudicating_support


def test_a_free_form_answer_settles_nothing_else(tmp_path):
    """No aspect, so there is nothing to propagate along."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Does it power on?", why_it_matters="")
    gateway.ask_operator(sku, question="Is the cable included?", why_it_matters="")
    first = open_ids(conn, sku)[0]
    accepted = gateway.answer_question(first, "yes", operator=True)
    assert accepted.data.get("duplicates_settled") is None
    assert len(open_ids(conn, sku)) == 1


# --- an answer has to change something ---------------------------------------------


def test_an_answered_aspect_reaches_the_identification(tmp_path):
    """MP-000016's complaint. Answering recorded an `aspect_candidate` and nothing
    promoted it, so publishing still refused for want of the aspect the operator
    had just supplied."""
    from resell.cli_item import operator_answers

    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(
        sku, question="Nothing observed supports a value for Model.",
        aspect_name="Model",
    )
    qid = open_ids(conn, sku)[0]
    gateway.answer_question(qid, "DJI Osmo Action 5 Pro", operator=True)

    assert operator_answers(conn, sku) == {"Model": ["DJI Osmo Action 5 Pro"]}


def test_the_operator_overlays_the_model(tmp_path):
    """Where the operator has spoken they win. `basis='operator'` is what
    adjudicates a contradiction, so the overlay is applied after the model's."""
    import inspect

    from resell import cli_item

    source = inspect.getsource(cli_item._apply_mapping)
    assert "resolved.update(answered)" in source
    at_model = source.index("for item in outcome.outcomes")
    at_operator = source.index("resolved.update(answered)")
    assert at_model < at_operator


def test_the_latest_answer_wins(tmp_path):
    """An operator who answered Model twice meant the second one. The first stays
    on the record as evidence."""
    from resell.cli_item import operator_answers

    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Model?", aspect_name="Model")
    gateway.answer_question(open_ids(conn, sku)[0], "DJI Osmo Action", operator=True)
    gateway.ask_operator(sku, question="Model, more precisely?", aspect_name="Model")
    remaining = open_ids(conn, sku)
    if remaining:
        gateway.answer_question(remaining[0], "DJI Osmo Action 5 Pro", operator=True)

    assert operator_answers(conn, sku)["Model"] == ["DJI Osmo Action 5 Pro"]


def test_an_unanswered_question_contributes_nothing(tmp_path):
    from resell.cli_item import operator_answers

    conn, gateway, sku = fixture(tmp_path)
    gateway.ask_operator(sku, question="Model?", aspect_name="Model")
    assert operator_answers(conn, sku) == {}


def test_a_question_is_not_asked_again_once_answered(tmp_path):
    """The third ask is what made this unbearable: the same two questions,
    answered twice, opened a third time."""
    import inspect

    from resell import cli_item

    source = inspect.getsource(cli_item._apply_mapping)
    assert "if gap.aspect_name in answered:" in source
    assert "Asking a third time" in source
