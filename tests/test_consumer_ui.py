"""The seller's screens, and the line between them and the operator's.

One backend, two presentations. The consumer routes are GET-only projections of
read models the operator UI already builds; every action on them posts to the
same endpoint the operator UI posts to. That is the property worth protecting:
two screens showing different words is a product choice, two screens computing
different answers is a second workflow, and the second one is always subtly
wrong.
"""

from __future__ import annotations

import json
import re

import pytest

from resell import db, views, views_consumer
from resell.domain import FeeModel
from resell.gateway import Gateway

# Words that describe how the system works rather than what the seller owns.
INTERNAL = (
    "comparab", "evidence", "citation", "taxonomy", "category_id", "condition_id",
    "aspect", "budget", "RESELL_", "comp_", "orchestrat", "identity_resolution",
    "search index", "derived_only", "next_step",
)


def fixture(tmp_path):
    from tests.test_webui import config_for

    config = config_for(tmp_path)
    conn = db.connect(config.db_path)
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    return config, conn, gateway


def with_item(gateway, conn, *, cost=1800):
    sku = gateway.ingest_item(purchase_cost_cents=cost).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    return sku


def client(config):
    from resell.webui import create_app

    app = create_app(config=config)
    app.config["TESTING"] = True
    return app.test_client()


def visible(body: str) -> str:
    """Text a person would read, with markup and URLs removed."""
    return re.sub(r"<[^>]+>", " ", body)


# --- the two UIs live side by side --------------------------------------------------


def test_the_consumer_owns_the_front_door(tmp_path):
    config, conn, gateway = fixture(tmp_path)
    c = client(config)
    assert c.get("/").status_code == 200
    assert c.get("/items").status_code == 200


def test_the_operator_ui_is_preserved_under_ops(tmp_path):
    """Still the place to diagnose an item. Moved, not replaced."""
    config, conn, gateway = fixture(tmp_path)
    c = client(config)
    for path in ("/ops", "/ops/inventory"):
        assert c.get(path).status_code == 200


def test_the_operator_ui_still_speaks_its_own_language(tmp_path):
    """The vocabulary the consumer must not see is exactly what makes the
    operator UI useful, so it stays there."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    body = client(config).get("/ops/inventory").get_data(as_text=True)
    assert sku in body
    assert "state" in body


# --- what the seller never sees ------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/items"])
def test_no_internal_vocabulary_reaches_the_seller(tmp_path, path):
    config, conn, gateway = fixture(tmp_path)
    with_item(gateway, conn)
    body = client(config).get(path).get_data(as_text=True)
    leaked = [word for word in INTERNAL if word in body]
    assert leaked == [], f"{path} shows {leaked}"


def test_the_sku_is_a_url_not_a_label(tmp_path):
    """It has to address the item; it must not be read as its name."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    body = client(config).get("/items").get_data(as_text=True)
    assert sku in body                       # in the href
    assert not re.search(r"MP-\d{6}", visible(body))


def test_an_unidentified_item_is_not_called_not_yet_identified(tmp_path):
    """The operator's placeholder is a status. To a seller it reads as a failure."""
    config, conn, gateway = fixture(tmp_path)
    with_item(gateway, conn)
    body = client(config).get("/").get_data(as_text=True)
    assert "not yet identified" not in body
    assert "Your new item" in body


def test_agent_steps_are_one_line_not_nineteen(tmp_path):
    """Ten of the orchestrator's steps are the agent talking to itself. Naming
    them reports progress against a plan the reader has no stake in."""
    from resell.orchestrator import Step

    config, conn, gateway = fixture(tmp_path)
    with_item(gateway, conn)
    body = visible(client(config).get("/").get_data(as_text=True))
    for step in Step:
        assert str(step) not in body, step


# --- the projection is a projection ---------------------------------------------------


def test_the_task_view_derives_everything_from_the_workflow_view(tmp_path):
    """No queries of its own. If it needed a fact the operator view lacks, the
    fix is to add it there -- not to grow a second source of truth."""
    import inspect

    source = inspect.getsource(views_consumer)
    for forbidden in ("conn.execute", "SELECT ", "next_step(", "import sqlite3"):
        assert forbidden not in source, forbidden


