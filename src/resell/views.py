"""Read models. One query layer, two front ends.

`cli_item` grew its own SQL, its own derivations and its own idea of what an item
"is" -- all of it tangled with `print`. A second front end cannot reuse any of
that, so the choice was to duplicate the queries in Flask or to separate the
answer from its rendering. This module is that separation: it computes, and
returns typed values. It never prints, never formats money, never mutates, and
never touches `argparse`.

The rule that keeps it honest: a function here may read, and may derive from what
it read, but anything that changes state belongs to the gateway or the pricing
store. If a view needs to write, the view is wrong.

Two consequences worth stating, because both were surprises:

* Views take `marketplace` and `environment` as arguments rather than loading the
  config. A read model that reads the environment cannot be asked about the other
  one, and the CLI and the UI are two processes with two `.env` loads.
* A few views do I/O -- the aspect form is eBay's, not ours, and there is no
  local copy. They are named for it (`fetch_`), and the pure part is factored
  out so it can be tested without a client.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from resell.gateway import (
    active_listing,
    current_identification,
    get_item,
    live_approval,
    validated_photos,
)

# --- shapes ------------------------------------------------------------------


@dataclass(frozen=True)
class PhotoView:
    position: int
    source_path: str
    filename: str
    image_format: str | None
    content_sha256: str
    valid: bool
    validation_errors: str | None


@dataclass(frozen=True)
class IdentificationView:
    version: int
    brand: str | None
    model: str | None
    variant: str | None
    title: str | None
    description: str | None
    category_id: str | None
    condition_id: str | None
    aspects: dict[str, list[str]]
    confidence: float | None
    reasoning: str | None

    @property
    def missing_to_price(self) -> tuple[str, ...]:
        """The three fields the pricing transition will not proceed without."""
        return tuple(
            name
            for name, value in (
                ("title", self.title),
                ("category_id", self.category_id),
                ("condition_id", self.condition_id),
            )
            if not value
        )


@dataclass(frozen=True)
class SupersededIdentification:
    version: int
    title: str | None
    category_id: str | None
    condition_id: str | None
    superseded_at: str


@dataclass(frozen=True)
class QuestionView:
    """An open question, plus the two things an answer form needs.

    `allowed_values` is eBay's list as it stood when the question was asked, and
    is what `Gateway.answer_question` validates against -- so a UI that renders it
    as a picker and a CLI that prints it are agreeing with the same authority
    rather than each guessing.

    `resolved_value` is the aspect's current value where the question named an
    aspect that a later mapping run has since settled. Asking again about
    something already decided teaches people to skip the prompt, so the answer
    that is already on the record is offered rather than hidden.
    """

    id: int
    sku: str
    item_state: str
    question: str
    why_it_matters: str | None
    blocking: bool
    asked_at: str
    aspect_name: str | None
    allowed_values: tuple[str, ...]
    resolved_value: tuple[str, ...] | None
    # eBay's values for this aspect, looked up now rather than captured when the
    # question was asked. Most aspects are FREE_TEXT with *recommended* values --
    # eBay offers 204 models and 7 types for an action camera -- and those are
    # suggestions, never a constraint: `allowed_values` remains the only thing
    # `answer_question` validates against. Without these the card was a bare text
    # box for an aspect eBay could have listed, and the operator guessed.
    suggested_values: tuple[str, ...] = ()

    @property
    def has_choices(self) -> bool:
        return bool(self.allowed_values or self.suggested_values)

    @property
    def choices(self) -> tuple[str, ...]:
        """What to put in the picker. Binding values win where there are any."""
        return self.allowed_values or self.suggested_values

    @property
    def choices_are_binding(self) -> bool:
        return bool(self.allowed_values)

    @property
    def suggested_answer(self) -> str | None:
        """What to prefill, when the record already contains the answer."""
        if not self.resolved_value:
            return None
        return " + ".join(str(v) for v in self.resolved_value)


@dataclass(frozen=True)
class ListingView:
    marketplace: str
    environment: str
    title: str | None
    description: str | None
    category_id: str | None
    condition_id: str | None
    aspects: dict[str, list[str]]
    price_cents: int | None
    shipping_terms: str | None
    seller_shipping_cost_cents: int
    buyer_shipping_charge_cents: int
    estimated_fees_cents: int | None
    fee_basis: str | None
    fee_rate_used: float | None
    offer_id: str | None
    listing_id: str | None
    published_at: str | None


@dataclass(frozen=True)
class EvidenceView:
    id: int
    kind: str
    source: str
    send_to_model: bool


@dataclass(frozen=True)
class ItemSummary:
    sku: str
    state: str
    acquisition_intent: str
    purchase_cost_cents: int | None
    identification_effort: str | None
    created_at: str
    notes: str | None
    photo_count: int
    # Positions are the order they were attached in and do not start at zero: a
    # thumbnail hard-coded to position 0 asked for a photo that does not exist on
    # most items, and every card on the list rendered a broken image.
    first_photo_position: int | None
    evidence_count: int
    open_question_count: int
    blocking_question_count: int
    listing_id: str | None
    title: str | None


@dataclass(frozen=True)
class ItemDetail:
    sku: str
    state: str
    acquisition_intent: str
    purchase_cost_cents: int | None
    identification_effort: str | None
    notes: str | None
    created_at: str
    photos: tuple[PhotoView, ...]
    photo_count_total: int
    identification: IdentificationView | None
    superseded: tuple[SupersededIdentification, ...]
    blocking_questions: tuple[QuestionView, ...]
    optional_questions: tuple[QuestionView, ...]
    listing: ListingView | None
    proposal_hash: str | None
    live_approval_hash: str | None
    voided_approvals: int
    evidence: tuple[EvidenceView, ...]

    @property
    def invalid_photo_count(self) -> int:
        return self.photo_count_total - len(self.photos)

    @property
    def approval_matches_proposal(self) -> bool:
        """Whether the live approval covers the content currently on record.

        Repricing deliberately does not void this: price authority belongs to the
        pricing approval, listing authority to this one. A mismatch here therefore
        means the *content* drifted, which is the only thing it should ever mean.
        """
        return bool(
            self.live_approval_hash
            and self.proposal_hash
            and self.live_approval_hash == self.proposal_hash
        )


# --- item list ---------------------------------------------------------------


def item_summaries(
    conn: sqlite3.Connection,
    *,
    states: tuple[str, ...] = (),
    active_only: bool = False,
) -> list[ItemSummary]:
    """Every item, in SKU order, terminal ones included unless filtered out.

    Abandoned items are not hidden by default: SKUs are never reused, so a gap in
    the sequence is a question worth being able to answer.
    """
    clauses: list[str] = []
    params: list[object] = []
    if states:
        clauses.append(f"i.state IN ({','.join('?' * len(states))})")
        params.extend(states)
    if active_only:
        clauses.append("i.state NOT IN ('abandoned', 'listed')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = conn.execute(
        f"""
        SELECT i.sku, i.state, i.acquisition_intent, i.purchase_cost_cents,
               i.identification_effort, i.created_at, i.notes,
               (SELECT COUNT(*) FROM photo p WHERE p.sku = i.sku) AS photos,
               (SELECT MIN(p.position) FROM photo p WHERE p.sku = i.sku)
                   AS first_photo,
               (SELECT COUNT(*) FROM evidence e WHERE e.sku = i.sku) AS evidence,
               (SELECT COUNT(*) FROM open_question q
                 WHERE q.sku = i.sku AND q.answered_at IS NULL) AS open_questions,
               (SELECT COUNT(*) FROM open_question q
                 WHERE q.sku = i.sku AND q.answered_at IS NULL
                   AND q.blocking = 1) AS blocking_questions,
               (SELECT l.listing_id FROM listing l
                 WHERE l.sku = i.sku AND l.active = 1) AS listing_id,
               (SELECT d.title FROM identification d
                 WHERE d.sku = i.sku AND d.superseded_at IS NULL) AS title
          FROM item i {where} ORDER BY i.seq
        """,
        params,
    ).fetchall()

    return [
        ItemSummary(
            sku=row["sku"],
            state=row["state"],
            acquisition_intent=row["acquisition_intent"],
            purchase_cost_cents=row["purchase_cost_cents"],
            identification_effort=row["identification_effort"],
            created_at=row["created_at"],
            notes=row["notes"],
            photo_count=row["photos"],
            first_photo_position=row["first_photo"],
            evidence_count=row["evidence"],
            open_question_count=row["open_questions"],
            blocking_question_count=row["blocking_questions"],
            listing_id=row["listing_id"],
            title=row["title"],
        )
        for row in rows
    ]


# --- questions ---------------------------------------------------------------


def open_questions(
    conn: sqlite3.Connection,
    *,
    sku: str | None = None,
    blocking_only: bool = False,
    config=None,
) -> list[QuestionView]:
    """The operator's queue: asked, and nobody has answered.

    Both kinds are returned. Non-blocking questions were being recorded and
    displayed nowhere, which meant the agent could ask something useful and have
    it silently disappear -- the operator-as-tool loop has to have a visible inbox
    or the tool never gets called.
    """
    clauses = ["q.answered_at IS NULL"]
    params: list[object] = []
    if sku:
        clauses.append("q.sku = ?")
        params.append(sku)
    if blocking_only:
        clauses.append("q.blocking = 1")

    rows = conn.execute(
        f"""
        SELECT q.id, q.sku, q.question, q.why_it_matters, q.blocking, q.asked_at,
               q.aspect_name, q.allowed_values_json, i.state
          FROM open_question q JOIN item i ON i.sku = q.sku
         WHERE {' AND '.join(clauses)}
         ORDER BY q.sku, q.blocking DESC, q.id
        """,
        params,
    ).fetchall()

    # One identification lookup per SKU, not per question: `resolved_value` reads
    # the same aspect dict for every question on an item.
    resolved: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        if row["sku"] not in resolved:
            resolved[row["sku"]] = _current_aspects(conn, row["sku"])


    # eBay's values for the aspects being asked about, one Taxonomy call per
    # category rather than per question. Optional: without credentials the card
    # degrades to the text box it has always been, which is the same posture the
    # condition field takes.
    suggestions = _aspect_suggestions(conn, rows, config)
    return [
        _question_view(row, resolved[row["sku"]], suggestions.get(row["sku"], {}))
        for row in rows
    ]


def _aspect_suggestions(conn, rows, config) -> dict[str, dict[str, tuple[str, ...]]]:
    """Per sku, the values eBay lists for each aspect a question is about.

    Looked up now rather than read from what was stored when the question was
    asked, for two reasons: the questions already open carry nothing, so a
    capture-at-ask-time fix would help only the next item and leave this one
    stuck; and eBay's list is a live thing, so the current answer is the useful
    one.

    Never raises. A card that cannot reach the Taxonomy API should still let
    somebody type the value they already know.
    """
    if config is None:
        return {}
    wanted = {row["sku"] for row in rows if row["aspect_name"]}
    if not wanted:
        return {}
    try:
        from resell.domain import FeeModel
        from resell.ebay.client import EbayClient
        from resell.ebay.publisher import Publisher
        from resell.gateway import Gateway
    except Exception:  # noqa: BLE001
        return {}

    by_sku: dict[str, dict[str, tuple[str, ...]]] = {}
    by_category: dict[str, dict[str, tuple[str, ...]]] = {}
    try:
        gateway = Gateway(
            conn, marketplace=config.marketplace_id,
            environment=config.env.name, fees=FeeModel(),
        )
        with EbayClient(config, conn) as client:
            publisher = Publisher(gateway, client, conn)
            for sku in sorted(wanted):
                category = _category_of(conn, sku)
                if not category:
                    continue
                if category not in by_category:
                    by_category[category] = {
                        spec.name: tuple(spec.allowed_values)
                        for spec in publisher.aspect_schema(
                            config.marketplace_id, category
                        )
                    }
                by_sku[sku] = by_category[category]
    except Exception:  # noqa: BLE001 - a picker is a convenience, not a precondition
        return by_sku
    return by_sku


def _category_of(conn, sku: str) -> str | None:
    row = conn.execute(
        "SELECT category_id FROM identification WHERE sku = ? AND superseded_at IS NULL",
        (sku,),
    ).fetchone()
    return row["category_id"] if row else None


def _question_view(
    row: sqlite3.Row, aspects: dict[str, list[str]],
    suggestions: dict[str, tuple[str, ...]] | None = None,
) -> QuestionView:
    aspect_name = row["aspect_name"]
    value = aspects.get(aspect_name) if aspect_name else None
    allowed = row["allowed_values_json"]
    return QuestionView(
        id=row["id"],
        sku=row["sku"],
        item_state=row["state"],
        question=row["question"],
        why_it_matters=row["why_it_matters"],
        blocking=bool(row["blocking"]),
        asked_at=row["asked_at"],
        aspect_name=aspect_name,
        allowed_values=tuple(json.loads(allowed)) if allowed else (),
        resolved_value=tuple(value) if value else None,
        suggested_values=(suggestions or {}).get(aspect_name, ()) if aspect_name else (),
    )


def _current_aspects(conn: sqlite3.Connection, sku: str) -> dict[str, list[str]]:
    identification = current_identification(conn, sku)
    if identification is None or not identification["aspects"]:
        return {}
    return json.loads(identification["aspects"])


# --- item detail -------------------------------------------------------------


def item_detail(
    conn: sqlite3.Connection,
    gateway,
    sku: str,
    *,
    marketplace: str,
    environment: str,
    config=None,
) -> ItemDetail:
    """Everything known about one item. Raises `Rejected` if the SKU is unknown.

    The proposal hash is computed rather than stored, and has to be retrievable at
    any time and not only from the output of `item propose` -- so it is derived
    here, from the gateway, using the same code path approval will check against.
    """
    item = get_item(conn, sku)

    photos = tuple(
        PhotoView(
            position=row["position"],
            source_path=row["source_path"],
            filename=Path(row["source_path"]).name,
            image_format=row["image_format"],
            content_sha256=row["content_sha256"],
            valid=True,
            validation_errors=None,
        )
        for row in validated_photos(conn, sku)
    )
    photo_count_total = conn.execute(
        "SELECT COUNT(*) FROM photo WHERE sku = ?", (sku,)
    ).fetchone()[0]

    identification = _identification_view(current_identification(conn, sku))

    superseded = tuple(
        SupersededIdentification(
            version=row["version"],
            title=row["title"],
            category_id=row["category_id"],
            condition_id=row["condition_id"],
            superseded_at=row["superseded_at"],
        )
        for row in conn.execute(
            "SELECT version, title, category_id, condition_id, superseded_at "
            "FROM identification WHERE sku = ? AND superseded_at IS NOT NULL "
            "ORDER BY version DESC LIMIT 5",
            (sku,),
        ).fetchall()
    )

    questions = open_questions(conn, sku=sku, config=config)
    listing_row = active_listing(conn, sku, marketplace, environment)
    listing = _listing_view(listing_row, marketplace, environment)

    proposal_hash = None
    if listing_row is not None:
        proposal_hash = gateway._proposal_from_listing(sku, listing_row).content_hash()

    approval = live_approval(conn, sku)
    voided = conn.execute(
        "SELECT COUNT(*) FROM approval WHERE sku = ? AND voided_at IS NOT NULL", (sku,)
    ).fetchone()[0]

    evidence = tuple(
        EvidenceView(
            id=row["id"],
            kind=row["kind"],
            source=row["source"],
            send_to_model=bool(row["send_to_model"]),
        )
        for row in conn.execute(
            "SELECT id, kind, source, send_to_model FROM evidence "
            "WHERE sku = ? ORDER BY id",
            (sku,),
        ).fetchall()
    )

    return ItemDetail(
        sku=item["sku"],
        state=item["state"],
        acquisition_intent=item["acquisition_intent"],
        purchase_cost_cents=item["purchase_cost_cents"],
        identification_effort=_optional_column(item, "identification_effort"),
        notes=item["notes"],
        created_at=item["created_at"],
        photos=photos,
        photo_count_total=photo_count_total,
        identification=identification,
        superseded=superseded,
        blocking_questions=tuple(q for q in questions if q.blocking),
        optional_questions=tuple(q for q in questions if not q.blocking),
        listing=listing,
        proposal_hash=proposal_hash,
        live_approval_hash=approval["proposal_hash"] if approval else None,
        voided_approvals=voided,
        evidence=evidence,
    )


def _identification_view(row: sqlite3.Row | None) -> IdentificationView | None:
    if row is None:
        return None
    return IdentificationView(
        version=row["version"],
        brand=row["brand"],
        model=row["model"],
        variant=row["variant"],
        title=row["title"],
        description=row["description"],
        category_id=row["category_id"],
        condition_id=row["condition_id"],
        aspects=json.loads(row["aspects"]) if row["aspects"] else {},
        confidence=row["confidence"],
        reasoning=row["reasoning"],
    )


def _listing_view(
    row: sqlite3.Row | None, marketplace: str, environment: str
) -> ListingView | None:
    if row is None:
        return None
    return ListingView(
        marketplace=row["marketplace"],
        environment=row["environment"],
        title=row["title"],
        description=row["description"],
        category_id=row["category_id"],
        condition_id=row["condition_id"],
        aspects=json.loads(row["aspects"]) if row["aspects"] else {},
        price_cents=row["price_cents"],
        shipping_terms=_optional_column(row, "shipping_terms"),
        seller_shipping_cost_cents=_optional_column(
            row, "seller_shipping_cost_cents"
        ) or 0,
        buyer_shipping_charge_cents=_optional_column(
            row, "buyer_shipping_charge_cents"
        ) or 0,
        estimated_fees_cents=row["estimated_fees_cents"],
        fee_basis=_optional_column(row, "fee_basis"),
        fee_rate_used=_optional_column(row, "fee_rate_used"),
        offer_id=row["offer_id"],
        listing_id=row["listing_id"],
        published_at=row["published_at"],
    )


def _optional_column(row: sqlite3.Row, name: str):
    """Read a column that a migration added, without assuming it is there.

    Views run against whatever schema the caller opened. Failing on a missing
    column would make a read model the thing that breaks on an un-migrated
    database, which is exactly backwards.
    """
    return row[name] if name in row.keys() else None


# --- listing draft -----------------------------------------------------------


@dataclass(frozen=True)
class ListingDraftView:
    """The listing copy, and which record it came from.

    Two sources can hold copy at once and they mean different things: the
    identification is the current belief, the listing row is what was proposed and
    is what an approval binds. Showing them as one field hid the case where a new
    draft was stored and never re-proposed, so both are returned and the drift is
    named rather than resolved.
    """

    sku: str
    state: str
    identification_title: str | None
    identification_description: str | None
    proposed_title: str | None
    proposed_description: str | None
    aspects: dict[str, list[str]]
    proposal_hash: str | None
    live_approval_hash: str | None
    blocking_questions: tuple[QuestionView, ...]

    @property
    def content_differs_from_proposal(self) -> bool:
        if self.proposed_title is None and self.proposed_description is None:
            return False
        return (
            self.identification_title != self.proposed_title
            or self.identification_description != self.proposed_description
        )

    @property
    def title_length(self) -> int:
        return len(self.identification_title or "")


def listing_draft(detail: ItemDetail) -> ListingDraftView:
    """Derived from `item_detail`, so the two can never disagree."""
    identification = detail.identification
    listing = detail.listing
    return ListingDraftView(
        sku=detail.sku,
        state=detail.state,
        identification_title=identification.title if identification else None,
        identification_description=(
            identification.description if identification else None
        ),
        proposed_title=listing.title if listing else None,
        proposed_description=listing.description if listing else None,
        aspects=identification.aspects if identification else {},
        proposal_hash=detail.proposal_hash,
        live_approval_hash=detail.live_approval_hash,
        blocking_questions=detail.blocking_questions,
    )


# --- aspect form -------------------------------------------------------------


@dataclass(frozen=True)
class AspectRow:
    name: str
    required: bool
    mode: str
    cardinality: str
    data_type: str
    max_length: int | None
    allowed_values: tuple[str, ...]
    selection_only: bool
    current: tuple[str, ...]
    unknown_current: tuple[str, ...]

    @property
    def status(self) -> str:
        if self.current:
            return "set"
        return "missing" if self.required else "unset"

    @property
    def free_text(self) -> bool:
        return not self.allowed_values and not self.selection_only


@dataclass(frozen=True)
class AspectForm:
    category_id: str
    marketplace: str
    rows: tuple[AspectRow, ...]
    error: str | None = None

    @property
    def required_count(self) -> int:
        return sum(1 for row in self.rows if row.required)

    @property
    def outstanding(self) -> tuple[str, ...]:
        return tuple(row.name for row in self.rows if row.required and not row.current)


def aspect_rows(specs, current: dict[str, list[str]]) -> tuple[AspectRow, ...]:
    """Join eBay's form to what we have filled in. Pure, so it is testable."""
    rows = []
    for spec in specs:
        have = [str(v) for v in (current.get(spec.name) or [])]
        rows.append(
            AspectRow(
                name=spec.name,
                required=spec.required,
                mode=spec.mode,
                cardinality=spec.cardinality,
                data_type=spec.data_type,
                max_length=spec.max_length,
                allowed_values=tuple(spec.allowed_values),
                selection_only=spec.selection_only,
                current=tuple(have),
                unknown_current=tuple(spec.unknown_values(have)) if have else (),
            )
        )
    return tuple(rows)


