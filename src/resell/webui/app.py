"""Routes. Read via views, write via the gateway, render, return.

Two things here are load-bearing and easy to undo by accident:

* Nothing computes. `_money` is a Jinja filter and the templates are the only
  callers; if a route ever needs to know what a number *means* it is asking the
  wrong module.
* Mutations name the backend function they call, and there are only three:
  `Gateway.ingest_item`, `Gateway.attach_photo` and `Gateway.answer_question`.
  Approve, propose, publish and price are deliberately absent -- they carry
  authority, they are already correct in the CLI, and a button is a bad place to
  put a decision that a hash is bound to.

`Rejected` is rendered rather than swallowed. The gateway's refusals are the most
informative output the system produces, and a UI that turns them into "something
went wrong" is strictly worse than the CLI it is meant to replace.
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

from resell import db, views
from resell.config import ConfigError, load_config
from resell.domain import FeeModel
from resell.gateway import Gateway, Rejected
from resell.images import inspect

# Formats a browser will not render inline, so the cached model derivative is
# served instead. The list is short on purpose: it is what iPhones produce.
_NEEDS_DERIVATIVE = {"heic", "heif", "tiff"}


def create_app(*, config=None) -> Flask:
    """Build the app. `config` is injectable so tests need no .env and no tokens."""
    app = Flask(__name__)
    # Signs the flash cookie and nothing else. Regenerated per process: a lost
    # flash message on restart is the entire consequence, and a checked-in
    # constant would be worse for no gain.
    app.secret_key = hashlib.sha256(str(id(app)).encode()).hexdigest()
    app.config["RESELL_CONFIG"] = config
    app.jinja_env.filters["money"] = _money
    app.jinja_env.filters["shorten"] = _shorten

    @app.before_request
    def _open_database() -> None:
        g.config = app.config["RESELL_CONFIG"] or load_config(require_credentials=False)
        g.conn = db.connect(g.config.db_path)
        g.gateway = Gateway(
            g.conn,
            marketplace=g.config.marketplace_id,
            environment=g.config.env.name,
            fees=FeeModel(),
        )

    @app.teardown_appcontext
    def _close_database(_exception) -> None:
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def _environment():
        """Every page says which environment it is looking at.

        Sandbox and production are different databases with different listings,
        and a UI that does not say which one it has open is one mis-set
        environment variable away from repricing something real.
        """
        return {
            "environment": g.config.env.name,
            "marketplace": g.config.marketplace_id,
        }

    _register_routes(app)
    return app


# --- read routes -------------------------------------------------------------


def _register_routes(app: Flask) -> None:

    @app.get("/")
    def items():
        active_only = request.args.get("active") == "1"
        return render_template(
            "items.html",
            items=views.item_summaries(g.conn, active_only=active_only),
            active_only=active_only,
        )

    @app.get("/items/<sku>")
    def item(sku: str):
        return render_template("item.html", item=_detail(sku))

    @app.get("/items/<sku>/questions")
    def questions(sku: str):
        detail = _detail(sku)
        return render_template("questions.html", item=detail)

    @app.get("/questions")
    def all_questions():
        """The whole inbox, across items. The CLI's `item questions` with no SKU."""
        return render_template(
            "inbox.html", questions=views.open_questions(g.conn)
        )

    @app.get("/items/<sku>/draft")
    def draft(sku: str):
        detail = _detail(sku)
        return render_template(
            "draft.html", item=detail, draft=views.listing_draft(detail)
        )

    @app.get("/items/<sku>/aspects")
    def aspects(sku: str):
        """eBay's aspect form for the item's category. Costs a Taxonomy call.

        The only page that needs credentials, so it asks for them here rather
        than at startup: the rest of the UI works on a machine with no tokens,
        and failing the whole app for one page would be the wrong trade.
        """
        detail = _detail(sku)
        try:
            config = _credentialed_config()
        except ConfigError as exc:
            form = views.AspectForm(
                category_id="", marketplace=g.config.marketplace_id, rows=(),
                error=f"eBay credentials are not configured: {exc}",
            )
        else:
            form = views.aspect_form(config, g.conn, g.gateway, sku)
        return render_template("aspects.html", item=detail, form=form)

    @app.get("/items/<sku>/pricing")
    def pricing(sku: str):
        """The band and the three strategies. Computes; records nothing.

        Every pricing judgment is editable from the form, and every one of them
        defaults to what the record already knows. Choosing between the
        strategies stays with `resell price propose`: a proposal freezes a comp
        set and claims an objective, and neither should happen because somebody
        loaded a page.
        """
        detail = _detail(sku)
        return render_template(
            "pricing.html",
            item=detail,
            view=views.pricing_view(
                g.conn, sku, _pricing_request(sku),
                marketplace=g.config.marketplace_id,
            ),
            request_values=_pricing_request(sku),
            condition_bands=_condition_bands(),
        )

    @app.get("/items/<sku>/photo/<int:position>")
    def photo(sku: str, position: int):
        """Serve a photo, converting only when a browser cannot render it.

        Reuses `derivatives.for_model`, which is the same cache the vision stage
        fills, so opening an item in the UI does not duplicate a conversion the
        pipeline has already paid for.
        """
        from resell.derivatives import ConversionError, for_model

        match = next(
            (p for p in _detail(sku).photos if p.position == position), None
        )
        if match is None:
            abort(404)
        source = Path(match.source_path)
        if not source.exists():
            abort(404)
        # Absolute, always. `RESELL_DB` is conventionally relative, and Flask
        # resolves a relative send_file path against its own package directory --
        # which is how this route once tried to read derivatives out of webui/.
        if (match.image_format or "").lower() not in _NEEDS_DERIVATIVE:
            return send_file(source.resolve())
        try:
            derivative = for_model(
                source,
                Path(g.config.db_path).resolve().parent / "derivatives",
                digest=match.content_sha256,
            )
        except ConversionError:
            abort(415)
        return send_file(Path(derivative).resolve())

    # --- write routes --------------------------------------------------------

    @app.post("/items")
    def create_item():
        """Allocate a SKU, then attach whatever photos came with the form.

        One request, two gateway calls, and the SKU survives a photo failure --
        `ingest_item` is what allocates it and SKUs are never reused, so silently
        rolling back on a bad JPEG would burn one. The item is created, the
        failures are reported, and the operator retries the upload.
        """
        cost = _cents(request.form.get("cost_dollars"))
        try:
            accepted = g.gateway.ingest_item(
                purchase_cost_cents=cost,
                acquisition_intent=request.form.get("intent") or "unknown",
                notes=request.form.get("notes") or None,
            )
        except Rejected as exc:
            _flash_rejection(exc)
            return redirect(url_for("items"))

        sku = accepted.sku
        attached, failed = _attach_uploads(sku, request.files.getlist("photos"))
        flash(f"created {sku}" + (f", {attached} photo(s) attached" if attached else ""),
              "ok")
        for message in failed:
            flash(message, "error")
        return redirect(url_for("item", sku=sku))

    @app.post("/items/<sku>/photos")
    def add_photos(sku: str):
        # Resolve the item first. Attaching to an unknown SKU would otherwise
        # flash a rejection and then redirect to a 404, losing the message.
        _detail(sku)
        attached, failed = _attach_uploads(sku, request.files.getlist("photos"))
        if attached:
            flash(f"{attached} photo(s) attached", "ok")
        for message in failed:
            flash(message, "error")
        return redirect(url_for("item", sku=sku))

    @app.post("/questions/<int:question_id>/answer")
    def answer(question_id: int):
        """Operator answers a question. Straight through to the gateway.

        `value_not_listed` is passed through rather than inferred. The gateway
        refuses an answer outside eBay's list unless the operator says the list is
        wrong, and that override is recorded as a decision -- so the UI has to
        make it an explicit checkbox, not something a form submission implies.
        """
        sku = request.form.get("sku", "")
        try:
            accepted = g.gateway.answer_question(
                question_id,
                request.form.get("answer", ""),
                operator=True,
                value_not_listed=request.form.get("value_not_listed") == "1",
            )
        except Rejected as exc:
            _flash_rejection(exc)
            return redirect(_back_to(sku))
        message = f"question {question_id} answered"
        if accepted.from_state != accepted.to_state:
            message += f"  ({accepted.from_state} -> {accepted.to_state})"
        flash(message, "ok")
        return redirect(_back_to(sku or accepted.sku))


