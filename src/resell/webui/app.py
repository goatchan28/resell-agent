"""Two screens. Upload, decide, approve.

The previous UI mirrored the CLI: a page per stage, a tab per concept, and an
operator who still had to know that aspects precede drafting. This one is built
the other way round -- the orchestrator says what an item needs, and the screen
shows that one thing.

  /            every item that wants something, and the one decision each wants
  /inventory   the table, for everything else

Flask's job is unchanged and still small: HTTP in, template out. Reads go through
`resell.views`, writes go through the gateway, the pricing store or the
orchestrator. Nothing here computes.

The rule that shaped the routes: an operator action is one POST. Accepting a
comparable writes an observation-claim pair; approving a price writes a proposal
and an approval. Those are two rows each and one decision each, and the interface
shows the decision.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from resell import db, runs, store_pricing as sp, views, views_consumer
from resell.config import ConfigError, load_config
from resell.domain import FeeModel
from resell.gateway import Gateway, Rejected
from resell.orchestrator import advance
from resell.images import inspect

_NEEDS_DERIVATIVE = {"heic", "heif", "tiff"}


def _comp_adapter(sku: str, urls: list[str]):
    """Search for comps if we can; read what was pasted if we cannot.

    Supplied URLs win when present: an operator who went and found a page has
    made a judgement the search backend has not, and overriding it would be
    perverse. With no URLs and a backend configured, discovery is the agent's.
    """
    from resell.reasoning.adapters.marketplace import (
        SearchedMarketplaceAdapter, SuppliedUrlsMarketplaceAdapter,
    )
    from resell.reasoning.adapters.search import NoSearchBackend, get_search_backend

    if urls:
        return SuppliedUrlsMarketplaceAdapter(urls, echo=lambda *a: None)
    backend = get_search_backend()
    if isinstance(backend, NoSearchBackend):
        return None
    return SearchedMarketplaceAdapter(
        backend, identity_terms=views.identity_terms(g.conn, sku), echo=lambda *a: None,
    )


def create_app(*, config=None) -> Flask:
    app = Flask(__name__)
    app.secret_key = hashlib.sha256(str(id(app)).encode()).hexdigest()
    app.config["RESELL_CONFIG"] = config
    app.jinja_env.filters["money"] = _money
    app.jinja_env.filters["shorten"] = _shorten
    app.jinja_env.filters["micros"] = _micros

    @app.before_request
    def _open_database() -> None:
        g.config = app.config["RESELL_CONFIG"] or load_config(require_credentials=False)
        g.conn = db.connect(g.config.db_path)
        g.gateway = Gateway(
            g.conn, marketplace=g.config.marketplace_id,
            environment=g.config.env.name, fees=FeeModel(),
        )

    @app.teardown_appcontext
    def _close_database(_exception) -> None:
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def _environment():
        return {
            "environment": g.config.env.name,
            "marketplace": g.config.marketplace_id,
        }

    _register_routes(app)
    return app


def _register_routes(app: Flask) -> None:

    # --- the two screens ------------------------------------------------------

    # --- the consumer's two screens ------------------------------------------
    #
    # GET only. Every action on them posts to the same endpoints the operator UI
    # uses, so there is one workflow, one set of approval seams, and one
    # orchestrator. These routes choose words; they decide nothing.

    @app.get("/")
    def home():
        """Whatever needs doing next, and nothing else.

        One item at a time is the whole design: a seller with four items in
        flight wants to know which one wants them, not to read four status
        reports. Items the agent is still working on appear as a quiet line.
        """
        rows = views.inventory(
            g.conn, marketplace=g.config.marketplace_id,
            environment=g.config.env.name,
        )
        waiting = [r for r in rows if r.actor == "operator"]
        working = [r for r in rows if r.actor == "agent"]
        tasks = [views_consumer.task_view(_workflow(r.sku)) for r in waiting]
        return render_template(
            "consumer/home.html",
            tasks=tasks,
            working=views_consumer.shelf_rows(working, net_of=_net_of),
            forms={t.sku: _correction_form(t.sku) for t in tasks},
            run=_run_in_view([r.sku for r in rows]),
        )

    @app.get("/items/<sku>")
    def item(sku: str):
        """One item's task, on its own."""
        _require_item(sku)
        task = views_consumer.task_view(_workflow(sku))
        return render_template(
            "consumer/home.html", tasks=[task], working=[],
            forms={sku: _correction_form(sku)}, run=_run_in_view([sku]),
        )

    @app.get("/items")
    def shelf():
        """Everything the seller has, as a list they would recognise."""
        rows = views.inventory(
            g.conn, marketplace=g.config.marketplace_id,
            environment=g.config.env.name,
            include_abandoned=request.args.get("aside") == "1",
        )
        return render_template(
            "consumer/shelf.html",
            rows=views_consumer.shelf_rows(rows, net_of=_net_of),
            showing_aside=request.args.get("aside") == "1",
            aside_count=views.abandoned_count(g.conn),
        )

    @app.get("/ops")
    def ops_home():
        """Everything that wants a decision, and nothing that does not.

        Items the agent can still work on are listed separately and quietly: an
        operator does not need to watch them, only to know they exist.
        """
        rows = views.inventory(
            g.conn, marketplace=g.config.marketplace_id,
            environment=g.config.env.name,
        )
        waiting = [r for r in rows if r.actor == "operator"]
        working = [r for r in rows if r.actor == "agent"]
        cards = [_workflow(r.sku) for r in waiting]
        return render_template(
            "home.html", cards=cards, working=working,
            done=[r for r in rows if r.actor == "nobody"],
            forms={card.sku: _correction_form(card.sku) for card in cards},
            run=_run_in_view([r.sku for r in rows]),
        )

    @app.get("/ops/inventory")
    def ops_inventory():
        # Abandoned items are hidden unless asked for. They are kept whole, so the
        # only reason to hide them is that a list of work should be a list of work.
        show_abandoned = request.args.get("abandoned") == "1"
        return render_template(
            "inventory.html",
            rows=views.inventory(
                g.conn, marketplace=g.config.marketplace_id,
                environment=g.config.env.name, include_abandoned=show_abandoned,
            ),
            show_abandoned=show_abandoned,
            abandoned_count=views.abandoned_count(g.conn),
        )

    @app.get("/ops/items/<sku>")
    def ops_item(sku: str):
        """One item's card on its own, for when the home screen is crowded."""
        return render_template(
            "home.html", cards=[_workflow(sku)], working=[], done=[],
            forms={sku: _correction_form(sku)}, run=_run_in_view([sku]),
        )

    @app.get("/items/<sku>/photo/<int:position>")
    def photo(sku: str, position: int):
        from resell.derivatives import ConversionError, for_model

        detail = views.item_detail(
            g.conn, g.gateway, sku, marketplace=g.config.marketplace_id,
            environment=g.config.env.name,
        )
        match = next((p for p in detail.photos if p.position == position), None)
        if match is None:
            abort(404)
        source = Path(match.source_path)
        if not source.exists():
            abort(404)
        if (match.image_format or "").lower() not in _NEEDS_DERIVATIVE:
            return send_file(source.resolve())
        try:
            derivative = for_model(
                source, Path(g.config.db_path).resolve().parent / "derivatives",
                digest=match.content_sha256,
            )
        except ConversionError:
            abort(415)
        return send_file(Path(derivative).resolve())

    # --- the one write that starts everything ---------------------------------

    @app.post("/items")
    def create_item():
        """Upload photos. That is the whole intake form.

        Cost is optional and asked for here only because it is the one fact the
        photographs cannot contain and the operator always knows at this moment.
        """
        try:
            accepted = g.gateway.ingest_item(
                purchase_cost_cents=_cents(request.form.get("cost_dollars")),
                acquisition_intent=request.form.get("intent") or "resale",
                notes=request.form.get("notes") or None,
            )
        except Rejected as exc:
            _flash_rejection(exc)
            return redirect(url_for("home"))

        sku = accepted.sku
        attached, failed = _attach_uploads(sku, request.files.getlist("photos"))
        for message in failed:
            flash(message, "error")
        if not attached:
            flash(f"{sku} created, but no photo attached yet.", "error")
            return redirect(url_for("home"))
        flash(f"{sku}: {attached} photo(s). Working on it.", "ok")
        # The same mechanism the Run button uses. Upload used to do the work
        # inside the request, so the browser sat on the upload for as long as
        # identification took -- with no progress, which is the one moment an
        # operator most wants to see something happening.
        run_id, _ = _start_agent(sku)
        return redirect(url_for("item", sku=sku, run=run_id))

    @app.post("/items/<sku>/photos")
    def add_photos(sku: str):
        _require_item(sku)
        attached, failed = _attach_uploads(sku, request.files.getlist("photos"))
        for message in failed:
            flash(message, "error")
        if attached:
            flash(f"{attached} photo(s) added", "ok")
            run_id, _ = _start_agent(sku)
            return redirect(url_for("item", sku=sku, run=run_id))
        return redirect(url_for("item", sku=sku))

    # --- let the agent work ---------------------------------------------------

    @app.post("/items/<sku>/run")
    def run(sku: str):
        """Start the agent and hand back at once. The work happens on a thread.

        This used to do the work first and answer afterwards, which meant a
        browser request held open for as long as the stages took -- two minutes on
        an item that searched, fetched and judged -- with nothing on the screen.
        The reasoning was that a background worker needs a job table and a status
        channel; it turned out to need one small table and one polling route,
        which is a great deal less than two minutes of a frozen page costs.

        A second click while a run is in flight is answered with the run already
        going, not with a second agent: two runs would spend two budgets, write
        two sets of observations, and race each other through one state machine.
        """
        _require_item(sku)
        run_id, already = _start_agent(sku)
        if already:
            flash(f"{sku} is already running", "reason")
        return redirect(url_for("item", sku=sku, run=run_id))

    @app.get("/runs/<run_id>")
    def run_status(run_id: str):
        """What the run is doing now. Polled by the page; cheap and read-only."""
        view = runs.read_run(g.conn, run_id)
        if view is None:
            return {"error": "no such run"}, 404
        return {
            "run_id": view.run_id,
            "sku": view.sku,
            "status": view.status,
            "running": view.running,
            "current": view.current,
            "elapsed_ms": view.elapsed_ms,
            "problems": list(view.problems),
            "steps": [
                {"phase": s.phase, "message": s.message,
                 "elapsed_ms": s.elapsed_ms, "ok": s.ok}
                for s in view.steps
            ],
        }

    # --- the three decisions --------------------------------------------------

    @app.post("/questions/<int:question_id>/answer")
    def answer(question_id: int):
        # The picker and the box are one answer: whichever the operator used.
        # Two controls for one field is the price of showing eBay's list without
        # making it a constraint on aspects where eBay does not impose one.
        answer = (request.form.get("choice") or "").strip() or (
            request.form.get("answer") or ""
        ).strip()
        sku = request.form.get("sku", "")
        try:
            g.gateway.answer_question(
                question_id, answer, operator=True,
                value_not_listed=request.form.get("value_not_listed") == "1",
            )
        except Rejected as exc:
            _flash_rejection(exc)
            return redirect(url_for("item", sku=sku))
        # Answering the last blocking question is exactly when the agent should
        # carry on, so it does -- here, rather than by redirecting into a
        # POST-only route, which a browser follows with GET.
        run_id, _ = _start_agent(sku)
        return redirect(url_for("item", sku=sku, run=run_id))

    @app.post("/candidates/<candidate_id>/accept")
    def accept_candidate(candidate_id: str):
        """One action; the observation and the claim are both this route's problem.

        The operator sees a listing and decides whether it is the same sort of
        thing. Which of the two rows already existed and which one this writes is
        not a question the interface should raise.
        """
        sku = request.form.get("sku", "")
        try:
            sp.accept_comp_candidate(
                g.conn, candidate_id,
                identity_resolution=_identity_resolution(sku),
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("item", sku=sku))
        # Handing back to the agent, like every other decision. Without this the
        # item sat at an agent-owned step with nothing running: the screen said
        # "Finding a price" over a spinner and no work was happening.
        run_id, _ = _start_agent(sku)
        return redirect(url_for("item", sku=sku, run=run_id))

    @app.post("/candidates/<candidate_id>/reject")
    def reject_candidate(candidate_id: str):
        sku = request.form.get("sku", "")
        reason = request.form.get("reason") or "not comparable"
        try:
            sp.reject_comp_candidate(g.conn, candidate_id, reason=reason)
        except ValueError as exc:
            flash(str(exc), "error")
        # Handing back to the agent, like every other decision. Without this the
        # item sat at an agent-owned step with nothing running: the screen said
        # "Finding a price" over a spinner and no work was happening.
        run_id, _ = _start_agent(sku)
        return redirect(url_for("item", sku=sku, run=run_id))

    @app.post("/items/<sku>/price")
    def approve_price(sku: str):
        """Propose and approve in one action, because it is one decision.

        `price propose` freezes a comp set and claims an objective; `price approve`
        binds it. Splitting them matters to the record and not to the person, who
        is choosing between three numbers they can see.
        """
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        _require_item(sku)
        from resell.pricing.strategy import SellerObjective

        objective = request.form.get("objective") or "balanced"
        try:
            proposal_id = _propose_price(sku, SellerObjective(objective))
        except (ValueError, LookupError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("item", sku=sku))
        flash(f"price approved ({objective})", "ok")
        run_id, _ = _start_agent(sku)
        return redirect(url_for("item", sku=sku, run=run_id))

    @app.post("/items/<sku>/more-research")
    def more_research(sku: str):
        """Give this one item another round's worth of allowance.

        A button because the alternative is editing `.env` and restarting the
        server, which turns "this one is worth another look" into an operations
        task. Per item, so it cannot quietly become a global raise.
        """
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        _require_item(sku)
        from resell.orchestrator import GRANT_CALLS, GRANT_LOOKUPS, grant_more_research

        grant_more_research(g.conn, sku)
        flash(f"{sku}: {GRANT_CALLS} more research call(s) and {GRANT_LOOKUPS} more "
              f"search(es) for this item", "ok")
        run_id, _ = _start_agent(sku)
        return redirect(url_for("item", sku=sku, run=run_id))

    @app.post("/items/<sku>/set-price")
    def set_price(sku: str):
        """Take the operator's own number when the evidence never arrived.

        Through the proposal seam, not around it: `propose_listing` refuses a
        price with no matching approval, so this has to become a real proposal.
        What it carries instead of evidence is `operator_judgement`.
        """
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        _require_item(sku)
        raw = (request.form.get("price") or "").strip().lstrip("$").replace(",", "")
        try:
            price_cents = round(float(raw) * 100)
        except ValueError:
            flash(f"{raw!r} is not a price", "error")
            return redirect(url_for("item", sku=sku))
        if price_cents <= 0:
            flash("a price has to be more than nothing", "error")
            return redirect(url_for("item", sku=sku))

        # Checked on the way in, not only previewed. A price typed into a box and
        # submitted without looking at the preview should still meet something.
        check = views.price_check(
            g.conn, sku, price_cents, marketplace=g.config.marketplace_id
        )
        if not check.above_floor and not request.form.get("confirm_below_floor"):
            flash(f"{sku}: {check.warning}", "ask")
            flash("submit again with the box ticked if that is what you meant",
                  "reason")
            return redirect(url_for("item", sku=sku))
        if check.warning:
            flash(f"{sku}: {check.warning}", "reason")

        try:
            _propose_operator_price(
                sku, price_cents, request.form.get("why") or "",
            )
        except (ValueError, LookupError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("item", sku=sku))
        flash(f"{sku}: priced at ${price_cents / 100:.2f} on your judgement "
              f"(net ${check.net_cents / 100:.2f} after fees), with no comparable "
              f"evidence behind it", "ok")
        run_id, _ = _start_agent(sku)
        return redirect(url_for("item", sku=sku, run=run_id))

    @app.post("/items/<sku>/approve-listing")
    def approve_listing(sku: str):
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        detail = views.item_detail(
            g.conn, g.gateway, sku, marketplace=g.config.marketplace_id,
            environment=g.config.env.name,
        )
        if not detail.proposal_hash:
            flash("nothing proposed to approve", "error")
            return redirect(url_for("item", sku=sku))
        try:
            g.gateway.approve(sku, detail.proposal_hash, operator=True)
        except Rejected as exc:
            _flash_rejection(exc)
            return redirect(url_for("item", sku=sku))
        flash("listing approved", "ok")
        return redirect(url_for("item", sku=sku))

    @app.post("/items/<sku>/comps")
    def find_comps(sku: str):
        """Read the listings the operator pasted, and offer them as candidates.

        The links are the operator's contribution; everything after is the
        agent's. Nothing here prompts -- `SuppliedUrlsMarketplaceAdapter` takes the
        list up front, which is what stops a web request blocking on stdin.
        """
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        _require_item(sku)
        from resell.reasoning.budget import BudgetExceeded
        from resell.reasoning.comp_loop import CompLoopError, run_comp_round

        urls = [
            line.strip()
            for line in (request.form.get("urls") or "").splitlines()
            if line.strip()
        ]
        adapter = _comp_adapter(sku, urls)
        if adapter is None:
            flash("paste at least one link, or configure a search backend", "error")
            return redirect(url_for("item", sku=sku))
        try:
            outcome = run_comp_round(
                g.conn, g.gateway, sku, research_adapter=adapter, propose_only=True,
            )
        except BudgetExceeded as exc:
            # An ordinary stop, not a fault. The guard did its job; saying so as a
            # 500 traceback told the operator the tool was broken when it was
            # working. Anything already recorded before the limit stands.
            flash(f"stopped on this item's budget: {exc}", "ask")
            flash("raise it for this run with RESELL_BUDGET_COMP_RESEARCH_MAX_CALLS, "
                  "or accept what was found so far", "reason")
            return redirect(url_for("item", sku=sku))
        except CompLoopError as exc:
            flash(str(exc), "error")
            return redirect(url_for("item", sku=sku))

        for note in adapter.notes:
            flash(note, "error")
        for note in outcome.notes:
            flash(note[:160], "error")
        if outcome.candidates_offered:
            flash(f"{outcome.candidates_offered} comparable(s) to review", "ask")
        elif outcome.stopped:
            flash(outcome.stop_reason[:200], "error")
        else:
            flash("nothing usable came back from those links", "error")
        return redirect(url_for("item", sku=sku))

    @app.post("/items/<sku>/fix")
    def fix(sku: str):
        """Correct the identification, and put the listing back if it had moved on.

        `propose_identification` supersedes rather than edits, so a correction is a
        new version and the old one stays readable. The second half matters as
        much: once a listing has been proposed, the listing row and any approval
        are bound to the old content, and changing the identification underneath
        them would leave an approval covering words nobody approved. Revising puts
        the item back to pricing, which voids the approval and lets the
        orchestrator rebuild the listing from the corrected record.
        """
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        _require_item(sku)
        from resell.cli_item import merged_identification
        from resell.domain import ItemState

        supplied = {
            name: request.form.get(name, "").strip()
            for name in ("title", "description", "condition_id", "category_id")
            if request.form.get(name, "").strip()
        }
        if not supplied:
            flash("nothing to change", "error")
            return redirect(url_for("item", sku=sku))

        fields, _ = merged_identification(g.conn, sku, **supplied)
        try:
            g.gateway.propose_identification(sku, **fields)
        except Rejected as exc:
            _flash_rejection(exc)
            return redirect(url_for("item", sku=sku))

        state = g.conn.execute(
            "SELECT state FROM item WHERE sku = ?", (sku,)
        ).fetchone()[0]
        if state in (str(ItemState.PROPOSED), str(ItemState.APPROVED),
                     str(ItemState.PUBLISH_FAILED)):
            try:
                g.gateway.revise(sku, reason="operator corrected the listing details")
                flash("the listing goes back for approval, since its words changed",
                      "ask")
            except Rejected as exc:
                _flash_rejection(exc)
                return redirect(url_for("item", sku=sku))

        # Back to the card, not on to `run`. Correcting a field is an edit, and an
        # edit that silently starts a chain of paid model calls is a surprise --
        # the operator may well want to change three things before carrying on.
        flash("updated: " + ", ".join(sorted(supplied)), "ok")
        return redirect(url_for("item", sku=sku))

    @app.post("/items/<sku>/publish")
    def publish(sku: str):
        """The one outward-facing action, and the only one that leaves this machine.

        Deliberately not part of `run`: everything else the orchestrator does is
        local and reversible, and this puts an item up for sale under the
        operator's account. It stays a separate, named press.
        """
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        _require_item(sku)
        from resell.ebay.client import EbayClient
        from resell.ebay.publisher import PublishAborted, Publisher

        try:
            config = load_config(require_credentials=True)
        except ConfigError as exc:
            flash(f"eBay credentials are not configured: {exc}", "error")
            return redirect(url_for("item", sku=sku))

        try:
            with EbayClient(config, g.conn) as client:
                steps = Publisher(g.gateway, client, g.conn).publish(sku)
        except (PublishAborted, Rejected) as exc:
            reasons = exc.reasons if isinstance(exc, Rejected) else [str(exc)]
            flash("publish aborted before any eBay write", "error")
            for reason in reasons:
                flash(str(reason), "reason")
            flash("fix the details below and it will go back for approval", "ask")
            return redirect(url_for("item", sku=sku))

        failed = [s for s in steps if not s.ok]
        for step in failed:
            flash(f"{step.name}: {step.detail}", "error")
        listing_id = next(
            (s.data.get("listingId") for s in reversed(steps) if s.data.get("listingId")),
            None,
        )
        if listing_id:
            flash(f"published as {listing_id}", "ok")
        return redirect(url_for("item", sku=sku))

    @app.post("/items/<sku>/confirm-identity")
    def confirm_identity(sku: str):
        """Agree that this is what the thing is, when the agent could not tell.

        Not a rubber stamp on the agent's work -- an item whose identity research
        resolved never reaches here. This is the operator supplying the one thing
        the record is missing, so a pricing budget is not spent on a guess.
        """
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        _require_item(sku)
        from resell.orchestrator import confirm_identity as record

        record(g.conn, sku, note=request.form.get("note") or "")
        flash(f"{sku}: identification confirmed. Moving on to pricing.", "ok")
        run_id, _ = _start_agent(sku)
        return redirect(url_for("item", sku=sku, run=run_id))

    @app.post("/items/<sku>/abandon")
    def abandon(sku: str):
        """Stop work on an item without losing any of it.

        Every record survives -- photos, evidence, research, what it cost, what was
        proposed. The item leaves the work queue and the runner will not touch it,
        and `restore` brings it back to the same state it left.
        """
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        try:
            accepted = g.gateway.abandon(
                sku, reason=request.form.get("reason") or "set aside by the operator",
            )
        except Rejected as exc:
            _flash_rejection(exc)
            return redirect(url_for("item", sku=sku))
        flash(f"{sku}: set aside from {accepted.from_state}. Nothing was deleted — "
              f"find it under Inventory, showing abandoned.", "ok")
        return redirect(url_for("home"))

    @app.post("/items/<sku>/restore")
    def restore(sku: str):
        """Put an abandoned item back where it was.

        The state comes from the event log, not from this request: an item returns
        to where it actually was, and nowhere else.
        """
        busy = _busy(sku)
        if busy:
            flash(f"{sku}: the agent is working on this one — that has to finish "
                  f"first", "ask")
            return redirect(url_for("item", sku=sku, run=busy))
        try:
            accepted = g.gateway.restore(
                sku, reason=request.form.get("reason") or "brought back by the operator",
            )
        except Rejected as exc:
            _flash_rejection(exc)
            return redirect(url_for("shelf", aside=1))
        flash(f"{sku}: back in {accepted.to_state}, where it was when you set it "
              f"aside", "ok")
        return redirect(url_for("item", sku=sku))