def fetch_aspect_schema(config, conn: sqlite3.Connection, gateway, category_id: str):
    """eBay's aspect form for a category. Costs a Taxonomy call.

    Kept here rather than in either front end because both need it and neither
    should own the client lifetime. The `EbayApiError` is deliberately not caught:
    a category with no form and a category we failed to ask about are different
    facts, and swallowing the difference here would let both render as "no
    required aspects".
    """
    from resell.ebay.client import EbayClient
    from resell.ebay.publisher import Publisher

    with EbayClient(config, conn) as client:
        return Publisher(gateway, client, conn).aspect_schema(
            config.marketplace_id, category_id
        )


def aspect_form(
    config, conn: sqlite3.Connection, gateway, sku: str, category_id: str | None = None
) -> AspectForm:
    """The form for an item's category, with its current values filled in.

    Returns an `AspectForm` carrying `error` rather than raising, because an
    unreachable Taxonomy API should not blank a page that has plenty else to
    show. Callers that need to distinguish the failure read `.error`.
    """
    from resell.ebay.client import EbayApiError

    identification = current_identification(conn, sku)
    category = category_id or (
        identification["category_id"] if identification else None
    )
    if not category:
        return AspectForm(
            category_id="",
            marketplace=config.marketplace_id,
            rows=(),
            error="no category on the identification yet",
        )

    current = _current_aspects(conn, sku)
    try:
        specs = fetch_aspect_schema(config, conn, gateway, category)
    except EbayApiError as exc:
        return AspectForm(
            category_id=category,
            marketplace=config.marketplace_id,
            rows=(),
            error=f"aspect lookup failed for category {category}: HTTP {exc.status_code}",
        )
    return AspectForm(
        category_id=category,
        marketplace=config.marketplace_id,
        rows=aspect_rows(specs, current),
        error=None,
    )


