"""The operator UI. Transport, and the three mutations it is allowed to make.

What is worth testing about a Flask app that holds no logic is exactly two
things: that every route reaches the read model without a formatting error, and
that a write goes through the gateway and is refused when the gateway refuses.
The assertions about pricing bands and question queues live in `test_views`,
where they belong.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from resell import db
from resell.config import SANDBOX, Config
from resell.domain import FeeModel
from resell.gateway import Gateway

flask = pytest.importorskip("flask", reason="the UI is an optional extra")


def config_for(tmp_path) -> Config:
    """No .env, no credentials, no tokens. The UI has to work without them."""
    return Config(
        env=SANDBOX,
        client_id="",
        client_secret="",
        runame="",
        scopes=(),
        marketplace_id="EBAY_US",
        db_path=tmp_path / "ui.db",
    )


def app_for(tmp_path):
    from resell.webui import create_app

    config = config_for(tmp_path)
    conn = db.connect(config.db_path)
    gateway = Gateway(
        conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel()
    )
    app = create_app(config=config)
    app.config["TESTING"] = True
    return app, conn, gateway


def seeded(tmp_path):
    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=2500, acquisition_intent="resale").sku
    gateway.attach_photo(
        sku, source_path=str(tmp_path / "a.jpg"), content_sha256="a" * 64,
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(
        sku, title="Brooks Brothers Suit Jacket", category_id="3001",
        condition_id="USED_GOOD",
    )
    return app, conn, gateway, sku


def png() -> bytes:
    """A 1x1 PNG. Small, real, and something `images.inspect` recognises."""
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
        "AAH/q842iQAAAABJRU5ErkJggg=="
    )


# --- read routes ---------------------------------------------------------------


def test_the_item_list_renders_with_no_items(tmp_path):
    app, _, _ = app_for(tmp_path)
    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"no items yet" in response.data


def test_every_item_page_renders(tmp_path):
    app, _, _, sku = seeded(tmp_path)
    client = app.test_client()
    for path in (
        "/",
        "/?active=1",
        "/questions",
        f"/items/{sku}",
        f"/items/{sku}/questions",
        f"/items/{sku}/draft",
        f"/items/{sku}/pricing",
    ):
        assert client.get(path).status_code == 200, path


def test_the_environment_is_named_on_every_page(tmp_path):
    """A UI that does not say which environment it has open is one mis-set
    variable away from repricing something real."""
    app, _, _, sku = seeded(tmp_path)
    assert b"sandbox" in app.test_client().get(f"/items/{sku}").data


def test_an_unknown_sku_is_a_404_not_a_traceback(tmp_path):
    app, _, _ = app_for(tmp_path)
    assert app.test_client().get("/items/MP-999999").status_code == 404


def test_the_aspects_page_reports_missing_credentials_rather_than_failing(tmp_path):
    """The rest of the UI works on a machine with no tokens; one page needing
    them must not take the app down."""
    app, _, _, sku = seeded(tmp_path)
    response = app.test_client().get(f"/items/{sku}/aspects")
    assert response.status_code == 200
    assert b"credentials" in response.data


def test_the_pricing_page_accepts_overridden_assumptions(tmp_path):
    app, _, _, sku = seeded(tmp_path)
    response = app.test_client().get(
        f"/items/{sku}/pricing?condition_band=used_fair&window_days=180"
    )
    assert response.status_code == 200
    assert b"used_fair" in response.data


def test_a_nonsense_window_falls_back_instead_of_erroring(tmp_path):
    app, _, _, sku = seeded(tmp_path)
    assert app.test_client().get(
        f"/items/{sku}/pricing?window_days=abc"
    ).status_code == 200


def test_a_missing_photo_file_is_a_404(tmp_path):
    """The database records a path; the file behind it can be gone."""
    app, _, _, sku = seeded(tmp_path)
    assert app.test_client().get(f"/items/{sku}/photo/1").status_code == 404


# --- creating an item ----------------------------------------------------------


def test_creating_an_item_allocates_a_sku_through_the_gateway(tmp_path):
    app, conn, _ = app_for(tmp_path)
    response = app.test_client().post(
        "/items",
        data={"cost_dollars": "25.00", "intent": "resale", "notes": "thrifted"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    row = conn.execute("SELECT * FROM item").fetchone()
    assert row["purchase_cost_cents"] == 2500
    assert row["acquisition_intent"] == "resale"
    assert row["state"] == "intake"


def test_a_blank_cost_is_recorded_as_unknown_not_as_zero(tmp_path):
    app, conn, _ = app_for(tmp_path)
    app.test_client().post("/items", data={"cost_dollars": "", "intent": "unknown"})
    assert conn.execute("SELECT purchase_cost_cents FROM item").fetchone()[0] is None


def test_uploading_a_photo_with_the_new_item_attaches_it(tmp_path):
    app, conn, _ = app_for(tmp_path)
    app.test_client().post(
        "/items",
        data={
            "cost_dollars": "10",
            "intent": "resale",
            "photos": (io.BytesIO(png()), "front.png"),
        },
        content_type="multipart/form-data",
    )
    row = conn.execute("SELECT * FROM photo").fetchone()
    assert row is not None
    assert Path(row["source_path"]).name == "front.png"


def test_an_uploaded_photo_is_written_somewhere_permanent(tmp_path):
    """attach_photo stores a path and publish reads it much later, so a temp file
    would produce an item whose photos exist only until the process ends."""
    app, conn, _ = app_for(tmp_path)
    app.test_client().post(
        "/items",
        data={"cost_dollars": "10", "photos": (io.BytesIO(png()), "front.png")},
        content_type="multipart/form-data",
    )
    stored = Path(conn.execute("SELECT source_path FROM photo").fetchone()[0])
    assert stored.is_absolute()
    assert stored.exists()


def test_the_stored_digest_is_of_the_bytes_that_were_written(tmp_path):
    import hashlib

    app, conn, _ = app_for(tmp_path)
    app.test_client().post(
        "/items",
        data={"cost_dollars": "10", "photos": (io.BytesIO(png()), "front.png")},
        content_type="multipart/form-data",
    )
    row = conn.execute("SELECT source_path, content_sha256 FROM photo").fetchone()
    assert row["content_sha256"] == hashlib.sha256(
        Path(row["source_path"]).read_bytes()
    ).hexdigest()


def test_a_second_upload_of_the_same_photo_is_reported_not_silently_dropped(tmp_path):
    """The gateway refuses a duplicate content hash; the operator has to see it."""
    app, conn, _, sku = seeded(tmp_path)
    client = app.test_client()
    for _ in range(2):
        response = client.post(
            f"/items/{sku}/photos",
            data={"photos": (io.BytesIO(png()), "front.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
    assert b"already attached" in response.data
    assert conn.execute(
        "SELECT COUNT(*) FROM photo WHERE sku = ?", (sku,)
    ).fetchone()[0] == 2


def test_adding_a_photo_to_an_unknown_sku_is_a_404(tmp_path):
    """Not a flashed rejection: the redirect target would 404 anyway and the
    message would be lost, so the URL is answered for what it is."""
    app, conn, _ = app_for(tmp_path)
    response = app.test_client().post(
        "/items/MP-999999/photos",
        data={"photos": (io.BytesIO(png()), "front.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM photo").fetchone()[0] == 0


# --- answering questions -------------------------------------------------------


def ask(gateway, sku, question="What size is it?", *, blocking=True):
    gateway.ask_operator(sku, question=question, why_it_matters="", blocking=blocking)
    return gateway.conn.execute(
        "SELECT id FROM open_question WHERE sku = ? ORDER BY id DESC", (sku,)
    ).fetchone()[0]


def test_answering_a_question_records_it_through_the_gateway(tmp_path):
    app, conn, gateway, sku = seeded(tmp_path)
    question_id = ask(gateway, sku)
    response = app.test_client().post(
        f"/questions/{question_id}/answer",
        data={"answer": "40R", "sku": sku},
        follow_redirects=True,
    )
    assert response.status_code == 200
    row = conn.execute(
        "SELECT answer, answered_at FROM open_question WHERE id = ?", (question_id,)
    ).fetchone()
    assert row["answer"] == "40R"
    assert row["answered_at"]


def test_an_answer_becomes_operator_evidence(tmp_path):
    """basis='operator' is what lets the answer adjudicate a contradiction later,
    rather than being filed and ignored."""
    app, conn, gateway, sku = seeded(tmp_path)
    question_id = ask(gateway, sku)
    app.test_client().post(
        f"/questions/{question_id}/answer", data={"answer": "40R", "sku": sku}
    )
    row = conn.execute(
        "SELECT kind, basis, payload FROM evidence WHERE sku = ? AND kind = ?",
        (sku, "operator_answer"),
    ).fetchone()
    assert row["basis"] == "operator"
    assert json.loads(row["payload"])["answer"] == "40R"


def test_an_answer_about_an_aspect_becomes_a_candidate(tmp_path):
    """The seam the question loop exists for: the answer resolves the aspect."""
    app, conn, gateway, sku = seeded(tmp_path)
    question_id = ask(gateway, sku)
    conn.execute(
        "UPDATE open_question SET aspect_name = 'Size' WHERE id = ?", (question_id,)
    )
    app.test_client().post(
        f"/questions/{question_id}/answer", data={"answer": "40R", "sku": sku}
    )
    row = conn.execute(
        "SELECT aspect_name, value FROM aspect_candidate WHERE aspect_name = 'Size'"
    ).fetchone()
    assert row["value"] == "40R"


def test_an_answer_outside_ebays_values_is_refused_with_the_list(tmp_path):
    app, conn, gateway, sku = seeded(tmp_path)
    question_id = ask(gateway, sku)
    conn.execute(
        "UPDATE open_question SET aspect_name = 'Size', allowed_values_json = ? "
        "WHERE id = ?",
        (json.dumps(["38R", "40R"]), question_id),
    )
    response = app.test_client().post(
        f"/questions/{question_id}/answer",
        data={"answer": "99R", "sku": sku},
        follow_redirects=True,
    )
    assert b"REJECTED" in response.data
    assert b"eBay accepts" in response.data
    assert conn.execute(
        "SELECT answered_at FROM open_question WHERE id = ?", (question_id,)
    ).fetchone()[0] is None


def test_the_override_checkbox_is_what_lets_an_unlisted_value_through(tmp_path):
    """eBay does not guarantee its list is exhaustive, so the override exists --
    but it is a decision the operator makes explicitly, and it goes on record."""
    app, conn, gateway, sku = seeded(tmp_path)
    question_id = ask(gateway, sku)
    conn.execute(
        "UPDATE open_question SET aspect_name = 'Size', allowed_values_json = ? "
        "WHERE id = ?",
        (json.dumps(["38R", "40R"]), question_id),
    )
    app.test_client().post(
        f"/questions/{question_id}/answer",
        data={"answer": "99R", "sku": sku, "value_not_listed": "1"},
    )
    payload = json.loads(
        conn.execute(
            "SELECT payload FROM evidence WHERE kind = 'operator_answer'"
        ).fetchone()[0]
    )
    assert payload["value_not_listed"] is True
    assert payload["allowed_values_at_answer"] == ["38R", "40R"]


def test_an_empty_answer_is_refused(tmp_path):
    app, conn, gateway, sku = seeded(tmp_path)
    question_id = ask(gateway, sku)
    response = app.test_client().post(
        f"/questions/{question_id}/answer",
        data={"answer": "   ", "sku": sku},
        follow_redirects=True,
    )
    assert b"REJECTED" in response.data


def test_answering_the_same_question_twice_is_refused(tmp_path):
    app, _, gateway, sku = seeded(tmp_path)
    question_id = ask(gateway, sku)
    client = app.test_client()
    client.post(f"/questions/{question_id}/answer", data={"answer": "40R", "sku": sku})
    response = client.post(
        f"/questions/{question_id}/answer",
        data={"answer": "42R", "sku": sku},
        follow_redirects=True,
    )
    assert b"already answered" in response.data


def test_answering_the_last_blocking_question_moves_the_item_out_of_needs_info(tmp_path):
    app, conn, gateway, sku = seeded(tmp_path)
    # ask_operator with blocking=True is itself what moves the item there.
    question_id = ask(gateway, sku)
    assert conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0] == (
        "needs_info"
    )
    response = app.test_client().post(
        f"/questions/{question_id}/answer",
        data={"answer": "40R", "sku": sku},
        follow_redirects=True,
    )
    assert b"needs_info -&gt; identifying" in response.data
    assert conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0] == (
        "identifying"
    )


def test_the_inbox_answers_a_question_and_returns_to_the_inbox(tmp_path):
    app, _, gateway, sku = seeded(tmp_path)
    question_id = ask(gateway, sku, blocking=False)
    response = app.test_client().post(
        f"/questions/{question_id}/answer",
        data={"answer": "yes", "sku": sku, "next": "inbox"},
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/questions"


# --- what the UI must not do ---------------------------------------------------


def test_there_is_no_route_that_approves_publishes_or_prices(tmp_path):
    """Those bind authority to a hash. A button is the wrong place for them, and
    a route that appears later should fail this test loudly."""
    app, _, _ = app_for(tmp_path)
    paths = {str(rule) for rule in app.url_map.iter_rules()}
    forbidden = ("approve", "publish", "propose", "abandon", "reprice", "apply")
    assert not [p for p in paths if any(word in p for word in forbidden)]


def test_every_write_route_is_post_only(tmp_path):
    app, _, _ = app_for(tmp_path)
    writes = [
        rule for rule in app.url_map.iter_rules() if "POST" in rule.methods
    ]
    assert {str(rule) for rule in writes} == {
        "/items", "/items/<sku>/photos", "/questions/<int:question_id>/answer",
    }
    assert all("GET" not in rule.methods for rule in writes)
