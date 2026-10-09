"""The operator UI: two screens, and one decision per item.

The previous UI had a page per stage and this one does not, so most of what was
asserted here is gone with it. What replaces it is the product claim: an item
appears on the home screen when it needs a person, showing the single thing it
needs, and the observation/claim split never surfaces as two actions.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from resell import db, store_pricing as sp
from resell.config import SANDBOX, Config
from resell.domain import FeeModel
from resell.gateway import Gateway
from resell.pricing.comps import (
    CompBasis,
    CompObservation,
    ConditionBand,
    PriceKind,
)

flask = pytest.importorskip("flask", reason="the UI is an optional extra")

NOW = __import__("datetime").datetime(2026, 8, 22, tzinfo=__import__("datetime").UTC)


def config_for(tmp_path) -> Config:
    return Config(
        env=SANDBOX, client_id="", client_secret="", runame="", scopes=(),
        marketplace_id="EBAY_US", db_path=tmp_path / "ui.db",
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


def png() -> bytes:
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
        "AAH/q842iQAAAABJRU5ErkJggg=="
    )


def seeded(tmp_path, *, with_photo=True, identified=False):
    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    if with_photo:
        gateway.attach_photo(
            sku, source_path=str(tmp_path / "a.jpg"), content_sha256="a" * 64,
            image_format="jpeg", size_bytes=1000, validation_errors=None,
        )
    if identified:
        gateway.begin_identification(sku)
        conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
            "recorded_at, basis, subject) VALUES (?,?,?,?,1,?,?,'this_item')",
            (sku, "vision_observation", "fake/m", json.dumps({"claim": "a speaker"}),
             db.now_iso(), "visual_observation"),
        )
        gateway.propose_identification(
            sku, title="Beats Pill", category_id="111694", condition_id="USED_GOOD",
            aspects={"Brand": ["Beats by Dr. Dre"]},
        )
    return app, conn, gateway, sku


# --- the two screens ---------------------------------------------------------


def test_the_home_screen_renders_with_nothing_to_do(tmp_path):
    app, _, _ = app_for(tmp_path)
    response = app.test_client().get("/ops")
    assert response.status_code == 200
    assert b"Nothing needs you right now" in response.data


def test_the_intake_form_asks_for_photographs(tmp_path):
    """Photographs are the input; everything else is worked out."""
    app, _, _ = app_for(tmp_path)
    body = app.test_client().get("/ops").data
    assert b'type="file"' in body
    assert b"upload" in body


def test_the_inventory_table_renders(tmp_path):
    app, _, _, sku = seeded(tmp_path, identified=True)
    response = app.test_client().get("/ops/inventory")
    assert response.status_code == 200
    assert sku.encode() in response.data


def test_there_are_only_two_screens(tmp_path):
    """The product constraint, kept honest. A third page is a redesign, not an
    addition."""
    app, _, _ = app_for(tmp_path)
    pages = {
        str(rule) for rule in app.url_map.iter_rules()
        if "GET" in rule.methods and "static" not in str(rule)
        and "photo" not in str(rule)
    }
    # Two screens each, and one JSON endpoint that is not a screen -- it answers
    # the page already open, which is what stopped the browser holding a request
    # for two minutes.
    #
    # The operator UI moved under /ops rather than changing: it is still the
    # place to diagnose an item, and the consumer screens are a projection of the
    # same state, not a second workflow.
    assert pages == {
        "/", "/items", "/items/<sku>",                      # consumer
        "/ops", "/ops/inventory", "/ops/items/<sku>",       # operator
        "/runs/<run_id>",                                   # neither
    }


def test_an_unknown_sku_is_a_404(tmp_path):
    app, _, _ = app_for(tmp_path)
    assert app.test_client().post("/items/MP-999999/run").status_code == 404


# --- an item appears when it needs a person ----------------------------------


def test_an_item_needing_photos_says_so(tmp_path):
    app, _, gateway = app_for(tmp_path)
    gateway.ingest_item(purchase_cost_cents=500)
    body = app.test_client().get("/ops").data
    assert b"add photos" in body


def test_an_item_the_agent_can_work_on_is_not_a_card(tmp_path):
    """Only decisions get a card. Work in progress is listed quietly."""
    app, _, _, sku = seeded(tmp_path)
    body = app.test_client().get("/ops").data.decode()
    assert "the agent still has work to do" in body
    assert "start work on it" in body


def test_a_blocking_question_becomes_the_card(tmp_path):
    app, conn, gateway, sku = seeded(tmp_path, identified=True)
    gateway.ask_operator(
        sku, question="What size is it?", why_it_matters="required aspect",
        aspect_name="Size",
    )
    body = app.test_client().get("/ops").data
    assert b"What size is it?" in body
    assert b"answer" in body


def test_answering_from_the_card_records_the_answer(tmp_path):
    app, conn, gateway, sku = seeded(tmp_path, identified=True)
    gateway.ask_operator(sku, question="What size?", why_it_matters="")
    question_id = conn.execute("SELECT id FROM open_question").fetchone()[0]
    app.test_client().post(
        f"/questions/{question_id}/answer", data={"answer": "40R", "sku": sku},
        follow_redirects=True,
    )
    assert conn.execute(
        "SELECT answer FROM open_question WHERE id = ?", (question_id,)
    ).fetchone()[0] == "40R"


# --- the comparable, which is one decision ------------------------------------


def comp_row(comp_id, price_cents, *, kind=PriceKind.ASKING):
    return CompObservation(
        comp_id=comp_id, marketplace="poshmark.com", external_id=comp_id,
        price_kind=kind,
        basis=CompBasis.ACTIVE_SIMILAR if kind is PriceKind.ASKING
        else CompBasis.SOLD_SIMILAR,
        price_cents=price_cents, observed_at=NOW,
        condition_band=ConditionBand.USED_GOOD, shipping_cents=None,
        condition_declared_raw="Pre-owned", title=f"Beats Pill {comp_id}",
        url=f"https://poshmark.com/{comp_id}",
    )


def offer(conn, sku, comp_id, price_cents, **kw):
    sp.record_comp_observation(conn, comp_row(comp_id, price_cents, **kw))
    return sp.record_comp_candidate(
        conn, sku=sku, comp_id=comp_id,
        proposed_comparability="same_family_variant",
        item_citations=("1",), comp_citations=("title",),
        rationale="same product line",
    )


def priced(tmp_path):
    app, conn, gateway, sku = seeded(tmp_path, identified=True)
    gateway.begin_pricing(sku)
    return app, conn, gateway, sku


def test_a_discovered_comp_appears_as_something_to_accept(tmp_path):
    app, conn, _, sku = priced(tmp_path)
    offer(conn, sku, "comp_a", 7995)
    body = app.test_client().get("/ops").data
    assert b"same sort of thing" in body
    assert b"$79.95" in body
    # And never the vocabulary underneath it.
    assert b"comp-add" not in body
    assert b"comparability" not in body


def test_accepting_a_comp_is_one_action_that_writes_the_claim(tmp_path):
    """The observation/claim split, hidden. One press, two rows."""
    app, conn, _, sku = priced(tmp_path)
    candidate_id = offer(conn, sku, "comp_a", 7995)
    app.test_client().post(
        f"/candidates/{candidate_id}/accept", data={"sku": sku},
        follow_redirects=True,
    )
    assert len(sp.load_scored_comps(conn, sku)) == 1
    assert sp.pending_comp_candidates(conn, sku) == []


def test_rejecting_a_comp_records_an_exclusion_rather_than_a_deletion(tmp_path):
    app, conn, _, sku = priced(tmp_path)
    candidate_id = offer(conn, sku, "comp_a", 7995)
    app.test_client().post(
        f"/candidates/{candidate_id}/reject",
        data={"sku": sku, "reason": "a bundle of three"}, follow_redirects=True,
    )
    row = conn.execute(
        "SELECT comparability, excluded_reason FROM comp_claim"
    ).fetchone()
    assert row["comparability"] == "excluded"
    assert row["excluded_reason"] == "a bundle of three"


def test_a_rejected_comp_is_not_offered_again(tmp_path):
    app, conn, _, sku = priced(tmp_path)
    candidate_id = offer(conn, sku, "comp_a", 7995)
    app.test_client().post(
        f"/candidates/{candidate_id}/reject", data={"sku": sku, "reason": "no"},
    )
    assert "comp_a" in sp.already_offered(conn, sku)


def test_an_already_decided_candidate_cannot_be_decided_twice(tmp_path):
    app, conn, _, sku = priced(tmp_path)
    candidate_id = offer(conn, sku, "comp_a", 7995)
    client = app.test_client()
    client.post(f"/candidates/{candidate_id}/accept", data={"sku": sku})
    response = client.post(
        f"/candidates/{candidate_id}/accept", data={"sku": sku},
        follow_redirects=True,
    )
    assert b"already accepted" in response.data
    assert len(sp.load_scored_comps(conn, sku)) == 1


# --- the price ----------------------------------------------------------------


def accepted_comps(conn, sku, prices):
    for index, price in enumerate(prices):
        candidate_id = offer(conn, sku, f"comp_{index}", price)
        sp.accept_comp_candidate(
            conn, candidate_id, identity_resolution="searched_not_found"
        )


def test_the_price_card_shows_three_options_and_a_range(tmp_path):
    app, conn, _, sku = priced(tmp_path)
    accepted_comps(conn, sku, [7995, 8995, 9995])
    body = app.test_client().get("/ops").data.decode()
    assert "approve a price" in body
    for objective in ("fast sale", "balanced", "max proceeds"):
        assert objective in body


def test_approving_a_price_records_a_proposal_and_an_approval(tmp_path):
    """One decision, and the two rows the record needs are this route's problem."""
    app, conn, _, sku = priced(tmp_path)
    accepted_comps(conn, sku, [7995, 8995, 9995])
    app.test_client().post(
        f"/items/{sku}/price", data={"objective": "balanced"}, follow_redirects=True,
    )
    assert conn.execute("SELECT COUNT(*) FROM price_proposal").fetchone()[0] == 1
    assert sp.approved_price_cents(conn, sku) is not None