# --- pricing -----------------------------------------------------------------


@dataclass(frozen=True)
class PricingRequest:
    """The judgments pricing needs that are not derivable from the item.

    Every one of these is an operator claim in the CLI, and none of them acquires
    authority by being defaulted here. This view computes; `price propose` is
    still what records a number, and the defaults exist so a read-only page can
    be rendered without first making four decisions.
    """

    condition_band: str = "unknown"
    identity_resolution: str = "unresolved"
    window_days: int = 90
    retail_cents: tuple[int, ...] = ()
    retail_kind: str | None = None
    category_id: str | None = None
    # eBay's own path to the category. Read only to choose a retention rate for
    # the retail-derived anchor -- see `pricing/retention.py`.
    category_path: str | None = None
    shipping_cost_cents: int = 0
    minimum_net_cents: int = 500
    brand_strength: str | None = None
    brand_citations: tuple[str, ...] = ()
    brand_rationale: str = ""


def pricing_input(conn: sqlite3.Connection, sku: str, request: "PricingRequest"):
    """The one place a `PricingInput` is assembled from the record.

    There were three, and they disagreed. The web page passed the retail
    references and the category path; `approve_price` passed neither, and the
    CLI passed only what the operator typed. So a seller could be shown three
    retail-anchored prices, tap one, and have the approval recompute without the
    retail that produced them -- "the evidence supports no strategy" on a screen
    that had just displayed the evidence. Anything that recomputes a price has to
    recompute *the same* price, so the assembly lives here and is called.
    """
    from resell import store_pricing as sp
    from resell.pricing.comps import ConditionBand, RetailKind
    from resell.pricing.estimate import PricingInput, RetailReference

    scored = sp.load_scored_comps(conn, sku)
    # Two sources, kept apart. What the operator typed is theirs and is taken at
    # its stated kind, about this item. What pricing research retrieved is only a
    # *current* shop price where the observation itself says so -- `retail_kind`
    # is set by the extractor against the page it read -- and it carries the
    # judge's grade for that page, because a shop price on a page the judge
    # excluded is a price tag for something else.
    retail = tuple(
        RetailReference(
            price_cents=cents,
            kind=RetailKind(request.retail_kind or "retail_original"),
        )
        for cents in request.retail_cents
    ) + tuple(
        RetailReference(
            price_cents=c.observation.price_cents,
            kind=RetailKind.CURRENT,
            source=c.observation.marketplace,
            match=c.claim.comparability,
        )
        for c in scored
        if c.observation.retail_kind is RetailKind.CURRENT
        and c.observation.price_cents
    )
    return PricingInput(
        sku=sku,
        item_condition_band=ConditionBand(request.condition_band),
        identity_resolution=request.identity_resolution,
        category_path=request.category_path,
        comps=tuple(scored),
        retail=retail,
        window_days=request.window_days,
    ), scored


