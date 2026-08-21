"""Operator statements as inputs to aspect resolution, not audit records.

These test the two new functions directly rather than through `map_aspects`,
which needs a model adapter. What they establish is the contract `map_aspects`
depends on: a stored operator candidate is loaded, merged, and carries an
adjudicating basis into `analyse`.
"""

from __future__ import annotations

import sqlite3

import pytest

from resell import db
from resell.reasoning.gaps import Candidate, Resolution, analyse
from resell.reasoning.mapping import (
    _merge_operator_candidates,
    load_operator_candidates,
)
from resell.reasoning.schema import ADJUDICATING_BASES, Basis, EvidenceRef


def fixture(tmp_path):
    """An item with an identification, so candidates have something to hang off."""
    from resell.gateway import Gateway

    conn = db.connect(tmp_path / "candidates.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=2500).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg",
        content_sha256="a" * 64, image_format="jpeg", size_bytes=1000,
        validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(
        sku, title="Blazer", category_id="3002", condition_id="USED_EXCELLENT"
    )
    return conn, gateway, sku


def add_candidate(conn, sku, aspect, value, *, basis, kind="operator_answer"):
    """Insert a candidate with a real evidence row behind it."""
    from resell.gateway import current_identification, now_iso

    cursor = conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
        "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
        (sku, kind, "operator", "{}", now_iso(), str(basis)),
    )
    evidence_id = cursor.lastrowid
    identification_id = current_identification(conn, sku)["id"]
    cursor = conn.execute(
        "INSERT INTO aspect_candidate (identification_id, aspect_name, value, "
        "created_at) VALUES (?,?,?,?)",
        (identification_id, aspect, value, now_iso()),
    )
    conn.execute(
        "INSERT INTO aspect_candidate_evidence (candidate_id, evidence_id) VALUES (?,?)",
        (cursor.lastrowid, evidence_id),
    )
    conn.commit()
    return evidence_id


# --- loading -------------------------------------------------------------------


def test_an_operator_candidate_is_loaded(tmp_path):
    conn, _, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Size", "40R", basis=Basis.OPERATOR)
    loaded = load_operator_candidates(conn, sku)
    assert list(loaded) == ["Size"]
    assert loaded["Size"][0].value == "40R"


def test_the_loaded_candidate_carries_an_adjudicating_basis(tmp_path):
    """The whole point: this is what makes it win in analyse."""
    conn, _, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Size", "40R", basis=Basis.OPERATOR)
    candidate = load_operator_candidates(conn, sku)["Size"][0]
    assert candidate.has_adjudicating_support
    assert all(ref.basis in ADJUDICATING_BASES for ref in candidate.support)


def test_non_operator_candidates_are_not_loaded(tmp_path):
    """Model proposals are recomputed each run; reloading them would resurrect
    values the model has since abandoned."""
    conn, _, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Size", "38R", basis=Basis.INFERENCE, kind="observation")
    assert load_operator_candidates(conn, sku) == {}


def test_an_answer_survives_re_identification(tmp_path):
    """A statement about the object does not expire when the model re-identifies."""
    conn, gateway, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Size", "40R", basis=Basis.OPERATOR)
    gateway.propose_identification(sku, title="Blazer, navy")   # new version
    assert load_operator_candidates(conn, sku)["Size"][0].value == "40R"


def test_another_items_answers_are_not_loaded(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Size", "40R", basis=Basis.OPERATOR)
    other = gateway.ingest_item(purchase_cost_cents=100).sku
    assert load_operator_candidates(conn, other) == {}


# --- merging ---------------------------------------------------------------------


def test_a_new_value_joins_as_a_competing_candidate(tmp_path):
    conn, _, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Size", "40R", basis=Basis.OPERATOR)
    model = {"Size": [Candidate("38R", (EvidenceRef(999, Basis.INFERENCE),))]}
    merged = _merge_operator_candidates(conn, sku, model, {"Size"})
    assert {c.value for c in merged["Size"]} == {"38R", "40R"}


def test_the_same_value_gains_the_operator_basis_rather_than_duplicating(tmp_path):
    """Otherwise the model's candidate would compete with itself."""
    conn, _, sku = fixture(tmp_path)
    evidence_id = add_candidate(conn, sku, "Size", "40R", basis=Basis.OPERATOR)
    model = {"Size": [Candidate("40R", (EvidenceRef(999, Basis.INFERENCE),))]}
    merged = _merge_operator_candidates(conn, sku, model, {"Size"})
    assert len(merged["Size"]) == 1
    candidate = merged["Size"][0]
    assert candidate.has_adjudicating_support
    assert evidence_id in candidate.evidence_ids
    assert 999 in candidate.evidence_ids


def test_an_aspect_outside_the_category_form_is_dropped(tmp_path):
    """analyse is given a cardinality map keyed on the form; an unknown aspect
    has no defined behaviour there."""
    conn, _, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Inseam", "32", basis=Basis.OPERATOR)
    merged = _merge_operator_candidates(conn, sku, {}, {"Size"})
    assert "Inseam" not in merged


def test_merging_with_nothing_stored_returns_the_input(tmp_path):
    conn, _, sku = fixture(tmp_path)
    model = {"Size": [Candidate("38R", (EvidenceRef(1, Basis.INFERENCE),))]}
    assert _merge_operator_candidates(conn, sku, model, {"Size"}) == model


# --- what analyse then does with it -------------------------------------------------


def test_an_operator_answer_resolves_an_aspect_the_model_could_not(tmp_path):
    """The behaviour seam 2 exists for: answering a blocking question resolves it."""
    conn, _, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Size", "40R", basis=Basis.OPERATOR)
    merged = _merge_operator_candidates(conn, sku, {}, {"Size"})
    outcomes, _ = analyse(["Size"], merged, {"Size": "SINGLE"}, {})
    outcome = next(o for o in outcomes if o.aspect_name == "Size")
    assert outcome.value == "40R"
    assert outcome.resolution in (
        Resolution.RESOLVED, Resolution.RESOLVED_BY_OPERATOR,
    )


def test_an_operator_answer_beats_a_conflicting_model_value(tmp_path):
    conn, _, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Size", "40R", basis=Basis.OPERATOR)
    model = {"Size": [Candidate("38R", (EvidenceRef(999, Basis.INFERENCE),))]}
    merged = _merge_operator_candidates(conn, sku, model, {"Size"})
    outcomes, _ = analyse(["Size"], merged, {"Size": "SINGLE"}, {})
    outcome = next(o for o in outcomes if o.aspect_name == "Size")
    assert outcome.resolution is Resolution.RESOLVED_BY_OPERATOR
    assert outcome.value == "40R"
    # The losing candidate is retained rather than deleted.
    assert {c.value for c in outcome.candidates} == {"38R", "40R"}


def test_two_conflicting_operator_answers_are_not_silently_resolved(tmp_path):
    """The operator contradicted themselves; inventing a winner would hide it."""
    conn, _, sku = fixture(tmp_path)
    add_candidate(conn, sku, "Size", "40R", basis=Basis.OPERATOR)
    add_candidate(conn, sku, "Size", "42R", basis=Basis.OPERATOR)
    merged = _merge_operator_candidates(conn, sku, {}, {"Size"})
    outcomes, _ = analyse(["Size"], merged, {"Size": "SINGLE"}, {})
    outcome = next(o for o in outcomes if o.aspect_name == "Size")
    assert outcome.resolution is Resolution.CONTRADICTED
    assert outcome.value is None