def test_the_approved_price_is_the_option_that_was_pressed(tmp_path):
    app, conn, _, sku = priced(tmp_path)
    accepted_comps(conn, sku, [7995, 8995, 9995])
    client = app.test_client()
    client.post(f"/items/{sku}/price", data={"objective": "fast_sale"},
                follow_redirects=True)
    row = conn.execute(
        "SELECT objective, price_cents FROM price_proposal"
    ).fetchone()
    assert row["objective"] == "fast_sale"
    assert sp.approved_price_cents(conn, sku) == row["price_cents"]


def test_an_unpriceable_item_says_so_rather_than_failing(tmp_path):
    app, conn, _, sku = priced(tmp_path)
    response = app.test_client().post(
        f"/items/{sku}/price", data={"objective": "balanced"}, follow_redirects=True,
    )
    assert response.status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM price_proposal").fetchone()[0] == 0


# --- what the UI still refuses to do -------------------------------------------


def test_publishing_is_not_part_of_carrying_on(tmp_path):
    """Everything the orchestrator does is local and reversible. Publishing is
    neither, so it stays a separate, named press."""
    from resell.orchestrator import Step, StageRunner

    assert not hasattr(StageRunner, f"_{Step.PUBLISH}")
    assert not hasattr(StageRunner, f"_{Step.APPROVE_PRICE}")
    assert not hasattr(StageRunner, f"_{Step.APPROVE_LISTING}")
    assert not hasattr(StageRunner, f"_{Step.ANSWER_QUESTIONS}")