def test_the_moment_comes_from_the_step_the_orchestrator_chose(tmp_path):
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    workflow = views.workflow_view(
        conn, gateway, sku, marketplace="EBAY_US", environment="sandbox"
    )
    task = views_consumer.task_view(workflow)

    assert task.action == (workflow.step if workflow.waiting_on_operator else "")
    assert task.needs_you is workflow.waiting_on_operator


def test_every_operator_step_has_words_for_the_seller():
    """A step with no mapping would render blank, which is worse than jargon."""
    from resell.orchestrator import Step
    from resell.views_consumer import _MOMENTS, _WORKING_LINES

    for step in Step:
        assert str(step) in _MOMENTS or str(step) in _WORKING_LINES, step


# --- profit is the trade, not the processing ------------------------------------------


def test_profit_is_sale_minus_fees_minus_what_you_paid(tmp_path):
    from resell.views_consumer import shelf_rows

    class Row:
        sku, title, state, actor, step = "MP-1", "A thing", "listed", "nobody", "done"
        photo_count, purchase_cost_cents = 1, 2500
        approved_price_cents, listing_price_cents = 10000, None
        active_run = None

    rows = shelf_rows([Row()], net_of=lambda cents: cents - 1300)
    assert rows[0].profit_cents == 10000 - 1300 - 2500


def test_the_cost_of_running_the_agent_is_not_in_it(tmp_path):
    """It is a business overhead. Telling a seller their $40 profit is really
    $39.83 invites them to optimise the wrong thing."""
    import inspect

    from resell import views_consumer

    source = inspect.getsource(views_consumer)
    assert "ai_cost" not in source


def test_the_shelf_does_not_show_processing_cost(tmp_path):
    config, conn, gateway = fixture(tmp_path)
    with_item(gateway, conn)
    body = client(config).get("/items").get_data(as_text=True)
    assert "to run" not in body.lower()
    assert "micros" not in body.lower()


def test_the_operator_inventory_still_shows_it(tmp_path):
    """Kept where it is useful."""
    config, conn, gateway = fixture(tmp_path)
    with_item(gateway, conn)
    body = client(config).get("/ops/inventory").get_data(as_text=True)
    assert "run" in body.lower()


# --- one workflow, not two -------------------------------------------------------------


def test_the_consumer_routes_are_read_only():
    """Every action posts to the endpoint the operator UI already uses. A POST
    unique to the consumer UI would be the start of a second workflow."""
    from resell.webui import create_app
    from tests.test_webui import config_for
    import tempfile, pathlib

    app = create_app(config=config_for(pathlib.Path(tempfile.mkdtemp())))
    # By endpoint, not by path: `/items` answers GET with the shelf and POST with
    # `create_item`, and that POST is the shared action both UIs use. A path
    # carrying both is fine; a *view function* that mutates is not.
    consumer_views = {"home", "item", "shelf"}
    for rule in app.url_map.iter_rules():
        if rule.endpoint in consumer_views:
            assert set(rule.methods) <= {"GET", "HEAD", "OPTIONS"}, rule.endpoint


def test_the_seller_posts_to_the_same_places_the_operator_does():
    from pathlib import Path as _Path

    task = _Path("src/resell/webui/templates/consumer/_task.html").read_text()
    card = _Path("src/resell/webui/templates/_card.html").read_text()

    def endpoints(text):
        return set(re.findall(r"url_for\('(\w+)', sku=", text))

    shared = endpoints(task) & endpoints(card)
    assert {"confirm_identity", "approve_listing", "publish", "fix", "abandon"} <= shared
    # and nothing the consumer posts to is absent from the operator UI
    assert not (endpoints(task) - endpoints(card) - {"add_photos"})


# --- the safety surfaces are not softened ------------------------------------------------


def test_publishing_still_says_what_it_does():
    from pathlib import Path as _Path

    task = _Path("src/resell/webui/templates/consumer/_task.html").read_text()
    publish = task[task.index("'publish'"):task.index("'publish'") + 600]
    assert "eBay" in publish


