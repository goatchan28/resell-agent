"""Who owns the moment, and what the screen may offer while they do.

Two faults on MP-000014, one in each direction.

Accepting a comparable answered `a comp claim must cite the item evidence it
matched on`. The agent had offered listings it was not allowed to assess, with no
item citation, because it had made no match to cite -- and the accept button then
built a claim the validator rightly refused.

And "carry on" stayed on the screen while a run was in flight, inviting a second
advance through the same state machine: duplicated work, two budgets spent, and a
race with nothing to show for it.
"""

from __future__ import annotations

import json

import pytest

from resell import db, runs, store_pricing as sp, views
from resell.domain import FeeModel
from resell.gateway import Gateway
from resell.pricing.comps import CompBasis, CompObservation, ModelVisibility, PriceKind

NOW = __import__("datetime").datetime(2026, 8, 23, tzinfo=__import__("datetime").UTC)


def fixture(tmp_path):
    conn = db.connect(tmp_path / "own.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    for claim in ("a pair of adjustable dumbbells", "the dial reads 45"):
        conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
            "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
            (sku, "vision_observation", "fake/m", json.dumps({"claim": claim}),
             db.now_iso(), "visual_observation"),
        )
    conn.commit()
    return conn, gateway, sku


def withheld_candidate(conn, sku, comp_id="c-shop"):
    """What the agent offers for a listing it is not permitted to read into a
    prompt: no item citation, because it made no match."""
    sp.record_comp_observation(conn, CompObservation(
        comp_id=comp_id, marketplace="citywideshop.com", external_id="1",
        price_kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
        price_cents=39999, observed_at=NOW,
        title="Bowflex SelectTech 552 Adjustable Dumbbells (Pair)",
        model_visibility=ModelVisibility.DERIVED_ONLY,
    ))
    return sp.record_comp_candidate(
        conn, sku=sku, comp_id=comp_id,
        proposed_comparability="same_family_variant",
        comp_citations=("title", "price"),
        rationale="not assessed by the agent: citywideshop.com is not registered",
    )


# --- accepting a comparable always produces a valid claim --------------------------


def test_accepting_an_unassessed_comp_creates_a_claim(tmp_path):
    """The reported failure. It refused with "a comp claim must cite the item
    evidence it matched on"."""
    conn, gateway, sku = fixture(tmp_path)
    candidate_id = withheld_candidate(conn, sku)

    sp.accept_comp_candidate(conn, candidate_id, identity_resolution="unattempted")

    claims = list(conn.execute(
        "SELECT item_citations_json, rationale FROM comp_claim WHERE sku = ?", (sku,)
    ))
    assert len(claims) == 1
    assert json.loads(claims[0]["item_citations_json"]), "claim cites nothing"


def test_the_claim_cites_this_items_own_observations(tmp_path):
    """Traceable the same way an agent-judged claim is: the citations are real
    evidence rows belonging to this item, not filler."""
    conn, gateway, sku = fixture(tmp_path)
    candidate_id = withheld_candidate(conn, sku)
    sp.accept_comp_candidate(conn, candidate_id, identity_resolution="unattempted")

    cited = {int(i) for i in json.loads(conn.execute(
        "SELECT item_citations_json FROM comp_claim WHERE sku = ?", (sku,)
    ).fetchone()["item_citations_json"])}
    real = {r["id"] for r in conn.execute(
        "SELECT id FROM evidence WHERE sku = ? AND subject = 'this_item'", (sku,)
    )}
    assert cited and cited <= real


def test_the_claim_records_whose_assertion_it_is(tmp_path):
    """The agent did not judge this one. A claim that read as though it had would
    be the agent taking credit for the operator's decision."""
    conn, gateway, sku = fixture(tmp_path)
    candidate_id = withheld_candidate(conn, sku)
    sp.accept_comp_candidate(conn, candidate_id, identity_resolution="unattempted")

    rationale = conn.execute(
        "SELECT rationale FROM comp_claim WHERE sku = ?", (sku,)
    ).fetchone()["rationale"]
    assert "accepted by the operator" in rationale