def test_every_write_is_a_post(tmp_path):
    app, _, _ = app_for(tmp_path)
    writes = [r for r in app.url_map.iter_rules() if "POST" in r.methods]
    assert all("GET" not in rule.methods for rule in writes)


def test_the_environment_is_named_on_every_screen(tmp_path):
    app, _, _, sku = seeded(tmp_path, identified=True)
    client = app.test_client()
    assert b"sandbox" in client.get("/ops").data
    assert b"sandbox" in client.get("/ops/inventory").data


# --- intake -------------------------------------------------------------------


def test_uploading_creates_an_item_and_attaches_the_photo(tmp_path):
    app, conn, _ = app_for(tmp_path)
    app.test_client().post(
        "/items",
        data={"cost_dollars": "18", "photos": (io.BytesIO(png()), "front.png")},
        content_type="multipart/form-data",
    )
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 1
    row = conn.execute("SELECT source_path FROM photo").fetchone()
    assert Path(row["source_path"]).exists()


def test_an_upload_with_no_photo_says_so(tmp_path):
    app, conn, _ = app_for(tmp_path)
    response = app.test_client().post(
        "/items", data={"cost_dollars": "18"}, follow_redirects=True
    )
    assert b"no photo attached yet" in response.data


# --- correcting what the record got wrong --------------------------------------


def test_the_card_offers_the_details_for_editing(tmp_path):
    app, _, _, sku = seeded(tmp_path, identified=True)
    body = app.test_client().get(f"/ops/items/{sku}").data.decode()
    assert "change the details" in body
    assert 'name="title"' in body
    assert 'name="condition_id"' in body


def test_a_correction_supersedes_rather_than_edits(tmp_path):
    """`propose_identification` never overwrites, so the old value stays readable."""
    app, conn, _, sku = seeded(tmp_path, identified=True)
    app.test_client().post(
        f"/items/{sku}/fix", data={"condition_id": "USED_EXCELLENT"},
        follow_redirects=True,
    )
    rows = conn.execute(
        "SELECT condition_id, superseded_at FROM identification WHERE sku = ? "
        "ORDER BY version", (sku,),
    ).fetchall()
    assert rows[-1]["condition_id"] == "USED_EXCELLENT"
    assert rows[-1]["superseded_at"] is None
    assert any(r["condition_id"] == "USED_GOOD" for r in rows[:-1])


def test_a_correction_carries_the_untouched_fields_forward(tmp_path):
    app, conn, _, sku = seeded(tmp_path, identified=True)
    app.test_client().post(
        f"/items/{sku}/fix", data={"condition_id": "USED_EXCELLENT"},
        follow_redirects=True,
    )
    row = conn.execute(
        "SELECT title, category_id FROM identification WHERE sku = ? "
        "AND superseded_at IS NULL", (sku,),
    ).fetchone()
    assert row["title"] == "Beats Pill"
    assert row["category_id"] == "111694"


def test_an_empty_correction_changes_nothing(tmp_path):
    app, conn, _, sku = seeded(tmp_path, identified=True)
    before = conn.execute("SELECT COUNT(*) FROM identification").fetchone()[0]
    response = app.test_client().post(
        f"/items/{sku}/fix", data={"title": "   "}, follow_redirects=True
    )
    assert b"nothing to change" in response.data
    assert conn.execute("SELECT COUNT(*) FROM identification").fetchone()[0] == before


def test_correcting_an_approved_listing_sends_it_back_for_approval(tmp_path):
    """The approval binds specific words. Changing them underneath it would leave
    an approval covering something nobody approved."""
    from resell.domain import ItemState

    app, conn, gateway, sku = seeded(tmp_path, identified=True)
    conn.execute("UPDATE item SET state = 'approved' WHERE sku = ?", (sku,))
    conn.commit()
    app.test_client().post(
        f"/items/{sku}/fix", data={"condition_id": "USED_EXCELLENT"},
        follow_redirects=True,
    )
    assert conn.execute(
        "SELECT state FROM item WHERE sku = ?", (sku,)
    ).fetchone()[0] == str(ItemState.PRICING)


def test_correcting_an_item_still_being_identified_does_not_revise(tmp_path):
    """Nothing has been proposed, so there is no approval to protect."""
    from resell.domain import ItemState

    app, conn, _, sku = seeded(tmp_path, identified=True)
    app.test_client().post(
        f"/items/{sku}/fix", data={"title": "A better title"}, follow_redirects=True,
    )
    assert conn.execute(
        "SELECT state FROM item WHERE sku = ?", (sku,)
    ).fetchone()[0] == str(ItemState.IDENTIFYING)


def test_the_form_falls_back_to_free_text_without_credentials(tmp_path):
    """The condition picker is a convenience. A card must not fail over it."""
    from resell import views

    app, conn, gateway, sku = seeded(tmp_path, identified=True)
    form = views.correction_form(conn, sku, config=None, gateway=None)
    condition = next(f for f in form.fields if f.name == "condition_id")
    assert not condition.is_choice
    assert condition.value == "USED_GOOD"


def test_price_is_not_editable_here(tmp_path):
    """It belongs to the pricing approval. A box here would be a second authority
    over the same number."""
    from resell import views

    app, conn, _, sku = seeded(tmp_path, identified=True)
    form = views.correction_form(conn, sku, config=None, gateway=None)
    assert "price" not in {f.name for f in form.fields}


# --- redirects ------------------------------------------------------------------