@dataclass(frozen=True)
class StrategyView:
    objective: str
    price_cents: int
    net_proceeds_cents: int
    anchor: str
    floor_bound: bool
    tradeoff: str
    is_default: bool


@dataclass(frozen=True)
class PricingView:
    sku: str
    unpriceable: bool
    reason: str
    summary: str
    price_kind: str | None
    band_low_cents: int | None
    band_central_cents: int | None
    band_high_cents: int | None
    band_relation: str
    qualifiers: tuple[str, ...]
    sample_exclusions: tuple[str, ...]
    comparability_profile: dict[str, int]
    retail_context: tuple[tuple[int, str], ...]
    distributions: tuple[tuple[str, int, int | None, int | None, int | None, int | None], ...]
    diagnostic_confidence: float
    comp_count: int
    strategies: tuple[StrategyView, ...]
    fee_schedule_version: str | None
    fee_basis: str | None
    sold_evidence_note: str
    uncertainty_note: str
    notes: tuple[str, ...]
    # Price authority lives with the pricing approval, not the listing approval,
    # so the two are reported side by side and neither is derived from the other.
    price_state: str | None = None
    live_price_cents: int | None = None
    latest_proposal_id: str | None = None
    latest_proposal_price_cents: int | None = None
    latest_proposal_reason: str | None = None
    price_approval_live: bool = False
    history: tuple[dict, ...] = field(default_factory=tuple)


def default_pricing_request(
    conn: sqlite3.Connection, sku: str, *, marketplace: str
) -> PricingRequest:
    """Fill in what the record already knows, and nothing more.

    The condition band comes from the identification's condition enum by way of
    eBay's own two tables -- enum to numeric ID, numeric ID to band -- rather than
    a third mapping invented here, so a category that surprises one of them
    surprises both consistently.
    """
    identification = current_identification(conn, sku)
    if identification is None:
        return PricingRequest()
    return PricingRequest(
        condition_band=str(_band_for_condition_enum(identification["condition_id"])),
        category_id=identification["category_id"],
        category_path=identification["category_path"],
    )


def _band_for_condition_enum(condition_enum: str | None):
    from resell.ebay.publisher import CONDITION_ID_TO_ENUM
    from resell.pricing.comps import ConditionBand, band_for_condition_id

    if not condition_enum:
        return ConditionBand.UNKNOWN
    for condition_id, enum_value in CONDITION_ID_TO_ENUM.items():
        if enum_value == condition_enum:
            return band_for_condition_id(int(condition_id))
    return ConditionBand.UNKNOWN