# --- helpers -----------------------------------------------------------------


def _detail(sku: str) -> views.ItemDetail:
    try:
        return views.item_detail(
            g.conn, g.gateway, sku,
            marketplace=g.config.marketplace_id, environment=g.config.env.name,
        )
    except Rejected:
        abort(404)


def _credentialed_config():
    """The config to reach eBay with, or a `ConfigError` naming what is missing.

    An injected config is used as given -- a test or an embedding caller that
    passed one in does not want the process environment consulted behind its
    back, which is exactly what a fallback to `load_config` would do.
    """
    injected = current_app.config["RESELL_CONFIG"]
    if injected is None:
        return load_config(require_credentials=True)
    if not injected.has_credentials:
        raise ConfigError(
            "the config this app was built with has no eBay client id, secret "
            "or RuName"
        )
    return injected


def _back_to(sku: str) -> str:
    """Return the operator to where they were, which is usually a question list."""
    if request.form.get("next") == "inbox" or not sku:
        return url_for("all_questions")
    return url_for("questions", sku=sku)


def _attach_uploads(sku: str, uploads) -> tuple[int, list[str]]:
    """Write the bytes somewhere permanent, then validate and attach each one.

    Order matters. `Gateway.attach_photo` stores a path, and publish reads that
    path much later, so a temp file would produce an item whose photos exist only
    until the process ends. The file lands next to the database first; the gateway
    is told about it second.
    """
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
                sku,
                source_path=str(destination.resolve()),
                content_sha256=digest,
                image_format=facts.image_format,
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