def test_uploading_lands_on_a_page_rather_than_a_405(tmp_path):
    """A 302 into a POST-only route is followed with GET and answers 405, which is
    what upload did. The previous tests never followed the redirect, so nothing
    caught it."""
    app, conn, _ = app_for(tmp_path)
    response = app.test_client().post(
        "/items",
        data={"cost_dollars": "18", "photos": (io.BytesIO(png()), "front.png")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    assert response.status_code == 200


def test_adding_photos_lands_on_a_page(tmp_path):
    app, conn, gateway, sku = seeded(tmp_path, with_photo=False)
    response = app.test_client().post(
        f"/items/{sku}/photos",
        data={"photos": (io.BytesIO(png()), "front.png")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    assert response.status_code == 200


def test_answering_lands_on_a_page(tmp_path):
    app, conn, gateway, sku = seeded(tmp_path, identified=True)
    gateway.ask_operator(sku, question="What size?", why_it_matters="")
    question_id = conn.execute("SELECT id FROM open_question").fetchone()[0]
    response = app.test_client().post(
        f"/questions/{question_id}/answer", data={"answer": "40R", "sku": sku},
        follow_redirects=True,
    )
    assert response.status_code == 200


def test_approving_a_price_lands_on_a_page(tmp_path):
    app, conn, _, sku = priced(tmp_path)
    accepted_comps(conn, sku, [7995, 8995, 9995])
    response = app.test_client().post(
        f"/items/{sku}/price", data={"objective": "balanced"}, follow_redirects=True,
    )
    assert response.status_code == 200


def test_no_route_redirects_into_a_post_only_route(tmp_path):
    """The class of bug, not the instance: a redirect whose target refuses GET is
    a 405 waiting to happen, whatever the status code used to dodge it."""
    import re

    from pathlib import Path as _Path

    # Derived from the app rather than listed here. A hand-maintained set silently
    # stops covering every route added after it was written, which is the same
    # failure mode as the bug it guards against.
    app, _conn, _gateway = app_for(tmp_path)
    post_only = {
        rule.endpoint.split(".")[-1]
        for rule in app.url_map.iter_rules()
        if "GET" not in rule.methods
    }
    assert "abandon" in post_only, "the guard is not seeing the routes"
    assert "restore" in post_only

    source = _Path("src/resell/webui/app.py").read_text()
    targets = set(re.findall(r'redirect\(url_for\("(\w+)"', source))
    assert not (targets & post_only), targets & post_only


# --- nothing the UI runs may wait on a terminal ---------------------------------


def test_no_web_request_can_block_on_stdin(tmp_path, monkeypatch):
    """The bug this closes: comp research built its adapter with the default
    prompt, which is builtin `input()`. Run from the web server that parks the
    request on the terminal the server was launched from — the browser waits
    forever and the item never leaves pricing.

    Asserted by making `input` itself fail, then driving every agent step.
    """
    def exploded(*_args, **_kwargs):
        raise AssertionError("a web request called input()")

    monkeypatch.setattr("builtins.input", exploded)

    app, conn, gateway, sku = seeded(tmp_path, identified=True)
    gateway.begin_pricing(sku)
    client = app.test_client()
    assert client.post(f"/items/{sku}/run", follow_redirects=True).status_code == 200


def test_the_orchestrator_owns_no_interactive_adapter():
    """Structural, not behavioural: the runner has no way to build one, so no
    future step can reintroduce the prompt by accident."""
    from pathlib import Path as _Path

    source = _Path("src/resell/orchestrator.py").read_text()
    assert "marketplace_adapter" not in source
    assert "OperatorUrlMarketplaceAdapter" not in source



def test_an_ebay_link_pasted_here_is_still_refused():
    """The licence blocklist applies to anything pasted, and is checked before any
    fetch. Driven at the adapter rather than through the route, because the route
    plans with a model first and the planner is not what this is about."""
    from resell.reasoning.adapters.marketplace import SuppliedUrlsMarketplaceAdapter
    from resell.reasoning.adapters.research import ResearchQuery

    said: list[str] = []
    adapter = SuppliedUrlsMarketplaceAdapter(
        ["https://www.ebay.com/sch/i.html?_nkw=beats"], echo=said.append
    )
    assert adapter.search(ResearchQuery("beats", "marketplace", "m")) == []
    assert any("must not be fetched" in note for note in adapter.notes)
    assert any("comp-add" in line for line in said)


def test_the_supplied_adapter_refuses_to_prompt():
    from resell.reasoning.adapters.marketplace import SuppliedUrlsMarketplaceAdapter

    adapter = SuppliedUrlsMarketplaceAdapter(["https://poshmark.com/x"])
    with pytest.raises(RuntimeError, match="never prompts"):
        adapter._ask("url?")


# --- the screen reflects whether the agent can search ------------------------------


def test_the_paste_box_is_a_fallback_when_search_exists(monkeypatch):
    """The banner said "there is no search backend yet" unconditionally. Once one
    exists that is simply false, and a stale limitation notice teaches an operator
    to ignore the notices that are true."""
    from resell import views

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "brave")
    assert views.search_is_available()


def test_no_backend_is_reported_as_a_limitation(monkeypatch):
    from resell import views

    monkeypatch.delenv("RESELL_SEARCH_BACKEND", raising=False)
    assert not views.search_is_available()


def test_an_unknown_backend_name_is_unavailable_not_a_crash(monkeypatch):
    """A typo in configuration should degrade to the paste box, not 500 the page
    that was going to explain the paste box."""
    from resell import views

    monkeypatch.setenv("RESELL_SEARCH_BACKEND", "nonesuch")
    assert not views.search_is_available()



def test_the_blocked_step_offers_a_way_out_rather_than_carry_on():
    """The deadlock had two halves. Routing the item to a new step fixes the
    first; without a card for it the item would fall through to the generic
    "carry on" and loop one step further along."""
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(money=lambda v: v, shorten=lambda v, n=80: v)
    env.globals["url_for"] = lambda name, **k: f"/{name}"

    class Card(dict):
        __getattr__ = dict.get

    out = env.get_template("_card.html").render(
        card=Card(sku="MP-11", step="price_without_comps", waiting_on_operator=True,
                  can_search=True, title="Canon EOS Rebel T6i", photo_positions=(),
                  questions=(), candidates=(), summary="decide a price",
                  detail="comp research is spent: 3 of 3 planning calls used",
                  state="pricing", is_done=False, has_price=False, ai_cost_micros=0),
        forms={},
    )
    assert "3 of 3 planning calls" in out
    # Every exit here has to actually work. Pasting a listing was offered first
    # and did nothing: `run_comp_round` plans before it fetches, so a supplied URL
    # hit the same exhausted planning budget. It is gone.
    assert "/set_price" in out          # type a number and move on
    assert "/more_research" in out      # or buy this item another round
    assert "/abandon" in out            # or stop
    assert "read these listings" not in out
    # The thing that looped: no button back into the step that cannot run. A
    # carry-on here would re-enter `/run`, find the same operator step, and
    # return the same screen.
    assert 'action="/run"' not in out


# --- setting an item aside from the screen ----------------------------------------


def test_every_unfinished_card_offers_a_way_to_stop():
    """The button lived on exactly one card, so an item that went wrong early had
    no way out of the queue except finishing it."""
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(money=lambda v: v, shorten=lambda v, n=80: v)
    env.globals["url_for"] = lambda name, **k: f"/{name}"

    class Card(dict):
        __getattr__ = dict.get

    for step in ("attach_photos", "answer_questions", "comp_research",
                 "approve_price", "approve_listing", "publish", "draft"):
        out = env.get_template("_card.html").render(
            card=Card(sku="MP-1", step=step, waiting_on_operator=True,
                      can_search=False, title="x", photo_positions=(), questions=(),
                      candidates=(), summary="s", detail="d", state="identifying",
                      is_done=False, has_price=False, ai_cost_micros=0,
                      grant_calls=3, grant_lookups=3, floor_cents=623,
                      purchase_cost_cents=None),
            forms={},
        )
        assert "/abandon" in out, step
        assert "Nothing is deleted" in out, step


def test_a_finished_card_does_not_offer_it():
    """There is nothing to stop, and the state machine would refuse anyway."""
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(money=lambda v: v, shorten=lambda v, n=80: v)
    env.globals["url_for"] = lambda name, **k: f"/{name}"

    class Card(dict):
        __getattr__ = dict.get

    out = env.get_template("_card.html").render(
        card=Card(sku="MP-1", step="done", waiting_on_operator=False, can_search=False,
                  title="x", photo_positions=(), questions=(), candidates=(),
                  summary="s", detail="d", state="listed", is_done=True,
                  has_price=False, ai_cost_micros=0, grant_calls=3, grant_lookups=3,
                  floor_cents=0, purchase_cost_cents=None),
        forms={},
    )
    assert "/abandon" not in out


def test_the_inventory_offers_to_show_what_it_is_hiding():
    """A hidden thing with no indication it is hidden is just a missing thing."""
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(money=lambda v: v, shorten=lambda v, n=80: v,
                       micros=lambda v: v)
    env.globals["url_for"] = lambda name, **k: (
        f"/{name}" + ("?abandoned=1" if k.get("abandoned") else "")
    )
    # `inventory.html` extends `base.html`, which is a Flask template.
    env.globals["get_flashed_messages"] = lambda **k: []
    out = env.get_template("inventory.html").render(
        rows=[], environment="sandbox", show_abandoned=False, abandoned_count=2,
    )
    assert "2 item(s) set aside and hidden" in out
    assert "?abandoned=1" in out


def test_showing_them_offers_a_way_back(tmp_path):
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(money=lambda v: v, shorten=lambda v, n=80: v,
                       micros=lambda v: v)
    env.globals["url_for"] = lambda name, **k: f"/{name}"
    env.globals["get_flashed_messages"] = lambda **k: []

    class Row(dict):
        __getattr__ = dict.get

    out = env.get_template("inventory.html").render(
        rows=[Row(sku="MP-2", title="x", state="abandoned", actor="nobody",
                  summary="abandoned", purchase_cost_cents=0,
                  approved_price_cents=None, listing_price_cents=None,
                  margin_cents=None, ai_cost_micros=0, photo_count=1, comps=0,
                  pending_candidates=0, listing_id=None)],
        environment="sandbox", show_abandoned=True, abandoned_count=1,
    )
    assert "/restore" in out
    assert "bring it back" in out


def test_the_work_queue_can_set_an_item_aside_without_opening_it():
    """Two destinations from one row. Separate side-by-side forms would each need
    their own grid cell and the columns would stop lining up, so the second button
    overrides the action with `formaction`."""
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(shorten=lambda v, n=80: v, money=lambda v: v,
                       micros=lambda v: v)
    env.globals["url_for"] = lambda n, **k: f"/{n}" + (
        f"/{k['sku']}" if "sku" in k else ""
    )
    env.globals["get_flashed_messages"] = lambda **k: []

    class Row(dict):
        __getattr__ = dict.get

    out = env.get_template("home.html").render(
        cards=[], done=[], forms={},
        working=[Row(sku="MP-000008", title="A camera", summary="find comparables")],
    )
    assert 'action="/run/MP-000008"' in out
    assert 'formaction="/abandon/MP-000008"' in out
    assert "set aside" in out


def test_the_queue_row_stays_one_form():
    """Nested forms are invalid HTML and browsers resolve them unpredictably."""
    import re

    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(shorten=lambda v, n=80: v, money=lambda v: v,
                       micros=lambda v: v)
    env.globals["url_for"] = lambda n, **k: f"/{n}"
    env.globals["get_flashed_messages"] = lambda **k: []

    class Row(dict):
        __getattr__ = dict.get

    out = env.get_template("home.html").render(
        cards=[], done=[], forms={},
        working=[Row(sku="MP-8", title="A camera", summary="x")],
    )
    row = re.search(r'<form[^>]*class="row".*?</form>', out, re.S).group(0)
    assert row.count("<form") == 1
    assert row.count("<button") == 2


def test_setting_aside_from_the_queue_removes_the_row(tmp_path):
    """End to end through the real app: press the button in the queue, the item
    leaves it, and pressing restore brings it back to the same place."""
    import re

    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    client = app.test_client()

    def queue(body):
        # The flash naming the item is not the item being in the queue.
        return sorted(set(re.findall(
            r"MP-\d{6}", re.sub(r'<ul class="flashes">.*?</ul>', "", body, flags=re.S)
        )))

    assert sku in queue(client.get("/ops").get_data(as_text=True))

    client.post(f"/items/{sku}/abandon", data={})
    assert sku not in queue(client.get("/ops").get_data(as_text=True))

    client.post(f"/items/{sku}/restore")
    assert sku in queue(client.get("/ops").get_data(as_text=True))


# --- a run that does not freeze the page -------------------------------------------


def test_pressing_run_answers_at_once(tmp_path):
    """It used to do the work first and answer afterwards, so the browser held one
    request for as long as the stages took -- measured at 67 seconds on a real
    item, 38 of them one host timing out twice."""
    import time

    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256="a" * 64, image_format="jpeg",
        size_bytes=1000, validation_errors=None,
    )
    client = app.test_client()

    started = time.monotonic()
    response = client.post(f"/items/{sku}/run")
    answered_in = time.monotonic() - started

    assert response.status_code == 302
    assert "run=" in response.headers["Location"]
    assert answered_in < 2.0, f"took {answered_in:.1f}s to acknowledge the click"


def test_a_second_click_does_not_start_a_second_agent(tmp_path):
    """Two runs would spend two budgets, write two sets of observations, and race
    each other through one state machine."""
    from resell import runs as runs_module

    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        ("run_open", sku, "running", db.now_iso()),
    )
    conn.commit()

    response = app.test_client().post(f"/items/{sku}/run")
    assert "run=run_open" in response.headers["Location"]
    assert conn.execute(
        "SELECT COUNT(*) FROM agent_run WHERE sku = ?", (sku,)
    ).fetchone()[0] == 1


def test_the_status_endpoint_reports_progress(tmp_path):
    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        ("run_1", sku, "running", db.now_iso()),
    )
    for n, (phase, message, ok) in enumerate([
        ("start", "finding comparable listings", 1),
        ("searching", "search 1/3: bowflex 552", 1),
        ("fetching", "bestbuy.com timed out after 20s; skipping it", 0),
    ]):
        conn.execute(
            "INSERT INTO agent_run_step (run_id, at, elapsed_ms, phase, message, ok)"
            " VALUES (?,?,?,?,?,?)",
            ("run_1", db.now_iso(), n * 1000, phase, message, ok),
        )
    conn.commit()

    body = app.test_client().get("/runs/run_1").get_json()
    assert body["running"] is True
    assert body["current"] == "bestbuy.com timed out after 20s; skipping it"
    assert body["problems"] == ["bestbuy.com timed out after 20s; skipping it"]
    assert len(body["steps"]) == 3