def pricing_view(
    conn: sqlite3.Connection,
    sku: str,
    request: PricingRequest,
    *,
    marketplace: str,
) -> PricingView:
    """The band, the three strategies, and where the price currently stands.

    Calls `recommend` and `build_strategies` -- the same functions `price
    recommend` and `price propose` call -- so a strategy shown here and a strategy
    proposed from the CLI are the same computation on the same comp set. Nothing
    is frozen and nothing is written: freezing a comp set is what `propose` does,
    and doing it on a page view would mint proposals nobody asked for.
    """
    from resell import store_pricing as sp
    from resell.pricing.comps import ConditionBand, RetailKind
    from resell.pricing.estimate import PricingInput, RetailReference, recommend
    from resell.pricing.proceeds import PROVISIONAL_DEFAULT, CostLines
    from resell.pricing.strategy import (
        BrandSignal,
        BrandStrength,
        SellerObjective,
        build_strategies,
    )

    built, scored = pricing_input(conn, sku, request)
    retail = built.retail
    rec = recommend(built)

    schedule = sp.active_fee_schedule(
        conn, marketplace=marketplace, category_id=request.category_id
    ) or PROVISIONAL_DEFAULT
    costs = CostLines(seller_paid_shipping_cents=request.shipping_cost_cents)
    strategy_set = build_strategies(
        rec,
        brand=BrandSignal(
            strength=BrandStrength(request.brand_strength or "unknown"),
            citations=request.brand_citations,
            rationale=request.brand_rationale,
        ),
        schedule=schedule,
        costs=costs,
        minimum_net_proceeds_cents=request.minimum_net_cents,
    )

    strategies: tuple[StrategyView, ...] = ()
    if strategy_set is not None:
        strategies = tuple(
            StrategyView(
                objective=str(objective),
                price_cents=strategy.price_cents,
                net_proceeds_cents=strategy.net_proceeds_cents,
                anchor=strategy.anchor.describe(),
                floor_bound=strategy.floor_bound,
                tradeoff=strategy.tradeoff,
                is_default=objective is strategy_set.default_objective,
            )
            for objective, strategy in (
                (o, strategy_set.get(o)) for o in SellerObjective
            )
        )

    price_state = sp.current_price_state(conn, sku)
    latest = sp.latest_proposal(conn, sku)
    price_approval = sp.live_approval(conn, latest.proposal_id) if latest else None

    return PricingView(
        sku=sku,
        unpriceable=rec.unpriceable,
        reason=rec.reason,
        summary=rec.describe(),
        price_kind=str(rec.price_kind) if rec.price_kind else None,
        band_low_cents=rec.band_low_cents,
        band_central_cents=rec.band_central_cents,
        band_high_cents=rec.band_high_cents,
        band_relation=rec.band_relation,
        qualifiers=tuple(str(q) for q in rec.qualifiers),
        sample_exclusions=tuple(rec.sample_exclusions),
        comparability_profile=dict(rec.comparability_profile),
        retail_context=tuple((r.price_cents, str(r.kind)) for r in rec.retail_context),
        distributions=tuple(
            (label, d.n, d.min_cents, d.max_cents, d.median_cents, d.iqr_cents)
            for label, d in (
                ("sold, condition matched", rec.realized_comparable),
                ("sold, other condition", rec.realized_off_band),
                ("asks, condition matched", rec.asking_comparable),
                ("asks, other condition", rec.asking_off_band),
            )
            if d is not None
        ),
        diagnostic_confidence=rec.diagnostic_confidence,
        comp_count=len(scored),
        strategies=strategies,
        fee_schedule_version=schedule.version,
        fee_basis=str(schedule.basis),
        sold_evidence_note=strategy_set.sold_evidence_note if strategy_set else "",
        uncertainty_note=strategy_set.uncertainty_note if strategy_set else "",
        notes=tuple(strategy_set.notes) if strategy_set else (),
        price_state=price_state["state"],
        live_price_cents=price_state["live_price_cents"],
        latest_proposal_id=latest.proposal_id if latest else None,
        latest_proposal_price_cents=latest.price_cents if latest else None,
        latest_proposal_reason=str(latest.reason) if latest else None,
        price_approval_live=bool(price_approval and price_approval.covers(latest)),
        history=tuple(sp.price_history(conn, sku)),
    )


# --- the workflow, as one screen sees it -------------------------------------


@dataclass(frozen=True)
class CompCandidateView:
    candidate_id: str
    comp_id: str
    title: str
    price_cents: int
    shipping_cents: int | None
    price_kind: str
    condition: str
    condition_band: str
    marketplace: str
    url: str | None
    proposed_comparability: str
    rationale: str

    @property
    def total_cents(self) -> int:
        return self.price_cents + (self.shipping_cents or 0)

    @property
    def shipping_known(self) -> bool:
        return self.shipping_cents is not None


@dataclass(frozen=True)
class PriceOption:
    objective: str
    price_cents: int
    net_proceeds_cents: int
    tradeoff: str
    is_default: bool


@dataclass(frozen=True)
class WorkflowView:
    """One item, as the main screen needs it. No CLI vocabulary anywhere.

    Deliberately flat. The screen shows a photo, a status line, whatever decision
    is outstanding, and a price -- so this carries exactly that and resolves the
    lifecycle, the question queue, the candidate queue and the pricing band into
    plain fields rather than making a template walk four object graphs.
    """

    sku: str
    state: str
    title: str
    photo_positions: tuple[int, ...]
    # what happens next
    step: str
    actor: str
    summary: str
    detail: str
    waiting_on_operator: bool
    # the outstanding decisions, whichever kind
    questions: tuple[QuestionView, ...] = ()
    candidates: tuple[CompCandidateView, ...] = ()
    # the price, when there is enough to say one
    price_summary: str = ""
    price_options: tuple[PriceOption, ...] = ()
    price_low_cents: int | None = None
    price_high_cents: int | None = None
    price_confidence_note: str = ""
    # The estimator's own qualifiers, carried across unchanged. The consumer UI
    # translates them into a sentence a seller can act on; parsing the operator's
    # `price_confidence_note` prose to recover the same facts would be a second,
    # lossier copy of a decision the estimator already made explicitly.
    price_qualifiers: tuple[str, ...] = ()
    approved_price_cents: int | None = None
    listing_description: str = ""
    purchase_cost_cents: int | None = None
    # What the agent has spent processing this item, in millionths of a currency
    # unit. One number on the card; the breakdown lives in `resell item cost`.
    ai_cost_micros: int = 0
    # Whether the agent can find listings itself, or the operator has to. A field
    # rather than a template checking an environment variable: the screen should
    # not be reading configuration.
    can_search: bool = False
    # What one press of "let it look again" buys, so the card can say it rather
    # than the template knowing the number.
    grant_calls: int = 0
    grant_lookups: int = 0
    # For the manual-price box: what the item cost, and the least it is worth
    # listing at once fees are taken. `None` cost means nobody recorded one, which
    # is not the same as free.
    purchase_cost_cents: int | None = None
    floor_cents: int = 0
    # A run in flight on this item. While one is going the agent owns the moment,
    # and the screen must not offer to advance the same item in parallel: two
    # advances race through one state machine and spend two budgets.
    active_run: str | None = None
    # Empty until the item is published. The done screen turns it into a link so
    # the seller can go and look at what they just put up for sale.
    listing_url: str = ""
    # eBay's own wording for the condition, for the final review. The id is on
    # the identification; the label is the one eBay shows a buyer.
    condition_id: str = ""
    condition_label: str = ""
    # The last run stopped without finishing. `stopped_reason` is the technical
    # one, for /ops; the consumer projection turns the step into a sentence.
    stopped_step: str = ""
    stopped_reason: str = ""
    stopped_cleanly: bool = False

    @property
    def is_done(self) -> bool:
        return self.step == "done"

    @property
    def agent_is_working(self) -> bool:
        """Whether to show progress instead of actions.

        Ownership, not merely business: an operator step with a run still
        finishing is still the operator's, and hiding their decision behind a
        spinner would strand them. Only an agent-owned step in flight takes the
        screen over.
        """
        return bool(self.active_run) and not self.waiting_on_operator

    @property
    def has_price(self) -> bool:
        return bool(self.price_options)


