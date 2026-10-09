"""The seams pressing Run must not slip past.

MP-000013 went from three photographs to `pricing` in sixty-two seconds without
anyone agreeing what it was. The operator pressed Run afterwards and reasonably
concluded Run had skipped something; it had not — the upload chain had already
carried the item through, because every identification step is the agent's and
nothing stopped it.

Two rules are pinned here. An operator-owned step is a wall: no route walks
through it, and no runner exists to execute it. And upload-triggered work is
observable in exactly the way Run's is, because it is the same machinery.
"""

from __future__ import annotations

import json

import pytest

from resell import db, runs
from resell.domain import FeeModel, ItemState
from resell.gateway import Gateway
from resell.orchestrator import (
    Actor, Step, StageRunner, advance, confirm_identity, identity_confirmed,
    next_step,
)


def fixture(tmp_path):
    conn = db.connect(tmp_path / "seams.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    return conn, gateway, sku


def drafted(conn, gateway, sku):
    """An item the agent has taken as far as it can without being told what it is."""
    gateway.begin_identification(sku)
    conn.execute(
        "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
        "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
        (sku, "vision_observation", "fake/m", json.dumps({"claim": "a dumbbell"}),
         db.now_iso(), "visual_observation"),
    )
    conn.commit()
    gateway.propose_identification(
        sku, category_id="137865", condition_id="USED_EXCELLENT",
        title="Bowflex SelectTech Adjustable Dumbbells (Pair)",
        description="A pair of adjustable dumbbells.",
        aspects={"Brand": ["Bowflex"]},
    )
    return conn, gateway, sku


# --- the wall ----------------------------------------------------------------------


def test_an_unresolved_item_stops_before_pricing(tmp_path):
    conn, gateway, sku = drafted(*fixture(tmp_path))
    step = next_step(conn, sku)
    assert step.step is Step.CONFIRM_IDENTITY
    assert step.actor is Actor.OPERATOR


def test_pressing_run_does_not_walk_through_it(tmp_path):
    """The property the whole file exists for. `advance` is what every route --
    Run, upload, adding photos -- ultimately calls."""
    conn, gateway, sku = drafted(*fixture(tmp_path))

    for _ in range(3):
        report = advance(conn, gateway, sku, max_steps=8)
        assert report.stopped_at.step is Step.CONFIRM_IDENTITY

    assert conn.execute(
        "SELECT state FROM item WHERE sku = ?", (sku,)
    ).fetchone()[0] == str(ItemState.IDENTIFYING)


def test_there_is_no_runner_that_could_execute_it(tmp_path):
    """Belt and braces: even a mis-routed agent step could not confirm an identity
    on the operator's behalf, because nothing knows how to."""
    assert not hasattr(StageRunner, f"_{Step.CONFIRM_IDENTITY}")


def test_confirming_lets_it_through(tmp_path):
    conn, gateway, sku = drafted(*fixture(tmp_path))
    assert not identity_confirmed(conn, sku)

    confirm_identity(conn, sku, note="it is a Bowflex SelectTech")

    assert identity_confirmed(conn, sku)
    assert next_step(conn, sku).step is Step.BEGIN_PRICING


def test_the_confirmation_is_on_the_record(tmp_path):
    """Like every other decision on an item."""
    conn, gateway, sku = drafted(*fixture(tmp_path))
    confirm_identity(conn, sku, note="matches the dial markings")

    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND kind = 'identity_confirmed'",
        (sku,),
    ).fetchone()["payload"])
    assert payload["note"] == "matches the dial markings"


def test_an_item_with_a_supported_mode_is_never_asked(tmp_path):
    """Stopping on an identification the agent can defend would be a rubber stamp,
    and a click on every item is how a seam stops being read.

    The seam used to require `RESOLVED`, which is held closed, so it asked on all
    54 items of the historical replay -- a pricing decision leaking out as a
    question to the seller."""
    conn, gateway, sku = drafted(*fixture(tmp_path))
    supported_mode(conn, sku)

    assert next_step(conn, sku).step is Step.BEGIN_PRICING
    assert not identity_confirmed(conn, sku)