def test_a_timeout_is_visible_rather_than_silent(tmp_path):
    """The complaint was staring at an unchanged page while a fetch sat on a
    twenty-second timeout. The failing step is reported, and the run continues."""
    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at) VALUES (?,?,?,?)",
        ("run_2", sku, "running", db.now_iso()),
    )
    conn.execute(
        "INSERT INTO agent_run_step (run_id, at, elapsed_ms, phase, message, ok)"
        " VALUES (?,?,?,?,?,0)",
        ("run_2", db.now_iso(), 20000, "fetching", "bestbuy.com timed out after 20s"),
    )
    conn.commit()

    body = app.test_client().get("/runs/run_2").get_json()
    assert body["problems"]
    assert body["running"] is True          # a failed page is not a failed run


def test_a_finished_run_says_so(tmp_path):
    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at, finished_at, detail)"
        " VALUES (?,?,?,?,?,?)",
        ("run_3", sku, "done", db.now_iso(), db.now_iso(), "over to you: approve a price"),
    )
    conn.commit()

    body = app.test_client().get("/runs/run_3").get_json()
    assert body["running"] is False
    assert body["current"] == "over to you: approve a price"


def test_a_failed_run_says_what_went_wrong(tmp_path):
    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    conn.execute(
        "INSERT INTO agent_run (run_id, sku, status, started_at, finished_at, detail)"
        " VALUES (?,?,?,?,?,?)",
        ("run_4", sku, "failed", db.now_iso(), db.now_iso(), "RuntimeError: no adapter"),
    )
    conn.commit()

    body = app.test_client().get("/runs/run_4").get_json()
    assert body["status"] == "failed"
    assert "no adapter" in body["current"]


