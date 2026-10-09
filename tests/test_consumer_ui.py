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
import pathlib
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


CONSUMER = pathlib.Path("src/resell/webui/templates/consumer")


def consumer_templates() -> str:
    """Every consumer template, concatenated.

    Read as a set rather than by filename: the screens were one 155-line
    `_task.html` and are now eleven partials, and a test that names a file breaks
    on a refactor that changed nothing it was checking.
    """
    return "\n".join(p.read_text() for p in sorted(CONSUMER.glob("*.html")))


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
        first_photo_position = 1
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
    card = pathlib.Path("src/resell/webui/templates/_card.html").read_text()

    def endpoints(text):
        # Form actions only. A plain `url_for('item', ...)` is a link to a GET
        # page, and matching those made the shelf's own links look like writes.
        return set(re.findall(
            r"(?:form)?action=\"\{\{ url_for\('(\w+)', sku=", text
        ))

    consumer = endpoints(consumer_templates())
    shared = consumer & endpoints(card)
    assert {"confirm_identity", "approve_listing", "publish", "fix", "abandon"} <= shared
    # and nothing the consumer posts to is absent from the operator UI
    assert not (consumer - endpoints(card) - {"add_photos"})


# --- the safety surfaces are not softened ------------------------------------------------


def test_publishing_still_says_what_it_does():
    """The one irreversible action names the marketplace before it is pressed."""
    publish = (CONSUMER / "_publish.html").read_text()
    assert "eBay" in publish
    assert "url_for('publish'" in publish


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
    decision = (CONSUMER / "_decision.html").read_text()
    # the slider branch needs a price; the fallthrough does not
    assert "task.action == 'approve_price' and task.has_price" in decision
    assert "task.action in ('approve_price', 'price_without_comps')" in decision, (
        "a priced screen and a priceless one, and neither renders blank"
    )


def test_confirming_the_identity_is_still_its_own_screen():
    """A human checkpoint. Consumer wording, same gate."""
    assert "confirm_identity" in (CONSUMER / "_confirm.html").read_text()
    assert "confirm_identity" in (CONSUMER / "_decision.html").read_text()


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
        first_photo_position = 1
        approved_price_cents = listing_price_cents = None
        active_run = "run_1" if running else None

    return views_consumer.shelf_rows([Row()], net_of=lambda c: c)[0]


def test_the_thumbnail_asks_for_a_photo_that_exists(tmp_path):
    """Positions are attachment order and do not start at zero. The shelf asked
    for position 0 on every row, so an item whose photos start at 1 -- which is
    most of them -- rendered a broken image on its card."""
    config, conn, gateway = fixture(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=100).sku
    for position, digest in enumerate(("b" * 64, "c" * 64), start=1):
        gateway.attach_photo(
            sku, source_path=f"/p{position}.jpg", content_sha256=digest,
            image_format="jpeg", size_bytes=10, validation_errors=None,
        )
    rows = views.inventory(conn, marketplace="EBAY_US", environment="sandbox")
    row = next(r for r in rows if r.sku == sku)
    assert row.first_photo_position == 1
    assert views_consumer.shelf_rows([row], net_of=lambda c: c)[0].photo_position == 1


def test_an_item_with_no_photos_has_no_thumbnail(tmp_path):
    config, conn, gateway = fixture(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=100).sku
    rows = views.inventory(conn, marketplace="EBAY_US", environment="sandbox")
    row = next(r for r in rows if r.sku == sku)
    assert views_consumer.shelf_rows([row], net_of=lambda c: c)[0].photo_position is None


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
    """The workspace shows progress only when `task.working` is true, which comes
    from the orchestrator saying the agent owns the step. The old home screen
    listed other items and needed a per-row check; there are no other items now,
    so the guard is the branch itself."""
    workspace = (CONSUMER / "workspace.html").read_text()
    assert "{% elif task.working %}" in workspace
    assert "consumer/_progress.html" in workspace
    assert "run-spinner" in (CONSUMER / "_progress.html").read_text()


def test_progress_appears_without_a_run_in_the_url(tmp_path):
    """Navigating to the home screen while the agent works used to show a line of
    text and no sign of movement: the panel only rendered if the button that
    started the run had put `?run=` in the URL."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    # The client first: startup reconciliation treats any `running` row it
    # finds as the wreckage of a dead process, which is the only thing it can
    # be at startup. A live run belongs to a live app, in that order.
    live = client(config)
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        ("run_live", sku, "running", db.now_iso()),
    )
    conn.commit()

    body = live.get("/").get_data(as_text=True)
    # The run id reaches the page from the item's own state, not from the URL the
    # button happened to redirect to. It is what the poll asks about.
    assert "/runs/run_live" in body
    assert "run-spinner" in body


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


# --- one item at a time --------------------------------------------------------------


class Row:
    """A stand-in for `views.InventoryRow`, with only the fields `active_sku` reads."""

    def __init__(self, sku, state, created_at):
        self.sku, self.state, self.created_at = sku, state, created_at


def test_the_active_item_is_the_newest_one_still_in_flight():
    rows = [
        Row("MP-1", "listed", "2026-01-01T00:00:00+00:00"),
        Row("MP-2", "pricing", "2026-01-02T00:00:00+00:00"),
        Row("MP-3", "intake", "2026-01-03T00:00:00+00:00"),
    ]
    assert views_consumer.active_sku(rows) == "MP-3"


def test_a_listed_or_set_aside_item_is_not_active():
    """Both are finished, one on purpose. Either way the seller is not selling it
    now, and leaving it active would mean the front door never comes back."""
    rows = [
        Row("MP-1", "listed", "2026-01-09T00:00:00+00:00"),
        Row("MP-2", "abandoned", "2026-01-08T00:00:00+00:00"),
    ]
    assert views_consumer.active_sku(rows) is None


def test_the_front_door_is_the_upload_when_nothing_is_active(tmp_path):
    config, conn, gateway = fixture(tmp_path)
    body = client(config).get("/").get_data(as_text=True)
    assert "Take or choose photos" in body
    assert 'accept="image/*"' in body


def test_the_picker_does_not_force_the_camera():
    """`capture` sends iOS Safari straight to the camera, takes exactly one
    photo, and ignores `multiple` -- so the first version allowed a single shot
    and offered no way into the photo library. Without it, Safari's own sheet
    offers Photo Library, Take Photo and Choose File."""
    picker = (CONSUMER / "_photo_picker.html").read_text()
    markup = picker[picker.index("<form"):]
    assert "capture" not in markup
    assert "multiple" in markup
    assert "data-accumulate" in picker


def test_photos_accumulate_instead_of_replacing():
    """Safari returns a fresh FileList each time the picker closes, so taking
    three photos one at a time would otherwise leave one."""
    base = (CONSUMER / "base.html").read_text()
    assert "DataTransfer" in base
    assert "kept.get(input)" in base
    # and re-picking the same photo from the library must not attach it twice
    assert "file.name" in base and "file.size" in base


def test_nothing_uploads_before_the_seller_says_so():
    """A listing has several photos. Submitting on the first selection is what
    made that impossible."""
    picker = (CONSUMER / "_photo_picker.html").read_text()
    assert "requestSubmit" not in picker
    assert "data-autosubmit" not in (CONSUMER / "base.html").read_text()


def test_the_front_door_asks_what_it_cost(tmp_path):
    """The one fact no photograph can contain, asked at the only moment the
    seller reliably knows it."""
    config, conn, gateway = fixture(tmp_path)
    body = client(config).get("/").get_data(as_text=True)
    assert 'name="cost_dollars"' in body
    assert "optional" in body.lower()


def test_the_upload_disappears_while_an_item_is_active(tmp_path):
    """Strictly one at a time. A second upload control is the start of a queue,
    and a queue is the thing the consumer UI does not have."""
    config, conn, gateway = fixture(tmp_path)
    with_item(gateway, conn)
    body = client(config).get("/").get_data(as_text=True)
    assert "create_item" not in body
    assert "Take photos" not in body or "add_photos" in body


def test_the_home_screen_shows_one_item_and_never_two(tmp_path):
    config, conn, gateway = fixture(tmp_path)
    first = with_item(gateway, conn)
    second = with_item(gateway, conn)
    body = client(config).get("/").get_data(as_text=True)
    assert second in body
    assert first not in body, "the older item belongs under Your items, not here"


# --- the price slider is a control, not a new decision --------------------------------


def test_the_slider_posts_an_objective_and_not_a_price():
    """A continuous slider would have to post cents through `set_price`, which
    records operator judgement and no comparable evidence -- quietly stripping the
    basis off every price that had one. Three stops, same endpoint as the
    operator's three buttons."""
    price = (CONSUMER / "_price.html").read_text()
    form = price[price.index('id="price-form"'):price.index("</form>")]
    assert "url_for('approve_price'" in price
    assert 'name="objective"' in form
    assert "url_for('set_price'" not in form, "the slider never posts a number"