def test_a_supported_mode_does_not_lift_the_pricing_ceiling(tmp_path):
    """The separation the change rests on. Continuing without a question is a
    statement about what we know; `same_product` is a statement about what the
    comps may claim. Only the second is gated on resolution."""
    from resell.pricing.comps import Comparability, ceiling_for_identity
    from resell.reasoning.research_loop import identity_resolution

    conn, gateway, sku = drafted(*fixture(tmp_path))
    supported_mode(conn, sku, "product_family")

    assert next_step(conn, sku).step is Step.BEGIN_PRICING
    assert ceiling_for_identity(str(identity_resolution(conn, sku))) is (
        Comparability.SAME_FAMILY_VARIANT
    )


# --- upload is observable in the same way Run is -----------------------------------


def test_every_route_that_starts_work_goes_through_the_same_helper():
    """Upload used to do the work inside the request, so the same long operation
    had a progress panel when Run started it and a frozen page when upload did."""
    from pathlib import Path as _Path

    source = _Path("src/resell/webui/app.py").read_text()
    # Run, both upload paths, and the four operator decisions that hand back to
    # the agent -- answering a question, approving a price, granting research,
    # setting a price. `more_research` runs a whole comp round.
    assert source.count("_start_agent(sku)") >= 7
    # There is no second way to do agent work. The synchronous helper is gone
    # rather than merely unused, so nothing can drift back onto it.
    assert "_advance_and_report" not in source


def test_uploading_photos_answers_with_a_run_to_watch(tmp_path):
    from resell.webui import create_app
    from tests.test_webui import config_for, png

    app = create_app(config=config_for(tmp_path))
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.post("/items", data={
        "cost_dollars": "18.00",
        "photos": (__import__("io").BytesIO(png()), "a.png"),
    }, content_type="multipart/form-data")

    assert response.status_code == 302
    assert "run=" in response.headers["Location"]


def test_adding_photos_to_an_existing_item_does_too(tmp_path):
    from resell.webui import create_app
    from tests.test_webui import config_for, png

    config = config_for(tmp_path)
    conn = db.connect(config.db_path)
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=500).sku

    app = create_app(config=config)
    app.config["TESTING"] = True
    response = app.test_client().post(f"/items/{sku}/photos", data={
        "photos": (__import__("io").BytesIO(png()), "b.png"),
    }, content_type="multipart/form-data")

    assert "run=" in response.headers["Location"]


# --- the three properties, stated as the operator would state them -----------------


def pricing_side_effects(conn, sku) -> dict:
    """Everything that only happens once an item is being priced."""
    return {
        "state": conn.execute(
            "SELECT state FROM item WHERE sku = ?", (sku,)
        ).fetchone()[0],
        "comp_calls": conn.execute(
            "SELECT COUNT(*) FROM model_call WHERE sku = ? AND purpose LIKE 'comp%'",
            (sku,),
        ).fetchone()[0],
        "lookups": conn.execute(
            "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'pricing'",
            (sku,),
        ).fetchone()[0],
        "proposals": conn.execute(
            "SELECT COUNT(*) FROM price_proposal WHERE sku = ?", (sku,)
        ).fetchone()[0],
    }


def test_an_unresolved_item_cannot_enter_pricing(tmp_path):
    """Not just "the next step is the operator's" -- nothing downstream of the
    seam may have happened. A budget spent before the question is asked is a
    budget spent on a guess, which is the whole reason the seam exists."""
    conn, gateway, sku = drafted(*fixture(tmp_path))
    before = pricing_side_effects(conn, sku)

    for _ in range(5):
        advance(conn, gateway, sku, max_steps=8)

    assert pricing_side_effects(conn, sku) == before
    assert before["state"] == str(ItemState.IDENTIFYING)
    assert before["comp_calls"] == 0


def test_pressing_run_over_and_over_through_the_real_route_changes_nothing(tmp_path):
    """`advance` is one caller. This is the button the operator actually presses,
    through the routing, the run thread and the state machine together."""
    from resell.webui import create_app
    from tests.test_webui import config_for

    config = config_for(tmp_path)
    conn = db.connect(config.db_path)
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    drafted(conn, gateway, sku)
    before = pricing_side_effects(conn, sku)

    app = create_app(config=config)
    app.config["TESTING"] = True
    client = app.test_client()
    for _ in range(4):
        assert client.post(f"/items/{sku}/run").status_code == 302

    # the run threads have nothing to do -- the next step is not the agent's
    _settle()
    reopened = db.connect(config.db_path)
    assert pricing_side_effects(reopened, sku) == before
    assert next_step(reopened, sku).step is Step.CONFIRM_IDENTITY