def test_an_unknown_run_is_a_404(tmp_path):
    app, _, _ = app_for(tmp_path)
    assert app.test_client().get("/runs/nope").status_code == 404


def test_the_long_forms_disable_themselves(tmp_path):
    """The half of double-click protection that happens before the round trip."""
    from pathlib import Path as _Path

    base = _Path("src/resell/webui/templates/base.html").read_text()
    home = _Path("src/resell/webui/templates/home.html").read_text()
    card = _Path("src/resell/webui/templates/_card.html").read_text()

    assert 'form.matches("[data-long]")' in base
    assert "b.disabled = true" in base
    for template, name in ((home, "home"), (card, "card")):
        assert "data-long" in template, name
        assert "data-busy" in template, name


# --- a question about an aspect eBay can answer ------------------------------------


def question_view(**kw):
    from resell.views import QuestionView

    base = dict(
        id=1, sku="MP-1", item_state="needs_info",
        question="Nothing observed supports a value for Model.",
        why_it_matters=None, blocking=True, asked_at="2026-08-23T00:00:00",
        aspect_name="Model", allowed_values=(), resolved_value=None,
        suggested_values=(),
    )
    base.update(kw)
    return QuestionView(**base)


def render_question(question):
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(money=lambda v: v, shorten=lambda v, n=80: v)
    env.globals["url_for"] = lambda n, **k: f"/{n}"

    class Card(dict):
        __getattr__ = dict.get

    return env.get_template("_card.html").render(
        card=Card(sku="MP-1", step="answer_questions", waiting_on_operator=True,
                  agent_is_working=False, active_run=None, can_search=False,
                  title="x", photo_positions=(), questions=[question],
                  candidates=(), summary="s", detail="d", state="needs_info",
                  is_done=False, has_price=False, ai_cost_micros=0, grant_calls=3,
                  grant_lookups=3, floor_cents=0, purchase_cost_cents=None),
        forms={},
    )