# --- helpers -----------------------------------------------------------------


def _busy(sku: str) -> str | None:
    """The run holding this item, if one is. Routes that change state ask first.

    Hiding a button is a convenience; this is the rule. A transition applied while
    the worker thread walks the same item through the same state machine is a race
    whichever direction it goes -- advancing it twice, or setting it aside out from
    under itself.
    """
    return runs.active_run_for(g.conn, sku)


def _start_agent(sku: str) -> tuple[str, bool]:
    """Begin an agent run on this item, or join the one already going.

    One helper for every route that starts agent work -- Run, upload, and adding
    photos to an existing item. They used to differ: Run went through the run
    machinery and the two upload paths did the work inside the request, so the
    same long operation had progress in one place and a frozen page in the other.
    Returns the run id and whether it was already in flight.
    """
    existing = runs.active_run_for(g.conn, sku)
    if existing:
        return existing, True

    config = g.config

    def work(conn, _reporter):
        from resell.domain import FeeModel
        from resell.gateway import Gateway

        gateway = Gateway(
            conn, marketplace=config.marketplace_id,
            environment=config.env.name, fees=FeeModel(),
        )
        report = advance(conn, gateway, sku, config=config)
        if report.errors:
            raise RuntimeError(report.errors[0])
        if report.halts:
            return report.halts[-1]
        if report.stopped_at and report.stopped_at.waiting_on_operator:
            return f"over to you: {report.stopped_at.summary}"
        return report.ran[-1] if report.ran else "nothing to do"

    return runs.start_run(config.db_path, sku, work), False