def condition_label(condition_id) -> str:
    """eBay's own wording for a condition id, or "" if there is not one.

    The catalogue already exists and is already what publishing validates
    against, so this reads it rather than keeping a second list of words for the
    same fourteen values.
    """
    from resell.pricing.condition import EBAY_CONDITIONS

    if condition_id in (None, ""):
        return ""
    try:
        wanted = int(condition_id)
    except (TypeError, ValueError):
        return ""
    return next((c.label for c in EBAY_CONDITIONS if c.condition_id == wanted), "")


def listing_url(listing_id: str | None, *, environment: str) -> str:
    """Where a published listing can be looked at, or "" if it is not published.

    One definition, because there are two of everything else here already: the
    CLI printed this pair inline after publishing, and a second copy in the web
    UI would eventually disagree about which host the sandbox is on.
    """
    if not listing_id:
        return ""
    host = "www.sandbox.ebay.com" if environment == "sandbox" else "www.ebay.com"
    return f"https://{host}/itm/{listing_id}"


def workflow_view(
    conn: sqlite3.Connection, gateway, sku: str, *, marketplace: str, environment: str,
    config=None,
) -> WorkflowView:
    """Everything the main screen shows, in one call."""
    from resell import store_pricing as sp
    from resell.orchestrator import GRANT_CALLS, GRANT_LOOKUPS, next_step
    from resell.reasoning.ledger import total_cost_micros

    detail = item_detail(
        conn, gateway, sku, marketplace=marketplace, environment=environment,
        config=config,
    )
    step = next_step(conn, sku, marketplace=marketplace, environment=environment)

    candidates = tuple(
        CompCandidateView(
            candidate_id=row["candidate_id"],
            comp_id=row["comp_id"],
            title=row["title"] or "(untitled listing)",
            price_cents=row["price_cents"],
            shipping_cents=row["shipping_cents"],
            price_kind=row["price_kind"],
            condition=row["condition_declared_raw"] or "not stated",
            condition_band=row["condition_band"],
            marketplace=row["marketplace"],
            url=row["url"],
            proposed_comparability=row["proposed_comparability"],
            rationale=row["rationale"],
        )
        for row in sp.pending_comp_candidates(conn, sku)
    )

    from resell.orchestrator import Actor as _Actor

    stopped = _stopped_run(conn, sku) if step.actor is _Actor.AGENT else None

    price_summary, options, low, high, note = "", (), None, None, ""
    qualifiers: tuple[str, ...] = ()
    # Retail-only pricing needs `pricing_view` to run with no usable comps. It
    # already does: a harvested shop price rides in on a comp observation, so
    # `load_scored_comps` is non-empty whenever there is one. The `or` covers
    # operator-supplied retail, which has no comp row behind it -- unreachable
    # from this path today, and a silent no-price if it ever becomes reachable.
    pricing_request = default_pricing_request(conn, sku, marketplace=marketplace)
    if sp.load_scored_comps(conn, sku) or pricing_request.retail_cents:
        pricing = pricing_view(
            conn, sku, pricing_request, marketplace=marketplace,
        )
        price_summary = pricing.summary
        low, high = pricing.band_low_cents, pricing.band_high_cents
        note = pricing.uncertainty_note
        qualifiers = pricing.qualifiers
        options = tuple(
            PriceOption(
                objective=s.objective, price_cents=s.price_cents,
                net_proceeds_cents=s.net_proceeds_cents, tradeoff=s.tradeoff,
                is_default=s.is_default,
            )
            for s in pricing.strategies
        )

    identification = detail.identification
    return WorkflowView(
        sku=sku,
        can_search=search_is_available(),
        active_run=_active_run(conn, sku),
        listing_url=listing_url(
            detail.listing.listing_id if detail.listing else None,
            environment=environment,
        ),
        stopped_step=(str(step.step) if stopped else ""),
        stopped_reason=(stopped.detail if stopped else ""),
        stopped_cleanly=bool(stopped and stopped.blocked),
        condition_id=(identification.condition_id if identification else "") or "",
        condition_label=condition_label(
            identification.condition_id if identification else None
        ),
        grant_calls=GRANT_CALLS,
        grant_lookups=GRANT_LOOKUPS,
        floor_cents=_floor_for(conn, sku, marketplace=marketplace),
        state=detail.state,
        title=(identification.title if identification else "") or "(not yet identified)",
        photo_positions=tuple(p.position for p in detail.photos),
        step=str(step.step),
        actor=str(step.actor),
        summary=step.summary,
        detail=step.detail,
        waiting_on_operator=step.waiting_on_operator,
        questions=detail.blocking_questions,
        candidates=candidates,
        price_summary=price_summary,
        price_options=options,
        price_low_cents=low,
        price_high_cents=high,
        price_confidence_note=note,
        price_qualifiers=qualifiers,
        approved_price_cents=sp.approved_price_cents(conn, sku),
        listing_description=(identification.description if identification else "") or "",
        purchase_cost_cents=detail.purchase_cost_cents,
        ai_cost_micros=total_cost_micros(conn, sku),
    )


@dataclass(frozen=True)
class InventoryRow:
    """One line of the inventory table. Everything an operator sorts or scans by."""

    sku: str
    state: str
    title: str
    step: str
    actor: str
    summary: str
    photo_count: int
    first_photo_position: int | None
    open_questions: int
    pending_candidates: int
    comps: int
    purchase_cost_cents: int | None
    approved_price_cents: int | None
    listing_price_cents: int | None
    listing_id: str | None
    created_at: str
    ai_cost_micros: int = 0
    # So the queue can show "working" instead of a Run button on an item the
    # agent already has in hand.
    active_run: str | None = None

    @property
    def margin_cents(self) -> int | None:
        """Price less what the item cost to buy. Processing cost is shown apart.

        Kept separate rather than folded in, because they answer different
        questions: margin is about the trade, and the AI figure is about what this
        way of working costs to run. Netting them would hide both.
        """
        price = self.approved_price_cents or self.listing_price_cents
        if price is None or self.purchase_cost_cents is None:
            return None
        return price - self.purchase_cost_cents


def inventory(
    conn: sqlite3.Connection, *, marketplace: str, environment: str,
    include_abandoned: bool = False,
) -> list[InventoryRow]:
    """The whole table in one pass.

    `next_step` per item is a handful of indexed reads, which is affordable at the
    scale this tool operates at and keeps one definition of what an item needs. A
    second, faster, approximate answer here would eventually disagree with the
    main screen, and disagreeing with itself is the one thing an inventory table
    must not do.
    """
    from resell import store_pricing as sp
    from resell.domain import ItemState
    from resell.orchestrator import next_step
    from resell.reasoning.ledger import total_cost_by_sku

    # One query for every item's total, rather than one per row.
    costs = total_cost_by_sku(conn)
    rows = []
    for summary in item_summaries(conn):
        # Hidden rather than deleted, and hidden by default: an abandoned item is
        # kept whole -- photos, evidence, research, costs -- and the only thing
        # wrong with it is that it clutters a list of work.
        if not include_abandoned and summary.state == str(ItemState.ABANDONED):
            continue
        step = next_step(
            conn, summary.sku, marketplace=marketplace, environment=environment
        )
        listing = conn.execute(
            "SELECT price_cents FROM listing WHERE sku = ? AND active = 1",
            (summary.sku,),
        ).fetchone()
        rows.append(InventoryRow(
            active_run=_active_run(conn, summary.sku),
            sku=summary.sku,
            state=summary.state,
            title=summary.title or (summary.notes or ""),
            step=str(step.step),
            actor=str(step.actor),
            summary=step.summary,
            photo_count=summary.photo_count,
            first_photo_position=summary.first_photo_position,
            open_questions=summary.open_question_count,
            pending_candidates=len(sp.pending_comp_candidates(conn, summary.sku)),
            comps=len(sp.load_scored_comps(conn, summary.sku)),
            purchase_cost_cents=summary.purchase_cost_cents,
            approved_price_cents=sp.approved_price_cents(conn, summary.sku),
            listing_price_cents=listing["price_cents"] if listing else None,
            listing_id=summary.listing_id,
            created_at=summary.created_at,
            ai_cost_micros=costs.get(summary.sku, 0),
        ))
    return rows