def test_typing_a_price_is_still_the_operator_judgement_path():
    """Wherever it is reached from. It is the same partial on all three screens,
    so there is one implementation of "the seller overruled the evidence"."""
    entry = (CONSUMER / "_set_price.html").read_text()
    assert "url_for('set_price'" in entry
    assert "confirm_below_floor" in entry
    assert 'include "consumer/_set_price.html"' in (CONSUMER / "_price.html").read_text()


# --- a price the seller can disagree with ---------------------------------------------


def test_a_researched_price_can_always_be_overruled():
    """The slider is three positions in the agent's evidence, and a seller who
    knows something the agent does not had no way to say so without abandoning
    the item. Reachable from the priced screen, and never the default."""
    price = (CONSUMER / "_price.html").read_text()
    assert 'id="price-own-toggle"' in price
    assert "Set your own price" in price
    assert 'id="price-own" hidden' in price, "offered, not preselected"
    assert "c-big-button" not in price[price.index("price-own-toggle"):
                                       price.index("price-own-toggle") + 200], (
        "a quiet way out, not a second primary action"
    )


def test_changing_your_mind_does_not_cost_you_the_researched_price():
    price = (CONSUMER / "_price.html").read_text()
    assert 'id="price-own-cancel"' in price
    assert "Use the suggested price instead" in price


def test_hiding_a_flex_region_actually_hides_it():
    """`display: flex` beats the user-agent `[hidden]` rule, so both price
    regions rendered at once -- two "Use this price" buttons on one screen."""
    css = CSS.read_text()
    assert ".c-fill[hidden] { display: none; }" in css
    assert ".c-own-price[hidden] { display: none; }" in css


# --- three stops are only three stops when they are three prices ----------------------


def test_identical_stops_do_not_render_as_a_slider():
    """The floor can clamp fast, balanced and hold-out onto one number. Dragging
    a slider and watching the amount not move reads as a broken screen."""
    from resell.views_consumer import TaskView

    same = views_consumer.price_stops(
        [_Option("fast_sale", 1200), _Option("balanced", 1200),
         _Option("max_proceeds", 1200)]
    )
    spread = views_consumer.price_stops(
        [_Option("fast_sale", 1000), _Option("balanced", 1500),
         _Option("max_proceeds", 2000)]
    )
    def screen(options):
        return TaskView(
            sku="MP-1", name="A thing", photo_positions=(), headline="Choose a price",
            hint="", needs_you=True, working=False, action="approve_price",
            price_options=options,
        )

    assert not screen(same).price_is_a_range
    assert screen(spread).price_is_a_range


def test_the_one_price_case_still_offers_the_decision():
    """No slider and no sentence about why. The amount, the button, and the way
    out are the whole screen -- explaining the absent control would be more copy
    than the control was worth."""
    price = (CONSUMER / "_price.html").read_text()
    assert "task.price_is_a_range" in price
    assert "id=\"price-own-toggle\"" in price


# --- how much the price is worth trusting ---------------------------------------------


def test_a_confident_price_says_nothing():
    """Silence is the claim of confidence. A hedge under every price teaches the
    seller to skip the line, which costs the warning that matters."""
    assert views_consumer.price_confidence(()) == ""
    assert views_consumer.price_confidence(("adjusted", "identity_resolved")) == ""


def test_the_hedge_is_at_most_two_sentences():
    """One about how much evidence there was, one about what kind it was. A
    phone screen that opens with four lines of grey reads as an apology."""
    everything = (
        "retail_anchored", "anchor_blended", "single_comp", "thin_sample",
        "wide_dispersion", "identity_unresolved", "asking_only",
    )
    said = views_consumer.price_confidence(everything)
    assert said.count(".") == 2, said
    assert len(said) < 160, said


def test_the_two_sentences_are_different_facts():
    """"Thin" and "asks rather than sales" are independent, and collapsing them
    would drop whichever came second."""
    both = views_consumer.price_confidence(("single_comp", "asking_only"))
    assert "one similar listing" in both
    assert "asking prices" in both


def test_the_strongest_limitation_is_the_one_said():
    """Ordered, not accumulated: a price worked back from retail is not also
    described as resting on one listing, because the listing is not what it
    rests on."""
    said = views_consumer.price_confidence(
        ("retail_anchored", "single_comp", "thin_sample", "wide_dispersion")
    )
    assert said == ("Nothing like this is for sale right now, so this works "
                    "back from what it costs new.")


def test_the_seller_is_never_shown_the_qualifier_names():
    from resell.views_consumer import _HOW_MUCH, _WHAT_KIND

    for flag, sentence in _HOW_MUCH + _WHAT_KIND:
        assert flag not in sentence, flag
        for word in INTERNAL:
            assert word not in sentence.lower(), (flag, word)


def test_the_qualifiers_are_still_carried_even_though_nothing_shows_them():
    """The estimator's judgement about its own evidence stays on the projection.
    Parsing the operator's prose note to recover the same facts would be a
    second, lossier copy of a decision already made explicitly."""
    from resell.views import WorkflowView

    assert "price_qualifiers" in WorkflowView.__annotations__
    view = WorkflowView(
        sku="MP-1", state="s", title="t", photo_positions=(), step="approve_price",
        actor="operator", summary="", detail="", waiting_on_operator=True,
        price_qualifiers=("single_comp", "asking_only"),
    )
    assert view.price_qualifiers == ("single_comp", "asking_only")
    assert "one similar listing" in views_consumer.price_confidence(
        view.price_qualifiers)


def test_the_price_screen_explains_nothing(tmp_path):
    """Not the evidence, not the strategies, not the absent slider. Three labels
    communicate the tradeoff and "Set your own price" covers disagreeing."""
    price = (CONSUMER / "_price.html").read_text()
    body = re.sub(r"\{#.*?#\}", "", price, flags=re.S)
    body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    for gone in ("price_confidence", "c-note", "nothing to choose between",
                 "asking prices", "similar listing", "costs new"):
        assert gone not in body, gone


def test_no_seller_facing_evidence_copy_survives_anywhere():
    """It was only ever on the slider, and it should not reappear by being
    quietly moved to the review screen or the done screen."""
    for name in ("_price.html", "_set_price.html", "_review.html", "_done.html"):
        body = re.sub(r"\{#.*?#\}", "", (CONSUMER / name).read_text(), flags=re.S)
        assert "price_confidence" not in body, name


def test_every_qualifier_the_seller_is_told_about_is_a_real_one():
    """A sentence keyed on a qualifier that no longer exists is a warning that
    silently stops appearing."""
    from resell.pricing.estimate import PriceQualifier
    from resell.views_consumer import _HOW_MUCH, _WHAT_KIND

    known = {str(q) for q in PriceQualifier}
    for flag, _ in _HOW_MUCH + _WHAT_KIND:
        assert flag in known, flag