def test_a_resolved_item_proceeds_into_pricing_on_its_own(tmp_path):
    """The other half. A seam that stopped everything would be a wall."""
    conn, gateway, sku = drafted(*fixture(tmp_path))
    supported_mode(conn, sku)

    report = advance(conn, gateway, sku, runner=_QuietRunner(gateway), max_steps=8)

    assert conn.execute(
        "SELECT state FROM item WHERE sku = ?", (sku,)
    ).fetchone()[0] == str(ItemState.PRICING)
    assert Step.CONFIRM_IDENTITY not in getattr(report, "ran", [])
    assert not identity_confirmed(conn, sku)      # nobody was asked


def test_confirming_an_unresolved_item_then_lets_it_proceed(tmp_path):
    conn, gateway, sku = drafted(*fixture(tmp_path))
    confirm_identity(conn, sku, note="it is a Bowflex")

    advance(conn, gateway, sku, runner=_QuietRunner(gateway), max_steps=8)

    assert conn.execute(
        "SELECT state FROM item WHERE sku = ?", (sku,)
    ).fetchone()[0] == str(ItemState.PRICING)


def supported_mode(conn, sku, mode="branded_generic"):
    """A mode the gate accepted: what "the agent has a claim it can defend" means.

    Written straight onto the current identification, the way `declare_mode` does,
    because what the seam reads is the accepted mode and not how it got there.
    """
    conn.execute(
        "UPDATE identification SET mode = ? WHERE sku = ? AND superseded_at IS NULL",
        (mode, sku),
    )
    conn.commit()


def resolve(conn, sku):
    """A catalogue match strong enough to donate: what `resolved` means."""
    conn.execute(
        "INSERT INTO product_match (sku, candidate_ref, is_match, strength, "
        "source_authority, donation_scope, created_at, rationale, item_evidence, "
        "candidate_evidence) VALUES (?,?,1,?,?,?,?,?,?,?)",
        (sku, "cand-552", "identifier_verified", "manufacturer", "attributes",
         db.now_iso(), "MPN matched the catalogue entry", "[1]", "[2]"),
    )
    conn.commit()


