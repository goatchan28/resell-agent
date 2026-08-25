"""Five family members, one database, one eBay account, five separate shelves.

The private beta's whole safety story, exercised through the real routes rather
than through the helpers underneath them -- because every problem this project
has had recently lived at a seam and not in a function. Nothing here calls
`views.inventory` or `access.email_for_request` directly; it posts multipart
photo uploads to `/items` with a Cloudflare Access header on the request and
reads back the HTML, which is what a phone does.

What is deliberately *not* claimed. This is not multi-tenancy and there is no
security boundary around the data: one Sandbox seller account, one SQLite file,
and /ops sees everything on purpose. `owner_email` decides whose consumer screens
an item appears on. The thing being protected is that five testers can use the
app at once without seeing, answering or publishing each other's items -- which
is a product property, and it is the one the beta stands or falls on.
"""

from __future__ import annotations

import io
import threading

import pytest

from resell import db, runs

TESTERS = (
    "ana@example.test",
    "ben@example.test",
    "cass@example.test",
    "dev@example.test",
    "eli@example.test",
)
OPERATOR = "owner@example.test"


@pytest.fixture(autouse=True)
def _beta_env(monkeypatch):
    """No ambient identity: every request in this file carries its own header.

    The suite-wide fixture in conftest hands everyone an admin address, which is
    right for tests about other things and would hide every question this file
    asks.
    """
    monkeypatch.delenv("RESELL_DEV_EMAIL", raising=False)
    monkeypatch.setenv("RESELL_ADMIN_EMAILS", OPERATOR)


def beta_app(tmp_path):
    from resell.webui import create_app
    from tests.test_webui import config_for

    config = config_for(tmp_path)
    app = create_app(config=config)
    app.config["TESTING"] = True
    return app, db.connect(config.db_path)


def as_user(app, email):
    """A client that carries one tester's Access header on every request."""
    client = app.test_client()
    client.environ_base["HTTP_CF_ACCESS_AUTHENTICATED_USER_EMAIL"] = email
    return client