def test_every_stop_is_labelled():
    """`PriceOption` carries no label, and the previous screen rendered
    `option.label` -- so every price button had a blank sub-line."""
    stops = views_consumer.price_stops([
        _Option("fast_sale", 1000), _Option("balanced", 1500),
        _Option("max_proceeds", 2000),
    ])
    assert [s.label for s in stops] == ["Sell quickly", "Balanced", "Aggressive"]
    # cheapest first, because that is where a slider starts
    assert [s.price_cents for s in stops] == [1000, 1500, 2000]


class _Option:
    def __init__(self, objective, price_cents):
        self.objective, self.price_cents = objective, price_cents
        self.net_proceeds_cents, self.tradeoff, self.is_default = 0, "", False


# --- nobody has to sit and watch ------------------------------------------------------


def test_the_working_screen_says_you_can_walk_away():
    """Research takes a couple of minutes and the screen gave no sign of that,
    so the honest reading was "keep looking at this"."""
    progress = (CONSUMER / "_progress.html").read_text()
    assert "No need to wait" in progress
    assert "come back later" in progress


def working_screen(tmp_path, *, active_run):
    """The progress template, rendered. Asserting against the source would pass
    on a sentence sitting in the wrong branch."""
    from resell.views_consumer import Phase, TaskView

    config, _, _ = fixture(tmp_path)
    app = _app(config)
    task = TaskView(
        sku="MP-1", name="A thing", photo_positions=(1,), headline="", hint="",
        needs_you=False, working=True, action="", active_run=active_run,
        phases=(Phase(label="Identifying your item", done=True, current=False),
                Phase(label="Finding market prices", done=False, current=True),
                Phase(label="Preparing your listing", done=False, current=False)),
    )
    with app.test_request_context("/"):
        html = app.jinja_env.get_template("consumer/_progress.html").render(task=task)
    import html as html_mod

    html = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    # Unescaped, because the tick and the empty circle are numeric entities and
    # a test looking for digits would find them instead of a clock.
    return re.sub(r"\s+", " ",
                  html_mod.unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _rendered(tmp_path, *, active_run):
    """The raw HTML, for the markup-level checks that `working_screen` strips."""
    from resell.views_consumer import Phase, TaskView

    config, _, _ = fixture(tmp_path)
    app = _app(config)
    task = TaskView(
        sku="MP-1", name="A thing", photo_positions=(1,), headline="", hint="",
        needs_you=False, working=True, action="", active_run=active_run,
        phases=(Phase(label="Finding market prices", done=False, current=True),),
    )
    with app.test_request_context("/"):
        return app.jinja_env.get_template("consumer/_progress.html").render(task=task)


def _app(config):
    from resell.webui import create_app

    app = create_app(config=config)
    app.config["TESTING"] = True
    return app


def test_it_is_said_only_while_something_is_actually_running(tmp_path):
    """The other branch of this template is the agent's turn with nobody taking
    it -- a died or abandoned run. "Come back later" there is a promise nobody
    is keeping."""
    assert "No need to wait" in working_screen(tmp_path, active_run="run_a")
    idle = working_screen(tmp_path, active_run=None)
    assert "No need to wait" not in idle
    assert "Carry on" in idle, "the idle branch still offers the only useful action"


def test_a_screen_waiting_on_the_seller_never_says_it():
    """It belongs to the agent working. The sentence lives in `_progress.html`,
    and `workspace.html` reaches that template only on `task.working` -- so no
    screen that is asking the seller something can render it."""
    workspace = (CONSUMER / "workspace.html").read_text()
    assert "{% elif task.working %}" in workspace
    branch = workspace[workspace.index("{% elif task.working %}"):]
    assert branch[:branch.index("{% else %}")].count("_progress.html") == 1
    for name in ("_decision.html", "_answer.html", "_confirm.html", "_price.html",
                 "_review.html", "_publish.html", "_done.html", "_hero.html"):
        assert "No need to wait" not in (CONSUMER / name).read_text(), name


def test_it_is_emphasised_without_being_an_alarm():
    """It has to compete with a number that ticks, so it is full ink and the
    first three words are bold. Still no border, no background and no red: it is
    good news, and a panel with a rule round it reads as something gone wrong."""
    css = CSS.read_text()
    block = css[css.index(".c-wait {"):css.index("}", css.index(".c-wait {"))]
    assert "var(--ink)" in block and "var(--muted)" not in block
    for alarm in ("border", "background", "var(--err)"):
        assert alarm not in block, alarm
    progress = (CONSUMER / "_progress.html").read_text()
    assert "<strong>No need to wait</strong>" in progress


def test_the_clock_says_the_run_is_alive_and_nothing_more(tmp_path):
    """Restored deliberately. It is the only signal that separates a normal
    two-minute run from one that has been going for ten, and there is no other
    way to notice the second. What it must never become is a prediction."""
    progress = (CONSUMER / "_progress.html").read_text()
    assert "c-elapsed" in progress
    assert "setInterval" in progress
    assert "clearInterval(clock)" in progress, "stopped when the run finishes"
    # Against what is on screen, not against the source -- the source explains
    # what this must not become, and saying so uses the words.
    said = working_screen(tmp_path, active_run="run_a").lower()
    for forbidden in ("remaining", "estimated", "eta", "left", "%", "of 3"):
        assert forbidden not in said, forbidden
    # The reload poll stays: a seller who did stay should not be left looking at
    # a run that has already finished.
    assert "run.running" in progress


def test_the_clock_measures_the_run_and_not_the_page(tmp_path):
    """A clock that restarts at zero on every reload hides the one thing this is
    for: a run at ten minutes is worth noticing, and it is only noticeable if the
    number survives opening the page late."""
    progress = (CONSUMER / "_progress.html").read_text()
    assert "Date.parse(run.started_at)" in progress
    assert "var started = null" in progress, "blank until the run record answers"

    from tests.test_webui import seeded

    app, conn, _, sku = seeded(tmp_path)
    client = app.test_client()
    response = client.post(f"/items/{sku}/run", follow_redirects=False)
    run_id = re.search(r"run=(run_\w+)", response.headers["Location"]).group(1)
    payload = client.get(f"/runs/{run_id}",
                         headers={"Accept": "application/json"}).get_json()
    assert "started_at" in payload
    # Parseable by `Date.parse` without guessing a zone: `now_iso` writes the
    # offset, and a naive stamp would be read as local time and be hours out.
    assert re.search(r"[+-]\d\d:\d\d$|Z$", payload["started_at"]), payload


def test_the_clock_does_not_use_the_last_steps_elapsed(tmp_path):
    """`elapsed_ms` is the last *recorded step's* elapsed, so it stands still
    through a long step. Syncing to it would make the clock tick backwards."""
    progress = (CONSUMER / "_progress.html").read_text()
    assert "elapsed_ms" not in progress

    from resell import db
    from tests.test_webui import seeded

    app, conn, _, sku = seeded(tmp_path)
    # A run with no steps recorded against it, written directly rather than raced
    # against a live worker. The claim is about what the endpoint reports when
    # there is nothing to report, and starting a real run to observe that made the
    # assertion a coin toss on how far the background thread had got.
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        ("run_stepless", sku, "running", db.now_iso()),
    )
    conn.commit()
    payload = app.test_client().get(
        "/runs/run_stepless", headers={"Accept": "application/json"}).get_json()
    assert payload["elapsed_ms"] == 0, "nothing recorded yet, and it shows"


def test_the_clock_is_quieter_than_the_phase_it_sits_beside():
    """A pulse, not a focal point."""
    css = CSS.read_text()
    block = css[css.index(".c-elapsed {"):css.index("}", css.index(".c-elapsed {"))]
    assert "var(--muted)" in block
    assert "font-size: .88rem" in block


def test_the_clock_appears_only_while_a_run_is_in_flight(tmp_path):
    assert "c-elapsed" in _rendered(tmp_path, active_run="run_a")
    assert "c-elapsed" not in _rendered(tmp_path, active_run=None)