def test_pricing_below_the_floor_still_needs_confirming():
    """The form moved into a partial shared by the two screens that need it."""
    from pathlib import Path as _Path

    form = _Path("src/resell/webui/templates/consumer/_set_price.html").read_text()
    assert "confirm_below_floor" in form
    assert "loses money" in form


def test_every_decision_screen_offers_a_decision():
    """"Choose a price" over empty space, with nothing to choose and no way
    forward, is the failure this guards. Every branch that asks for something has
    to render something."""
    from pathlib import Path as _Path

    task = _Path("src/resell/webui/templates/consumer/_task.html").read_text()
    # the price branch no longer depends on a price existing
    assert "task.action == 'approve_price' and task.has_price" in task
    assert "task.action == 'approve_price' %}" in task, (
        "a priced screen and a priceless one, and neither renders blank"
    )


def test_confirming_the_identity_is_still_its_own_screen():
    """A human checkpoint. Consumer wording, same gate."""
    from pathlib import Path as _Path

    task = _Path("src/resell/webui/templates/consumer/_task.html").read_text()
    assert "confirm_identity" in task


def test_the_header_has_one_way_home(tmp_path):
    """The brand is the way home. A "Next" link beside it went to the same place
    the word next to it already went."""
    config, conn, gateway = fixture(tmp_path)
    body = client(config).get("/").get_data(as_text=True)
    header = body[body.index("<header"):body.index("</header>")]
    assert header.count('href="/"') == 1
    assert ">Next<" not in header


# --- the screen must not report work that is not happening ---------------------------


def working_row(*, running):
    class Row:
        sku, title, state = "MP-1", "A teddy bear", "pricing"
        actor, step = "agent", "comp_research"
        photo_count, purchase_cost_cents = 1, None
        approved_price_cents = listing_price_cents = None
        active_run = "run_1" if running else None

    return views_consumer.shelf_rows([Row()], net_of=lambda c: c)[0]


def test_a_row_knows_whether_work_is_happening():
    """"Finding a price" is what the agent *would* do next, not proof that it is
    doing it. The spinner spun over an item nobody was working on."""
    assert working_row(running=True).running
    assert not working_row(running=False).running


def test_an_idle_item_is_waiting_not_working():
    row = working_row(running=False)
    assert row.waiting_to_start
    assert not working_row(running=True).waiting_to_start


def test_the_spinner_only_appears_while_something_runs():
    from pathlib import Path as _Path

    home = _Path("src/resell/webui/templates/consumer/home.html").read_text()
    section = home[home.index("c-working"):home.index("c-add")]
    assert "{% if row.running %}" in section
    assert "run-spinner" in section
    # and the idle case offers to start it rather than pretending it started
    assert "url_for('run', sku=row.sku)" in section
    assert "Ready to carry on" in section


def test_progress_appears_without_a_run_in_the_url(tmp_path):
    """Navigating to the home screen while the agent works used to show a line of
    text and no sign of movement: the panel only rendered if the button that
    started the run had put `?run=` in the URL."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        ("run_live", sku, "running", db.now_iso()),
    )
    conn.commit()

    body = client(config).get("/").get_data(as_text=True)
    assert 'data-run="run_live"' in body


def test_a_page_does_not_report_progress_on_an_item_it_is_not_showing(tmp_path):
    """Describing work on something the reader cannot see is worse than silence."""
    config, conn, gateway = fixture(tmp_path)
    shown = with_item(gateway, conn)
    hidden = with_item(gateway, conn)
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        ("run_elsewhere", hidden, "running", db.now_iso()),
    )
    conn.commit()

    body = client(config).get(f"/items/{shown}").get_data(as_text=True)
    assert "run_elsewhere" not in body


def test_deciding_a_comparable_hands_back_to_the_agent():
    """Every other decision starts a run. These two did not, which is how an item
    came to sit at an agent step with nothing running."""
    from pathlib import Path as _Path

    source = _Path("src/resell/webui/app.py").read_text()
    for name in ("accept_candidate", "reject_candidate"):
        body = source[source.index(f"def {name}("):]
        body = body[:body.index("\n    @app.")]
        assert "_start_agent(sku)" in body, name