def _net_of(price_cents: int) -> int:
    """Net proceeds on a price: what is left after eBay and postage.

    The same `net_from_gross` the pricing layer uses, so the shelf and the price
    screen cannot disagree about what an item earns. Returns the price unchanged
    if the fee schedule cannot be read -- a shelf without a profit column beats no
    shelf.
    """
    from resell.pricing.proceeds import CostLines, net_from_gross

    try:
        schedule, _version = sp.recorded_schedule(
            g.conn, marketplace=g.config.marketplace_id, category_id=None,
        )
        return net_from_gross(price_cents, schedule=schedule,
                              costs=CostLines()).net_cents
    except Exception:  # noqa: BLE001 - a missing schedule is not a broken page
        return price_cents


def _run_in_view(skus=()):
    """The run this page should show progress for.

    The query string wins: a page opened by pressing a button watches the run
    that button started. Failing that, a run in flight on one of the items the
    page is already showing.

    That second case is what was missing. Progress only appeared if `?run=` was
    in the URL, so navigating to the home screen while the agent worked showed a
    line of text and no sign of movement -- the work was happening and the page
    had no way to say so.

    Still not "whatever is running anywhere": a page reporting progress on an item
    it is not displaying would be describing something the reader cannot see.
    """
    run_id = request.args.get("run")
    if run_id:
        return runs.read_run(g.conn, run_id)
    for sku in skus:
        active = runs.active_run_for(g.conn, sku)
        if active:
            return runs.read_run(g.conn, active)
    return None