def test_the_status_screen_gained_no_other_words():
    """One sentence. Not a second status line, not a step name, not a host."""
    body = re.sub(r"\{#.*?#\}", "",
                  (CONSUMER / "_progress.html").read_text(), flags=re.S)
    body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    prose = re.findall(r">([A-Za-z][^<>{}]{12,})<", body)
    assert len(prose) == 1, prose


# --- the checklist ticks forwards -----------------------------------------------------


def test_the_phases_only_ever_tick_forwards():
    """Drafting runs before pricing begins. Filing it under the listing phase
    would tick line three, then untick it while line two was still running."""
    order = ["observe", "draft", "comp_research", "approve_price", "propose_listing"]
    reached = [
        sum(1 for phase in views_consumer.phases_for(step) if phase.done)
        for step in order
    ]
    assert reached == sorted(reached), reached
    assert reached[0] == 0 and reached[-1] == 2


def test_a_finished_item_has_finished_every_phase():
    phases = views_consumer.phases_for("done", is_done=True)
    assert all(phase.done for phase in phases)
    assert len(phases) == 3


def test_every_step_the_orchestrator_has_lands_in_a_phase():
    """A step outside the mapping renders a checklist with nothing current on it,
    which reads as stalled."""
    from resell.orchestrator import Step

    mapped = {step for _, steps in views_consumer.PHASES for step in steps}
    missing = {str(s) for s in Step} - mapped - {"done"}
    assert not missing, missing


# --- what the seller reads after an action --------------------------------------------


def test_the_sku_does_not_reach_the_seller_through_a_flash(tmp_path):
    """The vocabulary test only covers GET pages, and flashes appear after a POST
    redirect. "MP-000001: set aside from intake ... find it under Inventory,
    showing abandoned" was reaching the seller's screen."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    body = client(config).post(
        f"/items/{sku}/abandon", follow_redirects=True
    ).get_data(as_text=True)
    assert sku not in body
    assert "Inventory" not in body
    # And no green bar telling them it worked: setting an item aside lands on
    # Your items with the item sitting there, which says it better.
    assert "flash-ok" not in body


def test_success_is_not_announced_to_the_seller():
    """The screen has already moved to the next thing and is showing it, so a
    bar saying the last thing worked describes what the reader can see."""
    assert views_consumer.seller_flash("priced at $45.00", "ok") is None
    assert views_consumer.seller_flash("published as v1|2|0", "ok") is None
    # everything that wants attention still comes through
    assert views_consumer.seller_flash("a price has to be more than nothing",
                                       "error")
    assert views_consumer.seller_flash("that is below what it cost you", "ask")
    assert views_consumer.seller_flash("refused: publish", "reason")


def test_the_operator_still_gets_every_message(tmp_path):
    """`/ops` reads the flashes the handlers actually wrote. Only the consumer
    projection filters, and only for its own screens."""
    ops = pathlib.Path("src/resell/webui/templates/base.html").read_text()
    assert "get_flashed_messages" in ops
    assert "seller_flashes" not in ops


def test_a_refusal_is_never_silently_swallowed():
    """Only environment-variable instructions are dropped. A message that
    vanishes is a safety gate the seller cannot see."""
    assert views_consumer.seller_flash("refused: publish") == "refused: publish"
    assert views_consumer.seller_flash("a price has to be more than nothing")
    assert views_consumer.seller_flash(
        "raise it with RESELL_BUDGET_COMP_RESEARCH_MAX_CALLS"
    ) is None


# --- built for a phone ----------------------------------------------------------------


def test_no_table_reaches_the_seller():
    assert "<table" not in consumer_templates()


def test_the_answer_form_submits_exactly_one_answer():
    """The chips and the fallback text box share the name `answer`. A hidden but
    *enabled* text box submits an empty second value under that name, and the
    route would have two where the operator form guarantees one."""
    form = (CONSUMER / "_answer.html").read_text()
    other = form[form.index('class="c-other c-pinned"'):]
    assert "disabled" in other[:other.index("</div>")]


def test_the_seller_never_reads_ebays_taxonomy(tmp_path):
    """The operator's answer form names the marketplace's value list, its count,
    and an override. All three are facts about eBay's taxonomy."""
    form = re.sub(r"\{#.*?#\}", "", (CONSUMER / "_answer.html").read_text(),
                  flags=re.S)
    rendered = form[form.index("{% if question.choices %}"):]
    for phrase in ("eBay lists", "value(s)", "overriding"):
        assert phrase not in rendered, phrase


def test_the_stylesheet_is_written_for_a_phone_first():
    css = pathlib.Path("src/resell/webui/static/consumer.css").read_text()
    assert "@media (min-width:" in css, "widen for desktop, not narrow for phones"
    assert "safe-area-inset" in css, "the home indicator covers a bottom button"
    assert "--tap: 44px" in css


def test_a_stalled_item_offers_to_carry_on_rather_than_spinning(tmp_path):
    """`task.working` means the agent owns the step, not that anything is
    running. An item whose run died is the agent's turn with nobody taking it,
    and spinning at someone over work that is not happening is worse than saying
    so."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    body = client(config).get("/").get_data(as_text=True)
    assert "Carry on" in body
    assert "run-spinner" not in body


def test_the_research_stage_never_reaches_the_progress_area(tmp_path):
    """The operator's run panel prints the current step and any problems --
    "Reading marketplace listing…", "bestbuy.com timed out". Useful when
    diagnosing a stall, and exactly the detail this screen exists to keep out."""
    progress = (CONSUMER / "_progress.html").read_text()
    workspace = (CONSUMER / "workspace.html").read_text()
    assert '"_run.html"' not in workspace
    assert "run.current" not in progress
    assert "run.problems" not in progress


def test_several_photos_and_a_cost_arrive_together(tmp_path):
    """One POST carries the whole intake: every photo the seller picked, and the
    one fact no photograph contains."""
    import io

    config, conn, gateway = fixture(tmp_path)
    # Distinct bytes per shot: the gateway rejects a photo whose sha256 it has
    # already seen, so three identical files would attach as one.
    shots = [
        (io.BytesIO(b"\xff\xd8\xff\xe0" + bytes([n]) * 200), f"shot{n}.jpg")
        for n in range(3)
    ]
    client(config).post(
        "/items",
        data={"photos": shots, "cost_dollars": "24.50"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    sku = conn.execute("SELECT sku FROM item ORDER BY created_at DESC").fetchone()[0]
    attached = conn.execute(
        "SELECT COUNT(*) FROM photo WHERE sku = ?", (sku,)
    ).fetchone()[0]
    assert attached == 3
    cost = conn.execute(
        "SELECT purchase_cost_cents FROM item WHERE sku = ?", (sku,)
    ).fetchone()[0]
    assert cost == 2450


# --- an unwritten field is not a fault ------------------------------------------------


def test_an_empty_title_is_not_reported_as_a_problem(tmp_path):
    """The bug behind "something needs fixing" appearing while the agent was
    still writing the listing. Nothing was wrong: drafting had not run."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    gateway.propose_identification(sku, title="", description="")
    form = views.correction_form(conn, sku)
    assert not form.has_problem
    title = next(f for f in form.fields if f.name == "title")
    assert "not written one yet" in title.help_text