def test_ebays_values_are_visible_not_hidden_in_a_datalist():
    """MP-000016 asked for Model with a bare text box. eBay lists 204 models for
    that category and the card showed none of them, so the operator was asked to
    guess at something two clicks away."""
    out = render_question(question_view(
        suggested_values=("Compact", "Dash", "Handheld", "Helmet/Action"),
        aspect_name="Type",
    ))
    assert "<select" in out
    assert "Helmet/Action" in out
    assert "4 value(s) eBay lists" in out


def test_a_free_text_aspect_still_accepts_anything():
    """eBay's values for a FREE_TEXT aspect are recommendations. Turning them into
    a constraint would lock out an operator whose model eBay has not heard of."""
    out = render_question(question_view(suggested_values=("Compact", "Dash")))
    assert "or type your own below" in out
    assert "any value is accepted" in out
    assert "not one of eBay's values" not in out      # no override needed


def test_a_constrained_aspect_keeps_its_override():
    """Where eBay does impose a list, the answer is validated against it and the
    escape hatch stays."""
    out = render_question(question_view(allowed_values=("New", "Used")))
    assert "not one of eBay's values" in out
    assert "any value is accepted" not in out


def test_a_question_with_no_values_is_the_box_it_always_was():
    """Without credentials, or for an aspect eBay lists nothing for, the card
    degrades rather than showing an empty picker."""
    out = render_question(question_view())
    assert "<select" not in out
    assert 'placeholder="your answer"' in out


def test_the_picker_and_the_box_are_one_answer():
    from pathlib import Path as _Path

    source = _Path("src/resell/webui/app.py").read_text()
    assert 'request.form.get("choice")' in source
    assert "question_id, answer, operator=True" in source


def test_the_card_never_asks_for_links():
    """Comp research builds its own queries from the item's identity and reads a
    search index. There is no page for an operator to supply, and no extraction
    stage a supplied page could feed -- so asking for one would be asking for
    work that cannot be used."""
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/resell/webui/templates")
    )
    env.filters.update(money=lambda v: v, shorten=lambda v, n=80: v)
    env.globals["url_for"] = lambda *a, **k: "#"

    class Card(dict):
        __getattr__ = dict.get

    for waiting in (True, False):
        html = env.get_template("_card.html").render(
            card=Card(sku="MP-000001", step="comp_research", waiting=waiting,
                      summary="", detail="", questions=(), candidates=()),
        )
        assert 'name="urls"' not in html
        assert "paste" not in html.lower()


# --- setting a price by hand, when the market never turned up ------------------
#
# MP-000061 reached this screen with a correct identification and no comps, the
# seller clicked "Set my own price", and Flask returned 500. The route computed
# `build_strategies(rec, ...)` -- a name it never imported, from a variable it
# never assigned, into a variable nothing read. Both NameErrors were on one line
# added by the V1 pricing deletion, so the path had been dead since that commit
# and no test had ever POSTed to it. These do.


def priced_by_hand_fixture(tmp_path):
    """An item that has reached pricing with nothing to price from."""
    app, conn, gateway, sku = seeded(tmp_path, identified=True)
    gateway.begin_pricing(sku)
    conn.commit()
    return app, conn, gateway, sku


def test_setting_a_price_by_hand_records_a_proposal_and_approves_it(tmp_path):
    app, conn, gateway, sku = priced_by_hand_fixture(tmp_path)

    response = app.test_client().post(
        f"/items/{sku}/set-price", data={"price": "45.00", "why": "it is what it is"},
    )
    assert response.status_code in (302, 303), response.data[:400]

    proposals = list(conn.execute(
        "SELECT * FROM price_proposal WHERE sku = ?", (sku,)))
    assert len(proposals) == 1
    assert proposals[0]["price_cents"] == 4500
    approvals = list(conn.execute(
        "SELECT * FROM price_approval WHERE proposal_id = ?",
        (proposals[0]["proposal_id"],)))
    assert len(approvals) == 1, "a price that is not approved cannot reach a listing"


def test_a_hand_set_price_records_that_it_rests_on_nothing(tmp_path):
    """The fields that would normally carry evidence stay empty *and say so*. An
    empty band and a band nobody computed look identical afterwards otherwise."""
    app, conn, gateway, sku = priced_by_hand_fixture(tmp_path)
    app.test_client().post(f"/items/{sku}/set-price", data={"price": "45.00"})

    row = conn.execute("SELECT * FROM price_proposal WHERE sku = ?", (sku,)).fetchone()
    assert "operator_judgement" in (row["qualifiers_json"] or "")
    assert row["comp_set_id"] is None
    assert row["basis"] is None
    assert row["band_central_cents"] is None
    assert "no comparable evidence" in (row["rationale"] or "")