def _correction_form(sku: str) -> views.CorrectionForm:
    """Editable identification, with eBay's own vocabulary where it has one.

    Credentials are asked for optionally: without them the condition field falls
    back to free text and says why, rather than the whole card failing over a
    lookup that is a convenience.
    """
    injected = current_app.config["RESELL_CONFIG"]
    if injected is not None:
        # Honour the config this app was built with. Falling back to the process
        # environment would let a test or an embedding caller reach eBay with
        # credentials it deliberately did not supply.
        config = injected if injected.has_credentials else None
    else:
        try:
            config = load_config(require_credentials=True)
        except ConfigError:
            config = None
    return views.correction_form(
        g.conn, sku, config=config, gateway=g.gateway if config else None
    )


def _workflow(sku: str) -> views.WorkflowView:
    return views.workflow_view(
        g.conn, g.gateway, sku,
        marketplace=g.config.marketplace_id, environment=g.config.env.name,
        # So a question about an aspect can offer eBay's values instead of a bare
        # box. Optional everywhere below it: without credentials the card degrades
        # to free text and says nothing it cannot back up.
        config=g.config,
    )


def _require_item(sku: str) -> None:
    try:
        views.item_detail(
            g.conn, g.gateway, sku, marketplace=g.config.marketplace_id,
            environment=g.config.env.name,
        )
    except Rejected:
        abort(404)