def test_a_title_that_cannot_be_published_is_still_a_problem(tmp_path):
    """The distinction the fix turns on: empty is a state, too long is a fault."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    gateway.propose_identification(sku, title="x" * 90, description="d")
    form = views.correction_form(conn, sku)
    assert form.has_problem
    assert "over eBay's 80" in form.problems[0]


def test_nothing_is_flagged_while_the_agent_has_the_item():
    """Everything in the sheet posts behind the `_busy` guard, so announcing a
    problem mid-run offers an action the server refuses."""
    more = (CONSUMER / "_more.html").read_text()
    assert "form.has_problem and task.needs_you" in more
    workspace = (CONSUMER / "workspace.html").read_text()
    assert "not task.active_run" in workspace


# --- uploads cannot overwrite each other ----------------------------------------------


def test_ten_photos_from_a_phone_all_survive(tmp_path):
    """A full listing's worth, the way Safari sends them: ten distinct captures
    under one filename. Every one has to end up its own file, with the digest on
    the record matching the bytes on disk -- that pair is what the publisher
    checks before it uploads anything to eBay."""
    import hashlib
    import io

    config, conn, gateway = fixture(tmp_path)
    shots = [
        (io.BytesIO(b"\xff\xd8\xff\xe0" + bytes([n]) * (300 + n)), "image.jpg")
        for n in range(10)
    ]
    client(config).post(
        "/items", data={"photos": shots},
        content_type="multipart/form-data", follow_redirects=True,
    )
    sku = conn.execute("SELECT sku FROM item ORDER BY created_at DESC").fetchone()[0]
    rows = conn.execute(
        "SELECT position, source_path, content_sha256 FROM photo WHERE sku = ? "
        "ORDER BY position", (sku,)
    ).fetchall()

    assert len(rows) == 10
    assert len({r["source_path"] for r in rows}) == 10
    assert len({r["content_sha256"] for r in rows}) == 10
    for row in rows:
        path = pathlib.Path(row["source_path"])
        assert path.exists(), path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["content_sha256"]


def test_the_stored_extension_describes_the_bytes(tmp_path):
    """Safari calls a HEIC capture `image.jpg`. Trusting that name put HEIC bytes
    behind a .jpg extension, and `send_file` guesses Content-Type from it."""
    import io

    config, conn, gateway = fixture(tmp_path)
    # A PNG signature, sent under the name a phone camera would use.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(40)
    client(config).post(
        "/items", data={"photos": [(io.BytesIO(png), "image.jpg")]},
        content_type="multipart/form-data", follow_redirects=True,
    )
    sku = conn.execute("SELECT sku FROM item ORDER BY created_at DESC").fetchone()[0]
    row = conn.execute(
        "SELECT source_path, image_format FROM photo WHERE sku = ?", (sku,)
    ).fetchone()
    assert row["image_format"] == "png"
    assert pathlib.Path(row["source_path"]).suffix == ".png"


def test_photos_with_the_same_filename_do_not_overwrite_each_other(tmp_path):
    """MP-000028. iOS names every camera capture `image.jpg`, so three shots
    taken in one go arrived under one name and each save overwrote the last:
    three rows in the photo table, one file on disk, and the digests recorded
    for the first two describing bytes that were gone. Publish caught it -- the
    integrity check is exactly the right last line of defence -- but by then the
    photographs were unrecoverable."""
    import hashlib
    import io

    config, conn, gateway = fixture(tmp_path)
    shots = [
        (io.BytesIO(b"\xff\xd8\xff\xe0" + bytes([n]) * 300), "image.jpg")
        for n in range(3)
    ]
    client(config).post(
        "/items",
        data={"photos": shots},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    sku = conn.execute("SELECT sku FROM item ORDER BY created_at DESC").fetchone()[0]
    rows = conn.execute(
        "SELECT position, source_path, content_sha256 FROM photo WHERE sku = ? "
        "ORDER BY position", (sku,)
    ).fetchall()

    assert len(rows) == 3
    assert len({r["source_path"] for r in rows}) == 3, "one file per photo"
    for row in rows:
        path = pathlib.Path(row["source_path"])
        assert path.exists(), path
        # The check the publisher runs before it uploads anything.
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["content_sha256"]


def test_the_same_photo_twice_is_not_stored_twice(tmp_path):
    """Content-addressed names make a re-upload land on the file it already
    matches, and the gateway refuses the duplicate row."""
    import io

    config, conn, gateway = fixture(tmp_path)
    same = b"\xff\xd8\xff\xe0" + b"z" * 300
    client(config).post(
        "/items",
        data={"photos": [(io.BytesIO(same), "image.jpg"),
                         (io.BytesIO(same), "IMG_0001.JPG")]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    sku = conn.execute("SELECT sku FROM item ORDER BY created_at DESC").fetchone()[0]
    assert conn.execute(
        "SELECT COUNT(*) FROM photo WHERE sku = ?", (sku,)
    ).fetchone()[0] == 1
    directory = pathlib.Path(config.db_path).parent / "uploads" / sku
    kept = [p for p in directory.iterdir() if not p.name.startswith(".incoming-")]
    assert len(kept) == 1
    assert not [p for p in directory.iterdir() if p.name.startswith(".incoming-")]


# --- a finished item leads to the listing ---------------------------------------------


def test_the_listing_url_follows_the_environment():
    """One definition, shared with the CLI, so the two cannot disagree about
    which host the sandbox is on."""
    assert views.listing_url("110590236927", environment="sandbox") == (
        "https://www.sandbox.ebay.com/itm/110590236927"
    )
    assert views.listing_url("110590236927", environment="production") == (
        "https://www.ebay.com/itm/110590236927"
    )
    assert views.listing_url(None, environment="sandbox") == ""


def test_the_finished_card_opens_the_listing():
    done = (CONSUMER / "_done.html").read_text()
    assert "task.listing_url" in done
    assert 'rel="noopener"' in done
    # and it stays a plain card when there is nothing to open
    assert "{% else %}" in done


def test_a_published_item_carries_its_listing_url(tmp_path):
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    conn.execute(
        "INSERT INTO listing (sku, marketplace, environment, listing_id, active, "
        "currency, shipping_terms, created_at, updated_at) "
        "VALUES (?,?,?,?,1,'USD','seller_paid',?,?)",
        (sku, "EBAY_US", "sandbox", "110590236927", db.now_iso(), db.now_iso()),
    )
    conn.commit()
    view = views.workflow_view(
        conn, gateway, sku, marketplace="EBAY_US", environment="sandbox"
    )
    assert views_consumer.task_view(view).listing_url.endswith("/itm/110590236927")


# --- one screen, no page scrolling ----------------------------------------------------

CSS = pathlib.Path("src/resell/webui/static/consumer.css")


def test_the_sell_flow_is_exactly_one_viewport(tmp_path):
    """`svh` and not `dvh`: `svh` is the viewport with Safari's chrome fully
    expanded, so a layout that fits it can never be cut off, and it does not
    reflow while the chrome animates under a thumb."""
    css = CSS.read_text()
    assert "body.c-fixed" in css
    assert "height: 100svh" in css
    assert "height: 100vh" in css, "the fallback for WebKit without svh"
    assert "overflow: hidden" in css.split("body.c-fixed")[1][:300]
    # Comments stripped: the reasoning for choosing svh over dvh names dvh, and
    # explaining a decision is not making the opposite one.
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "dvh" not in rules, "svh is the guarantee; dvh reflows"


def test_the_workspace_and_the_front_door_do_not_scroll(tmp_path):
    config, conn, gateway = fixture(tmp_path)
    assert 'class="c-fixed"' in client(config).get("/").get_data(as_text=True)
    sku = with_item(gateway, conn)
    assert 'class="c-fixed"' in client(config).get(f"/items/{sku}").get_data(as_text=True)


def test_your_items_is_the_one_page_that_scrolls(tmp_path):
    """An inventory is a list, and a list may be longer than a screen."""
    config, conn, gateway = fixture(tmp_path)
    body = client(config).get("/items").get_data(as_text=True)
    assert 'class="c-scrolls"' in body
    assert 'class="c-fixed"' not in body


def test_unbounded_content_scrolls_inside_its_own_card():
    """The four screens whose content has no upper bound. Each keeps its action
    outside the scrolling region, so the way to answer is never what scrolls
    away."""
    for name, region in (
        ("_answer.html", "c-chips c-scroll"),      # eBay lists 204 model values
        ("_comps.html", '<div class="c-scroll">'),   # any number of candidates
    ):
        assert region in (CONSUMER / name).read_text(), name
    # Review Listing is deliberately not in that list: it is the one workflow
    # screen that scrolls as a page, because it is the last look at what goes to
    # eBay rather than a question to answer.
    assert "c-scroll" not in (CONSUMER / "_review.html").read_text()
    # the More sheet caps its own body rather than the disclosure element: a
    # `<details>` lays opened content out through an anonymous box, so a height
    # set on the element never reaches the content
    assert "max-height: 34svh" in CSS.read_text()


def test_the_action_is_never_inside_the_scrolling_region():
    for name in ("_price.html", "_set_price.html", "_confirm.html", "_publish.html"):
        text = (CONSUMER / name).read_text()
        assert "c-pinned" in text or "c-actions" in text, name


def test_touch_targets_survive_the_squeeze():
    """Fitting a screen must not be accomplished by shrinking what a thumb has
    to hit. Nothing in the short-viewport rules touches the tap floor."""
    css = CSS.read_text()
    assert "--tap: 44px" in css
    short = css[css.index("@media (max-height: 700px)"):]
    assert "--tap:" not in short, "the tap floor is not a variable to trade away"
    assert "min-height: 54px" in css, "the primary button keeps its size"


def test_a_decision_screen_has_no_photograph():
    """It was cropped to fit and told the seller nothing they needed to decide
    with, while taking the room the decision wanted. It stays where it earns its
    place: while the agent works, and on the finished item."""
    css = CSS.read_text()
    assert "clamp(180px, 47svh, 420px)" in css, "the working size"
    assert "is-compact" not in css, "the second size is gone, not just unused"
    workspace = (CONSUMER / "workspace.html").read_text()
    assert "{% if task.working or task.is_done %}" in workspace
    assert "is-compact" not in workspace


def test_the_photo_yields_before_the_card_does():
    """A stated floor, not `auto`. An automatic minimum measures a flex item by
    its specified height, so `auto` read this box's minimum as the photo's full
    210px, refused to shrink it, and squeezed the checklist to 15px instead."""
    css = CSS.read_text()
    item = css[css.index(".c-item {"):]
    assert "flex: 0 20 auto" in item[:900]
    assert "min-height: 116px" in item[:900]


# --- uploads are shrunk before they go over the wire ----------------------------------


def test_photos_are_shrunk_in_the_browser_before_upload():
    """A library pick is 5712x4284 and about 5 MB; the same photo taken through
    the control comes back 4032x3024 and under 2 MB. Four of the former is
    17.6 MB on the wire, and the server's share of that request is 10 ms -- all
    of the rest is a phone pushing bytes over Wi-Fi.

    2048px on the longest edge clears both consumers of those pixels: the model
    reads 1568px and eBay wants 1600px for zoom. Measured on the operator's own
    batch it turns 17.6 MB into 1.8 MB."""
    base = (CONSUMER / "base.html").read_text()
    assert "MAX_EDGE = 2048" in base
    assert "1568" in base and "1600" in base, "the two numbers that set the floor"


def test_the_resize_keeps_a_portrait_photo_upright():
    """A canvas draw ignores EXIF rotation, so every upright photo would arrive
    on its side. Where the option is unavailable the original is sent instead --
    a slow upload beats a sideways photograph."""
    base = (CONSUMER / "base.html").read_text()
    assert 'imageOrientation: "from-image"' in base
    assert 'typeof createImageBitmap !== "function"' in base, "the bail-out"
    assert ".catch(function () { return file; })" in base, "any failure sends the original"


def test_a_resize_that_does_not_help_is_not_done():
    base = (CONSUMER / "base.html").read_text()
    assert "SKIP_UNDER" in base, "small files are left alone"
    assert "blob.size >= file.size" in base, "never send more bytes than we started with"


def test_a_2048px_photo_still_passes_validation(tmp_path):
    """The point of 2048 is that nothing downstream notices. eBay's recommended
    minimum longest side is 500px."""
    from resell.images import RECOMMENDED_MIN_LONGEST_SIDE

    assert RECOMMENDED_MIN_LONGEST_SIDE <= 2048


def test_re_picking_the_same_photo_is_still_caught_after_shrinking():
    """The duplicate check has to run on the file as chosen. Shrinking changes
    its size, so a key taken afterwards would never match the one before."""
    base = (CONSUMER / "base.html").read_text()
    assert "f.pickedKey = k" in base
    assert "f.originalKey || keyOf(f)" in base


def test_there_is_exactly_one_photo_picker_handler():
    """A stale copy of the previous handler survived an edit and ran alongside
    the new one: two `kept` maps, two writes to `input.files`, and which set of
    photos actually got sent came down to listener order."""
    base = (CONSUMER / "base.html").read_text()
    assert base.count("input[type=file][data-accumulate]") == 1
    assert base.count("Preparing photos") == 1


# --- the simplification pass ----------------------------------------------------------


def test_review_listing_is_the_one_workflow_screen_that_scrolls(tmp_path):
    """Everywhere else the card is a fixed frame. This is the last look at what
    goes to eBay, and squeezing it into an internal box made the final check
    feel like another small form to clear."""
    workspace = (CONSUMER / "workspace.html").read_text()
    assert "'c-scrolls c-review' if task and task.action == 'approve_listing'" in workspace


def test_the_review_carousel_shows_whole_photographs():
    """`contain`, not `cover`. Every other photograph in the product is cropped
    to fill its box because there it is decoration; here it is the thing being
    checked."""
    review = (CONSUMER / "_review.html").read_text()
    assert "c-carousel" in review
    assert "for position in task.photo_positions" in review, "all of them, not the first"
    css = CSS.read_text()
    slide = css[css.index(".c-slide img"):]
    assert "object-fit: contain" in slide[:200]
    assert "scroll-snap-type: x mandatory" in css


def test_the_review_screen_carries_the_whole_listing(tmp_path):
    """Title, price, condition, description and the approval, in one place."""
    review = (CONSUMER / "_review.html").read_text()
    for part in ("task.name", "approved_price_cents", "task.condition_label",
                 "task.listing_description", "url_for('approve_listing'"):
        assert part in review, part


def test_condition_reaches_the_consumer_view(tmp_path):
    """A read-model addition, not a rule: the id is on the identification and
    the label comes from the catalogue publishing already validates against."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    gateway.propose_identification(sku, title="A thing", condition_id="3000")
    view = views.workflow_view(
        conn, gateway, sku, marketplace="EBAY_US", environment="sandbox"
    )
    assert views_consumer.task_view(view).condition_label == "Used"