def test_an_agent_judged_candidate_keeps_its_own_citations(tmp_path):
    """The fill-in must only apply where there is nothing to keep."""
    conn, gateway, sku = fixture(tmp_path)
    sp.record_comp_observation(conn, CompObservation(
        comp_id="c-judged", marketplace="ebay.com", external_id="2",
        price_kind=PriceKind.ASKING, basis=CompBasis.ACTIVE_SIMILAR,
        price_cents=25000, observed_at=NOW,
    ))
    ids = [str(r["id"]) for r in conn.execute(
        "SELECT id FROM evidence WHERE sku = ? ORDER BY id LIMIT 1", (sku,)
    )]
    candidate_id = sp.record_comp_candidate(
        conn, sku=sku, comp_id="c-judged",
        proposed_comparability="same_family_variant",
        item_citations=tuple(ids), comp_citations=("title",),
        rationale="same model line",
    )
    sp.accept_comp_candidate(conn, candidate_id, identity_resolution="unattempted")

    row = conn.execute(
        "SELECT item_citations_json, rationale FROM comp_claim WHERE sku = ?", (sku,)
    ).fetchone()
    assert json.loads(row["item_citations_json"]) == ids
    assert "accepted by the operator" not in row["rationale"]


def test_an_item_with_no_observations_is_refused_rather_than_filled(tmp_path):
    """Filling with an empty set would create exactly the untraceable claim the
    validator exists to stop."""
    conn = db.connect(tmp_path / "bare.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=100).sku
    candidate_id = withheld_candidate(conn, sku, comp_id="c-bare")

    with pytest.raises(ValueError, match="no observations to cite"):
        sp.accept_comp_candidate(conn, candidate_id, identity_resolution="unattempted")


# --- the agent owns the moment, so the screen offers nothing -----------------------


def running(conn, sku, run_id="run_open"):
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        (run_id, sku, "running", db.now_iso()),
    )
    conn.commit()
    return run_id


def test_a_card_shows_progress_not_actions_while_the_agent_works(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    running(conn, sku)

    card = views.workflow_view(
        conn, gateway, sku, marketplace="EBAY_US", environment="sandbox"
    )
    assert card.active_run == "run_open"
    assert card.agent_is_working


def test_an_operator_step_keeps_its_actions_even_with_a_run_finishing(tmp_path):
    """Ownership, not mere business. Hiding the operator's decision behind a
    spinner because a thread has not exited would strand them."""
    conn, gateway, sku = fixture(tmp_path)
    conn.execute(
        "INSERT INTO open_question (sku, question, blocking, asked_at, aspect_name) "
        "VALUES (?,?,1,?,?)",
        (sku, "What size?", db.now_iso(), "Size"),
    )
    conn.commit()
    running(conn, sku)

    card = views.workflow_view(
        conn, gateway, sku, marketplace="EBAY_US", environment="sandbox"
    )
    assert card.waiting_on_operator
    assert not card.agent_is_working


def test_the_template_hides_carry_on_while_the_agent_works():
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(money=lambda v: v, shorten=lambda v, n=80: v)
    env.globals["url_for"] = lambda n, **k: f"/{n}"

    class Card(dict):
        __getattr__ = dict.get

    def render(working):
        return env.get_template("_card.html").render(
            card=Card(sku="MP-1", step="draft", waiting_on_operator=False,
                      agent_is_working=working, active_run="run_1" if working else None,
                      can_search=False, title="x", photo_positions=(), questions=(),
                      candidates=(), summary="s", detail="d", state="identifying",
                      is_done=False, has_price=False, ai_cost_micros=0,
                      grant_calls=3, grant_lookups=3, floor_cents=0,
                      purchase_cost_cents=None),
            forms={},
        )

    busy = render(True)
    assert "the agent is working on this one" in busy
    assert "carry on" not in busy
    assert "/abandon" not in busy      # nor anything else that advances it

    idle = render(False)
    assert "carry on" in idle


def test_the_queue_row_shows_working_instead_of_run(tmp_path):
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(shorten=lambda v, n=80: v, money=lambda v: v, micros=lambda v: v)
    env.globals["url_for"] = lambda n, **k: f"/{n}"
    env.globals["get_flashed_messages"] = lambda **k: []

    class Row(dict):
        __getattr__ = dict.get

    out = env.get_template("home.html").render(
        cards=[], done=[], forms={}, run=None,
        working=[Row(sku="MP-8", title="x", summary="s", active_run="run_1")],
    )
    assert "working" in out
    assert ">run<" not in out


# --- and the backend does not depend on the screen ---------------------------------


def test_pressing_run_during_a_run_starts_nothing(tmp_path):
    """The half that matters. A hidden button is a convenience; this is the rule."""
    from resell.webui import create_app
    from tests.test_webui import config_for

    config = config_for(tmp_path)
    conn = db.connect(config.db_path)
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=100).sku
    running(conn, sku)

    app = create_app(config=config)
    app.config["TESTING"] = True
    client = app.test_client()
    for _ in range(5):
        response = client.post(f"/items/{sku}/run")
        assert "run=run_open" in response.headers["Location"]

    assert conn.execute(
        "SELECT COUNT(*) FROM agent_run WHERE sku = ?", (sku,)
    ).fetchone()[0] == 1