# --- correcting what the record got wrong ------------------------------------


@dataclass(frozen=True)
class Field:
    """One editable field, with whatever the marketplace will accept in it."""

    name: str
    label: str
    value: str
    allowed: tuple[tuple[str, str], ...] = ()   # (value, label)
    help_text: str = ""
    problem: str = ""

    @property
    def is_choice(self) -> bool:
        return bool(self.allowed)

    @property
    def value_is_allowed(self) -> bool:
        return not self.allowed or self.value in {v for v, _ in self.allowed}


@dataclass(frozen=True)
class CorrectionForm:
    """The fields an operator may fix, and what is wrong with them right now.

    Exists because the gateway's refusals are precise and the interface was
    throwing them away: `condition 'USED' is not accepted by category 111694`
    names the field, the bad value and the allowed set, and the only thing an
    operator could do with it was go and read the CLI help.

    Price is deliberately absent. It belongs to the pricing approval, and an
    editable box here would be a second authority over the same number.
    """

    sku: str
    fields: tuple[Field, ...]
    error: str = ""

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(f.problem for f in self.fields if f.problem)

    @property
    def has_problem(self) -> bool:
        return bool(self.problems)


# Free text, but eBay rejects a title over this and the item cannot be published.
EBAY_TITLE_MAX = 80


def _refused_draft(conn, sku: str) -> tuple[str, str, str]:
    """The last draft the reviewer refused: title, description, and why.

    Offered when the identification has no copy of its own. The agent got as far
    as writing a listing and was stopped on a phrase; handing back the phrase and
    the complaint is the difference between correcting one line and composing a
    listing from nothing.
    """
    import json as _json

    row = conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND kind = 'draft_refused' "
        "ORDER BY id DESC LIMIT 1", (sku,),
    ).fetchone()
    if row is None:
        return "", "", ""
    try:
        payload = _json.loads(row["payload"])
    except (TypeError, ValueError):
        return "", "", ""
    problems = "; ".join(payload.get("problems") or [])
    return (
        str(payload.get("title") or ""),
        str(payload.get("description") or ""),
        problems,
    )


def correction_form(
    conn: sqlite3.Connection, sku: str, *, config=None, gateway=None
) -> CorrectionForm:
    """The editable view of an item's identification.

    Fetches eBay's condition list for the category when credentials allow, so the
    field is a picker rather than a box the operator has to guess into. Without
    credentials it degrades to free text and says so -- a UI that cannot reach the
    Taxonomy API should still let someone type the value they already know.
    """
    identification = current_identification(conn, sku)
    if identification is None:
        return CorrectionForm(sku=sku, fields=(), error="nothing identified yet")

    title = identification["title"] or ""
    # The agent got as far as writing a listing and was stopped on a phrase.
    # Offering the copy back turns "write a listing" into "fix this line".
    refused_title, refused_description, refused_why = _refused_draft(conn, sku)
    refused_help = (
        f"the agent wrote this and the reviewer stopped it: {refused_why}. "
        f"Edit the phrase it names and it is yours."
        if refused_why else ""
    )
    category_id = identification["category_id"] or ""
    condition_id = identification["condition_id"] or ""

    allowed, note = _condition_choices(conn, category_id, config=config, gateway=gateway)
    condition_problem = ""
    if allowed and condition_id not in {v for v, _ in allowed}:
        condition_problem = (
            f"{condition_id or 'nothing'} is not one of the conditions category "
            f"{category_id} accepts, so publishing will be refused"
        )

    # An empty field is not a fault, and conflating the two is what put "something
    # needs fixing" on the screen of someone watching the agent write their
    # listing. Nothing had gone wrong: the title was empty because drafting had
    # not run yet. A `problem` means the value that exists cannot be published --
    # a title over eBay's limit, a condition the category rejects. "Not written
    # yet" is a state, and it belongs in the help text.
    title_problem, title_help = "", ""
    if not title.strip():
        title_help = refused_help or "the agent has not written one yet"
    elif len(title) > EBAY_TITLE_MAX:
        title_problem = f"{len(title)} characters, over eBay's {EBAY_TITLE_MAX}"

    return CorrectionForm(
        sku=sku,
        fields=(
            Field(
                name="title", label="title", value=title or refused_title,
                help_text=(
                    f"{len(title)}/{EBAY_TITLE_MAX} characters" if title
                    else title_help
                ),
                problem=title_problem,
            ),
            Field(
                name="condition_id", label="condition", value=condition_id,
                allowed=allowed, help_text=note, problem=condition_problem,
            ),
            Field(
                name="category_id", label="eBay category", value=category_id,
                help_text="changing this changes which aspects and conditions apply",
            ),
            Field(
                name="description", label="description",
                value=identification["description"] or refused_description,
                help_text=refused_help if not identification["description"] else "",
            ),
        ),
    )


def _condition_choices(
    conn: sqlite3.Connection, category_id: str, *, config=None, gateway=None
) -> tuple[tuple[tuple[str, str], ...], str]:
    """eBay's accepted conditions for a category, as (enum, label) pairs.

    The label is eBay's own wording for that category, which differs between them
    -- 1000 is "New with tags" in clothing and "Brand New" elsewhere. Showing our
    own words instead would be inventing a vocabulary the marketplace does not use.
    """
    if not category_id or config is None or gateway is None:
        return (), "no category yet, so the accepted conditions are unknown"
    if not getattr(config, "has_credentials", False):
        return (), "eBay credentials are not configured, so this is free text"

    from resell.ebay.client import EbayApiError, EbayClient
    from resell.ebay.oauth import OAuthError
    from resell.ebay.publisher import Publisher

    try:
        with EbayClient(config, conn) as client:
            policy = Publisher(gateway, client, conn).condition_policy(
                config.marketplace_id, category_id
            )
    except EbayApiError as exc:
        return (), f"could not read the condition list (HTTP {exc.status_code})"
    except OAuthError as exc:
        # Configured credentials are not the same as a stored token. This field is
        # a convenience, so it degrades to free text rather than failing the card.
        return (), f"not signed in to eBay, so this is free text ({exc})"

    choices = tuple(
        (option.enum_value, f"{option.description} ({option.enum_value})")
        for option in policy.options if option.enum_value
    )
    if not choices:
        return (), "eBay returned no condition policy for this category"
    return choices, "eBay's own wording for this category"


# --- what to match a search result against ----------------------------------------