def test_a_rejected_price_mutates_nothing(tmp_path):
    """The failure mode that matters on this route: a half-applied price. Whatever
    goes wrong, the item must not end up with a proposal and no approval."""
    app, conn, gateway, sku = priced_by_hand_fixture(tmp_path)

    for bad in ("", "free", "0", "-10"):
        app.test_client().post(f"/items/{sku}/set-price", data={"price": bad})
    assert conn.execute(
        "SELECT COUNT(*) FROM price_proposal WHERE sku = ?", (sku,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM price_event WHERE sku = ?", (sku,)).fetchone()[0] == 0


def test_the_route_never_500s(tmp_path):
    """Written as its own assertion because that is what the seller actually met.
    `set_price` catches only ValueError and LookupError, so anything else -- a
    NameError in dead code, say -- reaches Flask as an Internal Server Error."""
    app, conn, gateway, sku = priced_by_hand_fixture(tmp_path)
    response = app.test_client().post(f"/items/{sku}/set-price", data={"price": "45.00"})
    assert response.status_code != 500, response.data[:600]


# --- screen-sized photographs -------------------------------------------------
#
# The shelf renders one card per item and served every one as the original: 34 of
# them came to 36.3 MB across 35 requests in a single page load, against twelve
# Waitress threads. The log recorded a queue depth of 37 twice, which is that page
# almost exactly. A phone does not need a 24-megapixel capture to decide which
# item to tap.
#
# Originals stay put. Everything that reasons about or sells the object -- the
# vision stage, eBay's uploader, the integrity check at publish -- reads
# `photo.source_path`, and a resampled image is evidence of a different thing.


def photo_fixture(tmp_path):
    """An item whose one photograph is a real, large JPEG on disk."""
    import hashlib

    from test_oauth import _jpeg_bytes  # noqa: PLC0415

    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    source = tmp_path / "big.jpg"
    source.write_bytes(_jpeg_bytes(2400, 1800) + b"\x00" * 40000)
    gateway.attach_photo(
        sku, source_path=str(source),
        content_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        image_format="jpeg", size_bytes=source.stat().st_size, validation_errors=None,
    )
    conn.commit()
    return app, conn, gateway, sku


def test_a_sized_request_is_served_as_that_rendition(tmp_path):
    app, conn, gateway, sku = photo_fixture(tmp_path)
    client = app.test_client()
    for size in ("thumb", "view"):
        r = client.get(f"/items/{sku}/photo/1?size={size}")
        assert r.status_code == 200, r.data[:200]
        assert r.headers.get("X-Photo-Size") == size


def test_no_size_still_serves_the_original(tmp_path):
    """The correction form and anything else wanting the real bytes."""
    app, conn, gateway, sku = photo_fixture(tmp_path)
    r = app.test_client().get(f"/items/{sku}/photo/1")
    assert r.status_code == 200
    assert r.headers.get("X-Photo-Size") == "original"


def test_an_unknown_size_falls_through_to_the_original(tmp_path):
    """A typo in a template should serve a large correct image, never a 404."""
    app, conn, gateway, sku = photo_fixture(tmp_path)
    r = app.test_client().get(f"/items/{sku}/photo/1?size=enormous")
    assert r.status_code == 200
    assert r.headers.get("X-Photo-Size") == "original"


def test_a_thumb_is_much_smaller_than_the_original(tmp_path):
    """The load-bearing claim, so it needs a real decodable photograph rather than
    the synthetic header the other tests use."""
    import hashlib

    pytest.importorskip("PIL", reason="needs a real image to downscale")
    from PIL import Image

    app, conn, gateway = app_for(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    source = tmp_path / "real.jpg"
    Image.effect_noise((2400, 1800), 64).convert("RGB").save(
        source, format="JPEG", quality=92)
    gateway.attach_photo(
        sku, source_path=str(source),
        content_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        image_format="jpeg", size_bytes=source.stat().st_size, validation_errors=None,
    )
    conn.commit()

    client = app.test_client()
    original = len(client.get(f"/items/{sku}/photo/1").data)
    thumb = len(client.get(f"/items/{sku}/photo/1?size=thumb").data)
    view = len(client.get(f"/items/{sku}/photo/1?size=view").data)

    assert thumb < view < original, (thumb, view, original)
    # The shelf is the page that mattered: 34 of these at full size was 36.3 MB.
    assert thumb < original / 4, f"thumb {thumb} vs original {original}"


def test_the_shelf_asks_for_thumbs_and_the_workspace_for_a_view(tmp_path):
    """Asserted on the templates, because getting the size right at the call site
    is the entire change -- the route only honours what it is asked for."""
    from pathlib import Path as _Path

    templates = _Path("src/resell/webui/templates")
    for name, expected in (
        ("consumer/shelf.html", "size='thumb'"),
        ("_card.html", "size='thumb'"),
        ("consumer/workspace.html", "size='view'"),
        ("consumer/_review.html", "size='view'"),
    ):
        body = (templates / name).read_text()
        assert "url_for('photo'" in body, name
        assert expected in body, f"{name} should request {expected}"


def test_the_original_file_is_never_replaced(tmp_path):
    """The digest recorded at upload is what publish checks against."""
    import hashlib

    app, conn, gateway, sku = photo_fixture(tmp_path)
    row = conn.execute("SELECT source_path, content_sha256 FROM photo WHERE sku=?",
                       (sku,)).fetchone()
    before = hashlib.sha256(Path(row["source_path"]).read_bytes()).hexdigest()

    client = app.test_client()
    for size in ("thumb", "view", ""):
        client.get(f"/items/{sku}/photo/1?size={size}")

    after = hashlib.sha256(Path(row["source_path"]).read_bytes()).hexdigest()
    assert after == before == row["content_sha256"]