def test_an_unknown_condition_is_shown_as_nothing():
    assert views.condition_label(None) == ""
    assert views.condition_label("") == ""
    assert views.condition_label("nonsense") == ""
    assert views.condition_label("1000") == "New"


def test_the_publish_screen_says_each_thing_once():
    """It used to read "Put it up for sale" as a heading, "this lists it on
    eBay" as a sentence, "this puts it on eBay for $64.04" as another, and then
    "Put it up for sale" again on the button."""
    publish = (CONSUMER / "_publish.html").read_text()
    body = re.sub(r"\{#.*?#\}", "", publish, flags=re.S)
    assert body.count("Put it up for sale") == 0, "the heading already says it"
    assert body.count("eBay") == 1
    assert views_consumer._MOMENTS["publish"] == ("Put it up for sale", "")


def test_the_price_screen_drops_the_sentence_under_each_price():
    """"A fair price that still moves" says what "Balanced" says."""
    price = (CONSUMER / "_price.html").read_text()
    assert "tradeoff" not in re.sub(r"\{#.*?#\}", "", price, flags=re.S)
    assert "c-range-labels" in price, "the three labels stay"


def test_progress_states_its_status_once():
    """A headline reading "Finding a price" sat directly above a checklist row
    reading "Finding market prices"."""
    progress = (CONSUMER / "_progress.html").read_text()
    assert "task.headline" not in progress
    assert "run-spinner" in progress, "the spinner moved onto the active row"