def _pricing_request(sku: str) -> views.PricingRequest:
    """Query string over the record's own defaults. Absent means "as recorded"."""
    default = views.default_pricing_request(
        g.conn, sku, marketplace=g.config.marketplace_id
    )
    return views.PricingRequest(
        condition_band=request.args.get("condition_band") or default.condition_band,
        identity_resolution=(
            request.args.get("identity_resolution") or default.identity_resolution
        ),
        window_days=_int(request.args.get("window_days"), default.window_days),
        category_id=request.args.get("category_id") or default.category_id,
        shipping_cost_cents=_int(
            request.args.get("shipping_cost_cents"), default.shipping_cost_cents
        ),
        minimum_net_cents=_int(
            request.args.get("minimum_net_cents"), default.minimum_net_cents
        ),
        brand_strength=request.args.get("brand_strength") or default.brand_strength,
    )


def _condition_bands() -> tuple[str, ...]:
    from resell.pricing.comps import ConditionBand

    return tuple(str(band) for band in ConditionBand)


def _flash_rejection(exc: Rejected) -> None:
    """Show the gateway's own reasons, one per line. They are the useful part."""
    flash(f"REJECTED  {exc.command}", "error")
    for reason in exc.reasons:
        flash(reason, "reason")


def _int(raw: str | None, fallback: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else fallback
    except ValueError:
        return fallback


def _cents(raw: str | None) -> int | None:
    """Dollars from a form field to integer cents. Blank means unknown, not zero."""
    if raw is None or not raw.strip():
        return None
    try:
        return round(float(raw) * 100)
    except ValueError:
        return None


def _money(cents, unknown: str = "-") -> str:
    return unknown if cents is None else f"${cents / 100:,.2f}"


def _shorten(text, limit: int = 80) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def serve(*, host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> int:
    """Run the development server. Loopback only; see the package docstring."""
    create_app().run(host=host, port=port, debug=debug)
    return 0