class _QuietRunner:
    """Runs the real transitions and nothing else, so the state machine is what
    is under test rather than a model provider."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.ran = []

    def run(self, conn, gateway, sku, step):
        self.ran.append(step)
        if step is Step.BEGIN_PRICING:
            gateway.begin_pricing(sku)
        return str(step)


def _settle(seconds: float = 1.0):
    """Give the run threads a moment. They have nothing to do, so this is short."""
    import time

    time.sleep(seconds)


def test_the_seam_is_derived_from_the_record_not_from_a_flag(tmp_path):
    """A boolean somebody sets is a boolean somebody forgets to set.

    The gate reads the *accepted mode*, which `declare_mode` writes only when
    `mode_is_supported` says the stored evidence earns it -- so the question is
    still derived, and still from evidence rather than from an opinion. What
    changed is which derived fact it reads.
    """
    import inspect

    from resell import orchestrator

    source = inspect.getsource(orchestrator.identity_needs_confirming)
    body = source[source.index('"""', source.index('"""') + 3) + 3:]

    assert "current_identification(conn, sku)" in body
    assert "IdentificationMode.UNRESOLVED" in body
    # And deliberately not the pricing signal: the gate and the ladder read
    # different facts now, which is the whole point of the change.
    assert "identity_resolution" not in body


def test_the_ladder_still_answers_only_to_resolution(tmp_path):
    """The gate and the ladder read different facts on purpose, so this pins the
    ladder's own rule: nothing but a resolved identity lifts it to `same_product`,
    and no mode, however well supported, has any say."""
    from resell.pricing.comps import Comparability, ceiling_for_identity
    from resell.reasoning.research_loop import identity_resolution

    conn, gateway, sku = drafted(*fixture(tmp_path))
    assert ceiling_for_identity(str(identity_resolution(conn, sku))) is (
        Comparability.SAME_FAMILY_VARIANT
    )

    supported_mode(conn, sku, "product_family")
    assert ceiling_for_identity(str(identity_resolution(conn, sku))) is (
        Comparability.SAME_FAMILY_VARIANT
    )

    # Only a donating catalogue match does, and exact resolution is held closed --
    # so this is reachable in a test and not in the product.
    resolve(conn, sku)
    assert ceiling_for_identity(str(identity_resolution(conn, sku))) is (
        Comparability.SAME_PRODUCT
    )


# --- a question stops being asked once the value exists ----------------------------


def test_the_full_answer_lifecycle_through_the_ui(tmp_path):
    """Create a blocking aspect question, answer it the way the UI does, and check
    all three things that were broken in turn: the aspect is populated, the
    question stops being outstanding, and the workflow moves on.

    MP-000016 failed each of these in sequence. The answer never reached the
    aspects; once it did, publish kept re-opening the question against the frozen
    proposal, and the item collected twelve of them.
    """
    from resell.gateway import unresolved_blocking_questions
    from resell.webui import create_app
    from tests.test_webui import config_for

    config = config_for(tmp_path)
    conn = db.connect(config.db_path)
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=10000).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    drafted(conn, gateway, sku)
    gateway.ask_operator(
        sku, question="What is this item's Model?", aspect_name="Model",
    )
    question_id = conn.execute(
        "SELECT id FROM open_question WHERE sku = ? AND answered_at IS NULL", (sku,)
    ).fetchone()["id"]
    assert unresolved_blocking_questions(conn, sku)

    app = create_app(config=config)
    app.config["TESTING"] = True
    response = app.test_client().post(
        f"/questions/{question_id}/answer",
        data={"sku": sku, "answer": "DJI Osmo Action 5 Pro"},
    )
    assert response.status_code == 302

    # 1. the aspect is populated
    aspects = json.loads(conn.execute(
        "SELECT aspects FROM identification WHERE sku = ? AND superseded_at IS NULL",
        (sku,),
    ).fetchone()["aspects"])
    assert aspects["Model"] == ["DJI Osmo Action 5 Pro"]

    # 2. the question is no longer outstanding
    assert unresolved_blocking_questions(conn, sku) == []

    # 3. the workflow moves on rather than sitting on it
    assert next_step(conn, sku).step is not Step.ANSWER_QUESTIONS


def test_a_question_reopened_against_a_stale_proposal_is_not_outstanding(tmp_path):
    """Publish computes what is missing from the frozen listing, so an aspect
    supplied after the proposal looks missing for ever. The question it opens can
    never be satisfied by answering, so it must not gate the workflow."""
    from resell.gateway import unresolved_blocking_questions

    conn, gateway, sku = fixture(tmp_path)
    drafted(conn, gateway, sku)
    gateway.propose_identification(sku, aspects={"Brand": ["Bowflex"], "Model": ["552"]})

    gateway.ask_operator(
        sku, question="What is this item's Model?", aspect_name="Model",
    )

    assert unresolved_blocking_questions(conn, sku) == []
    assert next_step(conn, sku).step is not Step.ANSWER_QUESTIONS


def test_a_question_about_an_aspect_still_missing_does_gate(tmp_path):
    """The guard must not become a reason to ignore every question."""
    from resell.gateway import unresolved_blocking_questions

    conn, gateway, sku = fixture(tmp_path)
    drafted(conn, gateway, sku)
    gateway.ask_operator(
        sku, question="What is this item's Colour?", aspect_name="Colour",
    )

    assert len(unresolved_blocking_questions(conn, sku)) == 1
    assert next_step(conn, sku).step is Step.ANSWER_QUESTIONS


def test_a_question_about_no_aspect_still_gates(tmp_path):
    """Not every question is about an aspect, and those cannot be met by one."""
    from resell.gateway import unresolved_blocking_questions

    conn, gateway, sku = fixture(tmp_path)
    drafted(conn, gateway, sku)
    gateway.ask_operator(sku, question="Is the box included?")

    assert len(unresolved_blocking_questions(conn, sku)) == 1


def test_publish_does_not_reask_for_an_aspect_the_item_has():
    """The source of the twelve. `missing` is computed against the frozen listing;
    the question must be checked against what the item actually knows."""
    import inspect

    from resell.ebay.publisher import Publisher

    source = inspect.getsource(Publisher._open_aspect_questions)
    assert "known = self._live_aspects(sku)" in source
    assert "the proposal predates it" in source