def test_the_front_door_does_not_narrate_the_button():
    # Comments stripped: the reason the sentence went names the sentence.
    hero = re.sub(r"\{#.*?#\}", "", (CONSUMER / "_hero.html").read_text(), flags=re.S)
    assert "Take a few photos" not in hero
    assert "Take or choose photos" in hero, "the button still says it"


# --- a keyboard is part of the viewport too -------------------------------------------


def test_the_shell_follows_the_keyboard():
    """MP-000034 question 30. The answer was never recorded, and the POST works
    fine -- it never left the browser. `100svh` is the viewport with the
    browser's chrome expanded, and that is all it knows about: iOS does not
    resize the layout viewport for the software keyboard, it covers the bottom
    of the page with it. Measured, that put Continue 45px below the fold on a
    page whose whole point is that it does not scroll."""
    css = CSS.read_text()
    assert "height: var(--shell, 100svh)" in css
    base = (CONSUMER / "base.html").read_text()
    assert "window.visualViewport" in base
    assert 'root.style.setProperty("--shell"' in base
    # and it only engages when something is genuinely covering the page, or it
    # would fight Safari's own chrome animation -- the reason this uses svh
    assert "covered > 120" in base


def test_the_answer_box_and_its_button_never_scroll_away():
    """The prompt gives ground; the box you type in and the button that sends it
    do not."""
    answer = (CONSUMER / "_answer.html").read_text()
    assert '<div class="c-scroll"><p class="c-question-text">' in answer
    assert 'class="c-typed c-pinned"' in answer


def test_a_form_keeps_its_content_floor():
    """`min-height: 0` on the form removed its content floor, so it shrank to
    62px around a 118px input-and-button, which spilled and was clipped by the
    card. `auto` is right for both branches: a child with `overflow: auto`
    contributes nothing to it, so the chip list still gives ground."""
    css = CSS.read_text()
    start = css.index(".c-ask > form")
    rule = css[start:css.index("}", start)]
    assert "min-height: 0" not in rule, rule


def test_a_question_does_not_offer_to_fix_a_title_that_is_not_written(tmp_path):
    """The sheet exists to correct a title and a description, and the agent asks
    its questions before it has written either."""
    workspace = (CONSUMER / "workspace.html").read_text()
    assert "task.action != 'answer_questions'" in workspace
    app = pathlib.Path("src/resell/webui/app.py").read_text()
    # and the form is not built either: building it fetches eBay's condition
    # list, which was a network round trip for a form nobody would see
    assert 'form=_correction_form(sku) if shows_more else None' in app


def test_the_spinner_is_a_circle():
    """`.run-spinner` is a bare span, and a span is inline: width and height did
    nothing, so all that rendered was a 2px border on a zero-sized box. It used
    to sit directly in a flex row, where being a flex item made it block."""
    css = CSS.read_text()
    rule = css[css.index(".c-tick .run-spinner"):]
    assert "display: block" in rule[:160]
    assert "width: 15px" in rule[:160] and "height: 15px" in rule[:160]


# --- the double-tap guard was eating the answer ---------------------------------------


def test_the_submit_guard_carries_the_pressed_button_value():
    """The chips are `<button name="answer" value="...">`, so the button that
    was pressed *is* where the answer lives. A form's entry list is built after
    the submit event and excludes disabled controls, so disabling the submitter
    for the double-tap guard silently dropped it: tapping a chip posted no
    answer at all. Typing one worked, because that value comes from an <input>
    and the guard only touches buttons."""
    base = (CONSUMER / "base.html").read_text()
    guard = base[base.index('document.addEventListener("submit"'):]
    assert "event.submitter" in guard
    assert "data-submitter-value" in guard
    # carried before anything is disabled, or it is the same bug again
    assert guard.index("form.appendChild(carried)") < guard.index("b.disabled = true")


def test_a_chip_is_the_submitter():
    """Which is why the guard could eat it."""
    answer = (CONSUMER / "_answer.html").read_text()
    chip = answer[answer.index("c-chips"):]
    assert 'name="answer" value="{{ value }}"' in chip
    assert 'type="submit"' in chip


def test_the_carried_value_is_not_duplicated_on_a_second_tap():
    base = (CONSUMER / "base.html").read_text()
    assert "!form.querySelector('[data-submitter-value]')" in base


def test_a_chip_answer_is_accepted_end_to_end(tmp_path):
    """The server half, which was never the problem -- pinned so it stays that
    way."""
    config, conn, gateway = fixture(tmp_path)
    sku = with_item(gateway, conn)
    conn.execute(
        "INSERT INTO open_question (sku, question, why_it_matters, blocking, "
        "asked_at, aspect_name) VALUES (?,?,?,1,?,?)",
        (sku, "What model is this?", "required aspect Model is unsupported",
         db.now_iso(), "Model"),
    )
    conn.commit()
    qid = conn.execute(
        "SELECT id FROM open_question WHERE sku = ?", (sku,)
    ).fetchone()[0]
    client(config).post(f"/questions/{qid}/answer",
                        data={"sku": sku, "answer": "Terra"}, follow_redirects=True)
    assert conn.execute(
        "SELECT answer FROM open_question WHERE id = ?", (qid,)
    ).fetchone()[0] == "Terra"


# --- the card takes the screen --------------------------------------------------------


def test_the_column_does_not_collapse_to_its_content():
    """`.c-main` is a flex item of the shell, and an `auto` margin on a stretched
    flex item takes the free space and collapses the item to content width. With
    no photograph to hold it open the whole column shrank to 243px of a 375px
    phone."""
    css = CSS.read_text()
    rule = css[css.index("\n.c-main {"):]
    rule = rule[:rule.index("}")]
    assert "width: 100%" in rule
    assert "max-width: 34rem" in rule


def test_a_decision_card_is_as_tall_as_its_contents():
    """Stretching it to fill the screen made a three-line card look like a
    mistake -- slabs of white between the heading, the number and the button. It
    is content-sized and centred; what made centring look wrong the first time
    was the column collapsing to 243px wide, which is a different fault."""
    css = CSS.read_text()
    rule = css[css.index("body.c-fixed .c-main:not(:has(.c-item))"):]
    rule = rule[:rule.index("}") + 1]
    assert "justify-content: center" in rule
    assert "flex: 1 1 auto" not in rule


# --- "None of these" is a choice, not an escape hatch ---------------------------------


def test_none_of_these_is_one_of_the_chips():
    """It was a checkbox under the row, and a friend using this did not realise
    there was another way to answer when nothing on the list matched."""
    answer = (CONSUMER / "_answer.html").read_text()
    chips = answer[answer.index('class="c-chips'):answer.index('class="c-other')]
    assert "None of these" in chips, "in the row, not below it"
    assert 'class="c-chip c-chip-other"' in chips, "same shape as the others"


def test_choosing_it_asks_plainly_for_the_answer():
    answer = (CONSUMER / "_answer.html").read_text()
    assert "| ask_typed" in answer, "names the aspect over the box"
    assert 'name="answer"' in answer
    assert views_consumer.question_entry_label(
        _Question(aspect_name="Material")) == "Enter the Material"
    assert views_consumer.question_entry_label(
        _Question(aspect_name=None)) == "Enter the correct answer"


def test_no_screen_explains_the_taxonomy_to_the_seller():
    """The caveats are gone as well as the jargon. Whether a value is a
    suggestion or a bound choice is a fact about eBay's schema; the seller finds
    out by being told an answer was not accepted, which is one message instead of
    a warning on every attempt, most of which are fine."""
    answer = re.sub(r"\{#.*?#\}", "", (CONSUMER / "_answer.html").read_text(),
                    flags=re.S)
    for jargon in ("FREE_TEXT", "free_text", "taxonomy", "allowed values",
                   "override", "value_not_listed and I am"):
        assert jargon not in answer, jargon
    for caveat in ("Those were suggestions", "may not be accepted",
                   "fixed set of choices"):
        assert caveat not in answer, caveat