def upload(client, *, cost="12.00"):
    """The real intake: a multipart POST to `/items`, as the phone sends it."""
    from tests.test_webui import png

    response = client.post(
        "/items",
        data={"cost_dollars": cost,
              "photos": (io.BytesIO(png()), "photo.png")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302, response.status_code
    return response.headers["Location"].split("/items/")[1].split("?")[0]


def five_shelves(tmp_path):
    """One item each, uploaded by five different authenticated addresses."""
    app, conn = beta_app(tmp_path)
    clients = {email: as_user(app, email) for email in TESTERS}
    skus = {email: upload(client) for email, client in clients.items()}
    return app, conn, clients, skus


# --- identity ------------------------------------------------------------------------


def test_no_email_no_app(tmp_path):
    """Fails closed. An anonymous shelf is one misconfigured tunnel away from
    being the only shelf, and the failure would look like the product working."""
    app, _ = beta_app(tmp_path)
    assert app.test_client().get("/").status_code == 403


def test_the_address_is_case_insensitive(tmp_path):
    """Two spellings of one tester must not become two shelves."""
    app, _ = beta_app(tmp_path)
    sku = upload(as_user(app, "Ana@Example.test"))
    body = as_user(app, "ana@example.TEST").get("/items").get_data(as_text=True)
    assert sku in body


# --- five shelves, five items ---------------------------------------------------------


def test_each_tester_sees_only_their_own_item(tmp_path):
    _, _, clients, skus = five_shelves(tmp_path)
    for email, client in clients.items():
        shelf = client.get("/items").get_data(as_text=True)
        assert skus[email] in shelf
        for other, sku in skus.items():
            if other != email:
                assert sku not in shelf, f"{email} can see {other}'s item"


def test_each_tester_gets_their_own_active_item(tmp_path):
    """The reason this exists. Before ownership, `active_sku` was the most
    recently created live item across the whole database -- so the fifth upload
    became everybody's home screen and the other four could not start anything."""
    _, _, clients, skus = five_shelves(tmp_path)
    for email, client in clients.items():
        home = client.get("/").get_data(as_text=True)
        assert skus[email] in home
        for other, sku in skus.items():
            if other != email:
                assert sku not in home


def test_uploading_is_not_blocked_by_somebody_elses_item(tmp_path):
    """Five people uploading at once is the beta. If one tester's active item
    hides the upload control for the rest, there is no beta."""
    app, _, clients, skus = five_shelves(tmp_path)
    for email, client in clients.items():
        # Each already has a live item, so their own hero is hidden -- and that
        # is the rule working, not the bug. The bug was somebody *else's* item
        # doing it.
        assert skus[email] in client.get("/").get_data(as_text=True)
    fresh = as_user(app, "new@example.test")
    assert "Sell something" in fresh.get("/").get_data(as_text=True)


# --- nothing crosses between shelves --------------------------------------------------


@pytest.mark.parametrize("path", [
    "/items/{sku}",
    "/items/{sku}/photo/1",
])
def test_cross_user_reads_are_not_found(tmp_path, path):
    """404 rather than 403: a 403 confirms the SKU is real, and SKUs are
    sequential."""
    _, _, clients, skus = five_shelves(tmp_path)
    intruder = clients["ben@example.test"]
    target = skus["ana@example.test"]
    assert intruder.get(path.format(sku=target)).status_code == 404


def test_a_photo_is_not_readable_by_url_alone(tmp_path):
    """The one that would be worst to get wrong: the photos are of people's
    houses."""
    _, _, clients, skus = five_shelves(tmp_path)
    own = clients["ana@example.test"].get(f"/items/{skus['ana@example.test']}/photo/1")
    assert own.status_code == 200 and own.data
    for email, client in clients.items():
        if email == "ana@example.test":
            continue
        assert client.get(
            f"/items/{skus['ana@example.test']}/photo/1"
        ).status_code == 404, email


@pytest.mark.parametrize("route,payload", [
    ("run", {}),
    ("abandon", {}),
    ("confirm-identity", {}),
    ("set-price", {"price": "40"}),
    ("more-research", {}),
    ("approve-listing", {}),
    ("publish", {}),
    ("photos", {}),
])
def test_cross_user_writes_are_refused(tmp_path, route, payload):
    _, conn, clients, skus = five_shelves(tmp_path)
    target = skus["ana@example.test"]
    before = conn.execute(
        "SELECT state, updated_at FROM item WHERE sku = ?", (target,)
    ).fetchone()
    response = clients["ben@example.test"].post(f"/items/{target}/{route}", data=payload)
    assert response.status_code == 404, route
    after = conn.execute(
        "SELECT state, updated_at FROM item WHERE sku = ?", (target,)
    ).fetchone()
    assert tuple(after) == tuple(before), f"/{route} changed somebody else's item"


def test_a_question_cannot_be_answered_by_the_wrong_person(tmp_path):
    """The answer route takes its SKU from the form, so it needs its own check --
    the URL carries a question id and nothing about who owns it."""
    app, conn, clients, skus = five_shelves(tmp_path)
    from resell.domain import FeeModel
    from resell.gateway import Gateway

    target = skus["ana@example.test"]
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    gateway.ask_operator(target, question="What size?", why_it_matters="")
    question_id = conn.execute(
        "SELECT id FROM open_question WHERE sku = ?", (target,)
    ).fetchone()[0]

    refused = clients["ben@example.test"].post(
        f"/questions/{question_id}/answer", data={"answer": "40R", "sku": target}
    )
    assert refused.status_code == 404
    assert conn.execute(
        "SELECT answer FROM open_question WHERE id = ?", (question_id,)
    ).fetchone()[0] is None


def test_a_run_cannot_be_watched_by_the_wrong_person(tmp_path):
    """`/runs/<id>` returns step messages, which say what the agent is reading."""
    _, conn, clients, skus = five_shelves(tmp_path)
    target = skus["ana@example.test"]
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        ("run_ana", target, "running", db.now_iso()),
    )
    conn.commit()
    assert clients["ben@example.test"].get("/runs/run_ana").status_code == 404
    assert clients["ana@example.test"].get("/runs/run_ana").status_code == 200


# --- /ops is the operator's, and only the operator's -----------------------------------


@pytest.mark.parametrize("path", ["/ops", "/ops/inventory", "/ops/items/MP-000001"])
def test_ops_is_refused_to_testers(tmp_path, path):
    """In the app, not only at Cloudflare. The outer rule's failure mode is
    silent: a path policy that stops matching after a rename leaves these open
    with nothing to show for it."""
    app, _, clients, _ = five_shelves(tmp_path)
    for email, client in clients.items():
        assert client.get(path).status_code == 403, f"{email} reached {path}"


def test_the_operator_sees_every_testers_item(tmp_path):
    """Global on purpose. One inventory, one seller account, one person
    answerable for what goes up."""
    app, _, _, skus = five_shelves(tmp_path)
    body = as_user(app, OPERATOR).get("/ops/inventory").get_data(as_text=True)
    for sku in skus.values():
        assert sku in body


def test_the_operator_shelf_is_still_their_own(tmp_path):
    """Admin means "can see everything under /ops", not "everything is mine".
    The operator's consumer screens stay a seller's screens."""
    app, _, _, skus = five_shelves(tmp_path)
    operator = as_user(app, OPERATOR)
    shelf = operator.get("/items").get_data(as_text=True)
    for sku in skus.values():
        assert sku not in shelf


def test_the_paste_links_route_is_operator_only(tmp_path):
    _, _, clients, skus = five_shelves(tmp_path)
    own = skus["ana@example.test"]
    assert clients["ana@example.test"].post(
        f"/items/{own}/comps", data={"urls": "https://example.test/a"}
    ).status_code == 403


# --- writes from somewhere else --------------------------------------------------------


def test_a_cross_origin_post_is_refused(tmp_path):
    """The Access cookie is what an attacker would ride: a page a tester visits
    elsewhere posts here, the browser attaches the cookie, the request arrives
    authenticated."""
    _, _, clients, skus = five_shelves(tmp_path)
    own = skus["ana@example.test"]
    refused = clients["ana@example.test"].post(
        f"/items/{own}/abandon", data={},
        headers={"Origin": "https://not-us.example"},
    )
    assert refused.status_code == 403


def test_a_same_origin_post_is_allowed(tmp_path):
    _, _, clients, skus = five_shelves(tmp_path)
    own = skus["ana@example.test"]
    allowed = clients["ana@example.test"].post(
        f"/items/{own}/abandon", data={},
        headers={"Origin": "http://localhost"},
    )
    assert allowed.status_code == 302


# --- five at once ----------------------------------------------------------------------


def test_five_simultaneous_runs_do_not_collide(tmp_path):
    """The integration question: five phones, five items, five agent runs, one
    SQLite file. Checks the three things that could go wrong together -- a
    `database is locked`, a run attached to the wrong item, and one tester's run
    blocking another's."""
    import resell.orchestrator as orch

    app, conn, clients, skus = five_shelves(tmp_path)

    def stalls(self, conn_, gw, sku_, step):
        # Long enough that all five overlap, and it writes as it goes so the
        # threads genuinely contend for the same database.
        from resell import progress

        for n in range(6):
            progress.report(progress.Phase.THINKING, f"{sku_} step {n}")
        return "stopped on this item's budget"

    real, errors, started = orch.StageRunner.run, [], {}
    orch.StageRunner.run = stalls
    try:
        def press(email):
            try:
                response = clients[email].post(f"/items/{skus[email]}/run")
                started[email] = response.headers["Location"]
            except Exception as exc:  # noqa: BLE001 - collected, then asserted on
                errors.append(f"{email}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=press, args=(email,)) for email in TESTERS]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        assert len(started) == 5, started

        for _ in range(200):
            rows = conn.execute(
                "SELECT COUNT(*) FROM agent_run WHERE status = 'running'"
            ).fetchone()[0]
            if rows == 0:
                break
            import time

            time.sleep(0.05)
    finally:
        orch.StageRunner.run = real

    # Five distinct runs, each attached to the item whose owner pressed the
    # button, all finished. Counting rows per SKU would count the run that the
    # upload itself started, which is a different question.
    pressed = {email: location.split("run=")[1] for email, location in started.items()}
    assert len(set(pressed.values())) == 5, pressed
    for email, run_id in pressed.items():
        row = conn.execute(
            "SELECT sku, status FROM agent_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["sku"] == skus[email], f"{email}'s run landed on {row['sku']}"
        assert row["status"] == "done", (email, row["status"])
    steps = conn.execute("SELECT COUNT(*) FROM agent_run_step").fetchone()[0]
    assert steps >= 30, f"only {steps} steps written; the threads did not overlap"


def test_one_testers_run_does_not_hold_another_testers_item(tmp_path):
    """`_busy` is per item. If it were per anything wider, five testers would be
    back to taking turns with extra steps."""
    _, conn, clients, skus = five_shelves(tmp_path)
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        ("run_ana", skus["ana@example.test"], "running", db.now_iso()),
    )
    conn.commit()
    response = clients["ben@example.test"].post(f"/items/{skus['ben@example.test']}/run")
    assert response.status_code == 302
    assert "run=run_ana" not in response.headers["Location"]


# --- what a restart leaves behind --------------------------------------------------------


def test_a_restart_releases_every_testers_item(tmp_path):
    """The Mac sleeps mid-run. Without this the row stays `running` for ever,
    `_busy` refuses every action on that item, and the screen polls a run that
    will never finish -- with no way out from the interface."""
    from resell.webui import create_app
    from tests.test_webui import config_for

    config = config_for(tmp_path)
    app = create_app(config=config)
    app.config["TESTING"] = True
    conn = db.connect(config.db_path)
    clients = {email: as_user(app, email) for email in TESTERS}
    skus = {email: upload(client) for email, client in clients.items()}
    for email in TESTERS:
        conn.execute(
            "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
            (f"run_{email[:3]}", skus[email], "running", db.now_iso()),
        )
    conn.commit()

    # The process dies and comes back.
    restarted = create_app(config=config)
    restarted.config["TESTING"] = True

    for email in TESTERS:
        view = runs.read_run(conn, f"run_{email[:3]}")
        assert view.interrupted, email
        assert not view.broke and not view.blocked
        assert runs.active_run_for(conn, skus[email]) is None
        # And the item is workable again, which is the point.
        client = as_user(restarted, email)
        assert client.post(f"/items/{skus[email]}/run").status_code == 302


def test_the_seller_is_told_the_host_stopped_not_that_the_work_failed():
    """Distinct wording, because nothing was learned about the item. "We could
    not find prices for this" would be a claim about a market nobody finished
    looking at."""
    from resell.views_consumer import INTERRUPTED_SAYS, _STUCK, stuck_message

    class View:
        stopped_step = "comp_research"
        stopped_interrupted = True

    assert stuck_message(View()) == INTERRUPTED_SAYS
    assert INTERRUPTED_SAYS not in _STUCK.values()
    assert "could not" not in INTERRUPTED_SAYS.lower()

    View.stopped_interrupted = False
    assert stuck_message(View()) == "We could not find prices for this."