def test_no_duplicate_runs_even_from_different_routes(tmp_path):
    """Run, upload and every operator decision go through one helper, so they all
    join the run in flight rather than each starting one."""
    from resell.webui import create_app
    from tests.test_webui import config_for, png

    config = config_for(tmp_path)
    conn = db.connect(config.db_path)
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=100).sku
    running(conn, sku)

    app = create_app(config=config)
    app.config["TESTING"] = True
    client = app.test_client()
    client.post(f"/items/{sku}/run")
    client.post(f"/items/{sku}/photos", data={
        "photos": (__import__("io").BytesIO(png()), "b.png"),
    }, content_type="multipart/form-data")

    assert conn.execute(
        "SELECT COUNT(*) FROM agent_run WHERE sku = ?", (sku,)
    ).fetchone()[0] == 1


def test_the_backend_refuses_the_transition_not_just_the_button(tmp_path):
    """A hidden button is a convenience. Setting an item aside mid-run races the
    worker thread through the same state machine, so the rule has to hold for
    anything that reaches the route directly."""
    from resell.webui import create_app
    from tests.test_webui import config_for

    config = config_for(tmp_path)
    conn = db.connect(config.db_path)
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=100).sku
    running(conn, sku)

    app = create_app(config=config)
    app.config["TESTING"] = True
    app.test_client().post(f"/items/{sku}/abandon", data={})

    state = conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0]
    assert state != "abandoned", "an item was set aside out from under a running agent"


@pytest.mark.parametrize("route,payload", [
    ("abandon", {}),
    ("confirm-identity", {}),
    ("price", {"objective": "balanced"}),        # the price-approval route
    ("set-price", {"price": "450"}),
    ("more-research", {}),
])
def test_no_state_changing_route_acts_while_a_run_holds_the_item(tmp_path, route, payload):
    """The rule, applied to every route that can change an item -- not just the
    one that had a visible button."""
    from resell.webui import create_app
    from tests.test_webui import config_for

    config = config_for(tmp_path)
    conn = db.connect(config.db_path)
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=100).sku
    running(conn, sku)

    def snapshot():
        return (
            conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM events WHERE item_id = ?",
                         (sku,)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM price_proposal WHERE sku = ?",
                         (sku,)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM agent_run WHERE sku = ?",
                         (sku,)).fetchone()[0],
        )

    app = create_app(config=config)
    app.config["TESTING"] = True
    before = snapshot()
    response = app.test_client().post(f"/items/{sku}/{route}", data=payload)

    assert response.status_code == 302
    assert snapshot() == before, f"/{route} changed the item mid-run"


def test_every_state_changing_route_is_guarded():
    """Derived from the app, so a route added later is covered or fails here.

    The two exceptions are deliberate. `run` has its own join semantics -- it
    returns the run already in flight rather than refusing. `photos` adds evidence
    without transitioning anything, and wanting to add a picture while the agent
    looks at the others is reasonable.
    """
    import re
    from pathlib import Path as _Path

    source = _Path("src/resell/webui/app.py").read_text()
    joins_instead = {"run", "photos"}

    unguarded = {
        m.group(1)
        for m in re.finditer(
            r'@app\.post\("/items/<sku>/([^"]+)"\)\n    def \w+\(sku: str\):(.*?)'
            r'(?=\n    @app\.|\ndef )',
            source, re.S,
        )
        if "busy = _busy(sku)" not in m.group(2)
    }
    assert unguarded == joins_instead, f"unguarded: {unguarded - joins_instead}"