def _identity_resolution(sku: str) -> str:
    from resell.reasoning.research_loop import identity_resolution

    return str(identity_resolution(g.conn, sku))


def _propose_price(sku: str, objective) -> str:
    """Freeze the comp set, record the proposal, approve it. All three or none.

    Lives here rather than in a template or a view because it is a sequence of
    existing store calls; nothing about the arithmetic is decided in this
    function, and the objective it passes is the operator's own choice.
    """
    import uuid

    from resell.pricing.estimate import PricingInput, recommend
    from resell.pricing.lifecycle import PriceProposal, PriceReason
    from resell.pricing.proceeds import CostLines, net_from_gross
    from resell.pricing.strategy import build_strategies

    request_values = views.default_pricing_request(
        g.conn, sku, marketplace=g.config.marketplace_id
    )
    scored = sp.load_scored_comps(g.conn, sku)
    rec = recommend(PricingInput(
        sku=sku,
        item_condition_band=_band(request_values.condition_band),
        identity_resolution=request_values.identity_resolution,
        comps=tuple(scored),
        window_days=request_values.window_days,
    ))
    if rec.unpriceable:
        raise ValueError(f"unpriceable: {rec.reason}")

    schedule, schedule_version = sp.recorded_schedule(
        g.conn, marketplace=g.config.marketplace_id,
        category_id=request_values.category_id,
    )
    costs = CostLines(seller_paid_shipping_cents=request_values.shipping_cost_cents)
    strategies = build_strategies(rec, schedule=schedule, costs=costs)
    if strategies is None:
        raise ValueError("the evidence supports no strategy")
    chosen = strategies.get(objective)

    set_id, set_hash = sp.freeze_comp_set(
        g.conn, sku, scored, window_days=request_values.window_days,
        aggregate=sp.aggregate_for_storage(rec),
    )
    proceeds = net_from_gross(chosen.price_cents, schedule=schedule, costs=costs)
    proposal = PriceProposal(
        proposal_id=f"price_{uuid.uuid4().hex[:12]}",
        sku=sku,
        reason=PriceReason("initial"),
        price_cents=chosen.price_cents,
        created_at=_now(),
        basis=rec.basis,
        price_kind=rec.price_kind,
        comp_set_id=set_id,
        comp_set_hash=set_hash,
        band_low_cents=rec.band_low_cents,
        band_central_cents=rec.band_central_cents,
        band_high_cents=rec.band_high_cents,
        qualifiers=rec.qualifiers,
        fee_schedule_version=schedule_version,
        fee_basis=schedule.basis,
        net_proceeds_cents=proceeds.net_cents,
        floor_ok=True,
        rationale=f"operator chose {objective} from the recommendation",
        objective=objective,
        anchor_statistic=str(chosen.anchor.statistic),
        anchor_value_cents=chosen.anchor.value_cents,
    )
    sp.record_proposal(
        g.conn, proposal,
        uncertainty_note=strategies.uncertainty_note,
        sold_evidence_note=strategies.sold_evidence_note,
        sample_exclusions=rec.sample_exclusions,
    )
    sp.approve_proposal(g.conn, proposal)
    return proposal.proposal_id


