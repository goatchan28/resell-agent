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

    return [_question_view(row, resolved[row["sku"]]) for row in rows]


def _question_view(row: sqlite3.Row, aspects: dict[str, list[str]]) -> QuestionView:
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

    questions = open_questions(conn, sku=sku)
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
    shipping_cost_cents: int = 0
    minimum_net_cents: int = 500
    brand_strength: str | None = None
    brand_citations: tuple[str, ...] = ()
    brand_rationale: str = ""


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

    scored = sp.load_scored_comps(conn, sku)
    retail = tuple(
        RetailReference(
            price_cents=cents,
            kind=RetailKind(request.retail_kind or "retail_original"),
        )
        for cents in request.retail_cents
    )
    rec = recommend(
        PricingInput(
            sku=sku,
            item_condition_band=ConditionBand(request.condition_band),
            identity_resolution=request.identity_resolution,
            comps=tuple(scored),
            retail=retail,
            window_days=request.window_days,
        )
    )

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