def test_a_constrained_aspect_keeps_its_validation():
    """Where the marketplace binds the list, the flag still goes on the record and
    the gateway still checks -- the wording changed, the rule did not."""
    answer = (CONSUMER / "_answer.html").read_text()
    assert "{% if question.allowed_values %}" in answer
    assert 'name="value_not_listed"' in answer


class _Question:
    """The two fields the consumer wording is derived from."""

    def __init__(self, *, aspect_name, choices=(), question="the operator's words"):
        self.aspect_name, self.choices, self.question = aspect_name, choices, question


# --- the question screen says what to do, and nothing else ----------------------------


def test_the_prompt_is_built_from_the_aspect_name():
    ask = views_consumer.question_prompt
    assert ask(_Question(aspect_name="Department", choices=("Men", "Women"))) == (
        "Pick the Department or choose None of these."
    )
    assert ask(_Question(aspect_name="Material", choices=("Nylon",))) == (
        "Pick the Material or choose None of these."
    )
    assert ask(_Question(aspect_name="Model")) == "Enter the Model."


def test_the_operators_diagnostic_wording_is_never_shown_to_the_seller():
    """MP-000041's real questions. Every one explains why the identification run
    could not settle the aspect, which is what /ops is for and is three lines of
    preamble in front of the object."""
    real = (
        "Nothing observed supports a value for Material. Can you supply it, or "
        "point to where on the item it appears?",
        "Style has no truthful value among this category's allowed options. This "
        "is a question about the category, not the item: forcing a least-wrong "
        "value here is exactly the failure to avoid.",
        "Size was partly observed but not enough to name a value. A closer photo "
        "of the relevant detail may settle it.",
    )
    for aspect, operator_words in zip(("Material", "Style", "Size"), real):
        said = views_consumer.question_prompt(
            _Question(aspect_name=aspect, choices=("a", "b"), question=operator_words))
        assert said == f"Pick the {aspect} or choose None of these."
        assert operator_words not in said


def test_the_record_keeps_the_diagnostic_wording():
    """Derived for display, never written back. /ops reads `q.question` and has
    to keep finding the sentence the stage actually wrote."""
    import inspect

    source = inspect.getsource(views_consumer.question_prompt)
    assert "question.question" in source, "read, not rewritten"
    ops = (pathlib.Path("src/resell/webui/templates") / "_card.html").read_text()
    assert "| ask" not in ops, "the operator card keeps the stage's own words"


def test_a_question_with_no_aspect_falls_back_to_what_was_asked():
    """A free-form question somebody wrote by hand has nothing to derive from,
    and inventing a prompt for it would lose the only wording there is."""
    hand_written = _Question(aspect_name=None, question="Which of the two boxes?")
    assert views_consumer.question_prompt(hand_written) == "Which of the two boxes?"


# --- how many are left --------------------------------------------------------------


@pytest.mark.parametrize("remaining,expected", [
    (4, "We need 4 more details"),
    (3, "We need 3 more details"),
    (2, "We need 2 more details"),
    (1, "We need 1 more detail"),
])
def test_the_heading_counts_what_is_left(remaining, expected):
    """Numerals, because it is a quantity: a seller deciding whether to finish
    now or later is reading it as one."""
    assert views_consumer.details_headline(remaining) == expected


def test_the_count_is_derived_and_decrements_by_itself():
    """No new state. `blocking_questions` is already the unanswered ones, so
    answering one shortens the list and the heading follows."""
    from resell.views import WorkflowView

    def heading(n):
        view = WorkflowView(
            sku="MP-1", state="s", title="t", photo_positions=(),
            step="answer_questions", actor="operator", summary="", detail="",
            waiting_on_operator=True,
            questions=tuple(_Question(aspect_name="Color") for _ in range(n)),
        )
        return views_consumer.task_view(view).headline

    assert heading(3) == "We need 3 more details"
    assert heading(2) == "We need 2 more details"
    assert heading(1) == "We need 1 more detail"


def test_the_count_is_not_repeated_under_the_chips():
    """It used to say "One more after this" below the form as well. The heading
    carries it now, and a screen this small has room for the fact once."""
    answer = re.sub(r"\{#.*?#\}", "", (CONSUMER / "_answer.html").read_text(),
                    flags=re.S)
    assert "One more after this" not in answer


def test_the_reveal_still_guarantees_one_answer_field():
    """The hidden box stays disabled until it is the one being used, or a tapped
    chip would post its value alongside an empty second one."""
    answer = (CONSUMER / "_answer.html").read_text()
    other = answer[answer.index('class="c-other'):]
    assert "disabled" in other[:other.index("</div>")]
    base = (CONSUMER / "base.html").read_text()
    assert "field.disabled = false" in base


def test_the_front_door_asks_for_the_tag():
    """A tag is where the brand, model, material and size are written down --
    MP-000041's owner was asked for four of those."""
    # Wrapping collapsed: the sentence is one line in the browser and two in an
    # 88-column file.
    hero = re.sub(r"\s+", " ", (CONSUMER / "_hero.html").read_text())
    assert "Include a photo of the tag or label if there is one." in hero
    # one sentence, not a checklist
    assert hero.count('<p class="c-hero-note"') == 1


# --- an interrupted upload does not become an error page ----------------------
#
# A phone moving from Wi-Fi to cellular changes IP and every open connection dies
# with it. As a plain form post that means the browser navigates to its own error
# page and takes the FileList with it, so the seller re-picks every photograph
# and uploads the same bytes again. Reproducible, not our error, and not our page.


def test_the_picker_has_somewhere_to_report_a_failed_upload():
    picker = (CONSUMER / "_photo_picker.html").read_text()
    assert "data-upload-problem" in picker
    assert "aria-live" in picker, "a screen reader should hear it without focus moving"
    assert "hidden" in picker, "silent until something goes wrong"


def test_the_upload_is_sent_without_leaving_the_page():
    base = (CONSUMER / "base.html").read_text()
    assert "form.c-picker" in base
    assert "new FormData(form)" in base
    assert "xhr.open(\"POST\", form.getAttribute(\"action\")" in base, (
        "the same server route, unchanged"
    )


def test_a_network_failure_offers_retry_rather_than_navigating():
    base = (CONSUMER / "base.html").read_text()
    error_block = base[base.index('xhr.addEventListener("error"'):]
    assert "window.location" not in error_block.split("})")[0], (
        "an interrupted upload must not navigate anywhere"
    )
    assert "Retry" in base
    assert "your photos are still here" in base


def test_the_upload_reports_real_progress():
    """These take tens of seconds on a phone; the button used to say "Uploading…"
    whether it was moving or stalled."""
    base = (CONSUMER / "base.html").read_text()
    assert "xhr.upload.addEventListener(\"progress\"" in base
    assert "lengthComputable" in base


def test_a_browser_without_xhr_keeps_the_plain_form():
    """Progressive enhancement: no JS, no regression."""
    base = (CONSUMER / "base.html").read_text()
    assert "if (!window.XMLHttpRequest || !window.FormData) { return; }" in base
    picker = (CONSUMER / "_photo_picker.html").read_text()
    assert 'method="post"' in picker and 'action="{{ action }}"' in picker
    assert 'enctype="multipart/form-data"' in picker


def test_a_double_tap_cannot_start_two_uploads():
    base = (CONSUMER / "base.html").read_text()
    assert 'form.dataset.uploading === "1"' in base


def test_too_large_is_told_apart_from_interrupted():
    """413 is a decision about the request; a dead connection is not."""
    base = (CONSUMER / "base.html").read_text()
    assert "xhr.status === 413" in base
    assert "too large" in base


def test_the_failure_message_is_styled():
    css = CSS.read_text()
    assert ".c-upload-problem" in css