def _propose_operator_price(sku: str, price_cents: int, rationale: str) -> str:
    """Record a price the operator set themselves, through the same seam.

    Not a bypass. `propose_listing` refuses a price that does not match an
    approved one, so a number typed into a box has to become a proposal and an
    approval or it cannot reach a listing at all -- and routing around that would
    put an unapproved price on eBay.

    What it does not have is evidence, and the record says so rather than leaving
    the fields that would normally carry it merely empty: no comp set, no band, no
    basis, and `operator_judgement` on the proposal. An empty band and a band
    nobody computed look identical afterwards otherwise.
    """
    import uuid

    from resell.pricing.estimate import PriceQualifier
    from resell.pricing.lifecycle import PriceProposal, PriceReason
    from resell.pricing.proceeds import CostLines, net_from_gross

    request_values = views.default_pricing_request(
        g.conn, sku, marketplace=g.config.marketplace_id
    )
    schedule, schedule_version = sp.recorded_schedule(
        g.conn, marketplace=g.config.marketplace_id,
        category_id=request_values.category_id,
    )
    costs = CostLines(seller_paid_shipping_cents=request_values.shipping_cost_cents)
    proceeds = net_from_gross(price_cents, schedule=schedule, costs=costs)

    proposal = PriceProposal(
        proposal_id=f"price_{uuid.uuid4().hex[:12]}",
        sku=sku,
        reason=PriceReason("initial"),
        price_cents=price_cents,
        created_at=_now(),
        # basis, price_kind, comp_set and band all stay None: there was no sample.
        qualifiers=(PriceQualifier.OPERATOR_JUDGEMENT,),
        fee_schedule_version=schedule_version,
        fee_basis=schedule.basis,
        net_proceeds_cents=proceeds.net_cents,
        floor_ok=True,
        rationale=rationale or "set by the operator; no comparable evidence",
    )
    sp.record_proposal(g.conn, proposal)
    sp.approve_proposal(g.conn, proposal)
    return proposal.proposal_id