# Aspects that say *which product this is*. Ranked, because they are not equally
# useful: a brand and a model number distinguish a listing, while a Type of
# "Adjustable" or "Handheld" or "Paperback" describes a whole shelf.
#
# MP-000022 had exactly two terms, "Bowflex" and "Adjustable", and the gate
# demanded both. Every eBay title reading "Bowflex SelectTech 552 Dumbbells" was
# thrown away for lacking the word "Adjustable" -- 22 of 31 priced listings on one
# search, 27 of 30 on the next.
DISTINCTIVE_ASPECTS = (
    "Brand", "Model", "MPN", "Product Line", "Series",
    # Categories that identify by something other than a brand. A book has no
    # brand and its title is the identifier; without these an item like a
    # paperback had no terms at all, and no gate.
    "Book Title", "Title", "Author", "Model Number", "Style Code",
    "UPC", "EAN", "ISBN",
)
GENERIC_ASPECTS = ("Type",)
IDENTITY_ASPECTS = DISTINCTIVE_ASPECTS + GENERIC_ASPECTS


def _stopped_run(conn, sku: str):
    """The last run on this item, if it stopped without finishing the work.

    A blocked run and a run that never started look identical from the item's
    state -- the step is owed either way -- so the difference has to come from
    the run record. Without it the screen offers "Carry on" on an item that has
    already been tried and could not get through, which reads as if nothing has
    happened yet.
    """
    from resell.runs import latest_run_for

    try:
        run = latest_run_for(conn, sku)
    except Exception:  # noqa: BLE001 - a card without this beats no card
        return None
    if run is None or run.running or run.status == "done":
        return None
    return run


def _active_run(conn, sku: str) -> str | None:
    from resell.runs import active_run_for

    try:
        return active_run_for(conn, sku)
    except Exception:  # noqa: BLE001 - a card without this beats no card
        return None


def search_is_available() -> bool:
    """Whether autonomous discovery is configured.

    Asked here rather than in a template so the screen never reads configuration,
    and asked by construction rather than by name so adding a second backend does
    not need this function edited.
    """
    from resell.reasoning.adapters.search import NoSearchBackend, get_search_backend

    try:
        return not isinstance(get_search_backend(), NoSearchBackend)
    except Exception:  # noqa: BLE001 - an unusable backend is an unavailable one
        return False


def identity_terms(conn, sku: str) -> tuple[str, ...]:
    """The words that say which product this is, for filtering search results.

    A search for a product returns the product, its accessories, its spare parts
    and its carry cases, and eBay's own related-items carousels put all of them in
    one response -- one query came back with two hundred priced entries, a good
    number of them charging cables. Requiring a result's title to carry two of
    these terms removes what is certainly a different product. What is merely not
    comparable -- a stand for the speaker, a replacement plate for the dumbbell --
    is left to the judging stage, because that is a judgement and this is not.

    Deliberately not the whole aspect set: "Color: Black" and "Material: Plastic"
    match half the catalogue and would let anything through.
    """
    row = conn.execute(
        "SELECT brand, model, aspects FROM identification "
        "WHERE sku = ? AND superseded_at IS NULL", (sku,),
    ).fetchone()
    if row is None:
        return ()

    terms: list[str] = []
    for column in ("brand", "model"):
        value = row[column]
        if value:
            terms.append(str(value))
    try:
        aspects = json.loads(row["aspects"] or "{}")
    except (TypeError, ValueError):
        aspects = {}
    for name in DISTINCTIVE_ASPECTS:
        for value in aspects.get(name) or []:
            if value:
                terms.append(str(value))

    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        folded = term.casefold()
        if folded not in seen and len(folded) > 1:
            seen.add(folded)
            unique.append(term)
    return tuple(unique)


# --- what a hand-typed price actually earns ---------------------------------------


@dataclass(frozen=True)
class PriceCheck:
    """One price, after fees and costs. What the operator is really choosing.

    A manual price skips the strategy layer, which is where the floor normally
    lives -- so nothing was warning that $30 on an item that cost $25 loses money
    once eBay takes its cut. The arithmetic is the same `net_from_gross` the
    recommended prices use; only the number going in is different.
    """

    price_cents: int
    net_cents: int
    fee_cents: int
    cost_cents: int
    profit_cents: int
    floor_cents: int
    fee_basis: str
    # Whether anyone recorded what this item cost. Without it "profit" is just the
    # net wearing a different label, and showing $389 profit on an item whose cost
    # nobody knows is a lie by omission.
    cost_known: bool = True

    @property
    def above_floor(self) -> bool:
        return self.price_cents >= self.floor_cents

    @property
    def profitable(self) -> bool:
        return self.profit_cents > 0

    @property
    def warning(self) -> str:
        if not self.above_floor:
            return (
                f"below the floor: at this price the net proceeds do not cover the "
                f"minimum. {_money(self.floor_cents)} is the least worth listing at."
            )
        if self.cost_known and not self.profitable:
            return (
                f"loses money: {_money(self.net_cents)} net against "
                f"{_money(self.cost_cents)} paid for it."
            )
        if not self.cost_known:
            return (
                "no purchase cost is recorded for this item, so this is net "
                "proceeds and not profit."
            )
        return ""


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _floor_for(conn, sku: str, *, marketplace: str) -> int:
    """The least this item is worth listing at, or 0 if that cannot be worked out.

    Never raises: this is one field on a card, and a missing fee schedule should
    not take the whole screen down with it.
    """
    try:
        return price_check(conn, sku, 1000, marketplace=marketplace).floor_cents
    except Exception:  # noqa: BLE001 - a card without a floor beats no card
        return 0


def price_check(
    conn, sku: str, price_cents: int, *, marketplace: str,
    minimum_net_proceeds_cents: int = 500,
) -> PriceCheck:
    """Fees, net and profit for a price the operator typed."""
    from resell import store_pricing as sp
    from resell.pricing.proceeds import CostLines, gross_from_net, net_from_gross

    request_values = default_pricing_request(conn, sku, marketplace=marketplace)
    schedule, _version = sp.recorded_schedule(
        conn, marketplace=marketplace, category_id=request_values.category_id
    )
    costs = CostLines(
        seller_paid_shipping_cents=request_values.shipping_cost_cents
    )
    proceeds = net_from_gross(price_cents, schedule=schedule, costs=costs)
    paid = conn.execute(
        "SELECT purchase_cost_cents FROM item WHERE sku = ?", (sku,)
    ).fetchone()
    raw_cost = paid["purchase_cost_cents"] if paid else None
    cost_known = raw_cost is not None
    cost_cents = int(raw_cost or 0)

    return PriceCheck(
        price_cents=price_cents,
        net_cents=proceeds.net_cents,
        fee_cents=proceeds.marketplace_fee_cents + proceeds.ad_fee_cents,
        cost_cents=cost_cents,
        profit_cents=proceeds.net_cents - cost_cents,
        floor_cents=gross_from_net(
            minimum_net_proceeds_cents, schedule=schedule, costs=costs
        ),
        fee_basis=str(schedule.basis),
        cost_known=cost_known,
    )


def abandoned_count(conn: sqlite3.Connection) -> int:
    """How many items are set aside, so the inventory can offer to show them."""
    from resell.domain import ItemState

    return conn.execute(
        "SELECT COUNT(*) FROM item WHERE state = ?", (str(ItemState.ABANDONED),)
    ).fetchone()[0]