def _band(name: str):
    from resell.pricing.comps import ConditionBand

    return ConditionBand(name)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _attach_uploads(sku: str, uploads) -> tuple[int, list[str]]:
    directory = Path(g.config.db_path).parent / "uploads" / sku
    directory.mkdir(parents=True, exist_ok=True)

    attached, failures = 0, []
    for upload in uploads:
        name = Path(upload.filename or "").name
        if not name:
            continue
        destination = directory / name
        upload.save(destination)
        facts = inspect(destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        try:
            g.gateway.attach_photo(
                sku, source_path=str(destination.resolve()),
                content_sha256=digest, image_format=facts.image_format,
                size_bytes=facts.size_bytes,
                validation_errors=facts.errors or None,
            )
        except Rejected as exc:
            failures.append(f"{name}: " + "; ".join(exc.reasons))
            continue
        attached += 1
        for error in facts.errors:
            failures.append(f"{name} attached but INVALID: {error}")
    return attached, failures


def _flash_rejection(exc: Rejected) -> None:
    flash(f"refused: {exc.command}", "error")
    for reason in exc.reasons:
        flash(reason, "reason")


def _cents(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return round(float(raw) * 100)
    except ValueError:
        return None


def _money(cents, unknown: str = "—") -> str:
    return unknown if cents is None else f"${cents / 100:,.2f}"


def _micros(micros, unknown: str = "—") -> str:
    """Millionths of a currency unit, at a resolution that shows a fraction of a
    cent. A stage that costs $0.0043 reads as zero at two decimal places, and the
    whole point of the figure is to see where it goes."""
    if not micros:
        return unknown
    return f"${micros / 1_000_000:,.4f}"


def _shorten(text, limit: int = 80) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def serve(*, host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> int:
    create_app().run(host=host, port=port, debug=debug)
    return 0
