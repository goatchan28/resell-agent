"""Persistence for the pricing slice. Plain SQL over sqlite3, no ORM.

Everything here takes an open connection and does no network work. The pure
modules in `resell.pricing` decide; this file records.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .pricing.comps import (
    CompBasis,
    CompClaim,
    CompObservation,
    Comparability,
    ConditionBand,
    ConditionSource,
    ModelVisibility,
    PriceKind,
    RetailKind,
    RetrievalMethod,
    validate_claim,
)
from .pricing.estimate import Distribution, PriceRecommendation, ScoredComp
from .domain import ItemState
from .pricing.lifecycle import (
    coerce_item_state,
    PriceApproval,
    PriceEventType,
    PriceProposal,
    PriceReason,
    PriceState,
)
from .pricing.proceeds import FeeBasis, FeeSchedule
from .pricing.strategy import SellerObjective

SCHEMA_PATH = Path(__file__).with_name("schema_pricing.sql")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def apply_schema(conn: sqlite3.Connection) -> None:
    """One entry point, so nothing can apply the schema the unmigrated way."""
    from .migrate import migrate

    migrate(conn)


# --- comps -------------------------------------------------------------------


def record_comp_observation(conn: sqlite3.Connection, obs: CompObservation) -> str:
    """Append-only. Re-observing the same listing later is a second row."""
    conn.execute(
        """
        INSERT INTO comp_observation (
            comp_id, marketplace, external_id, url, title, price_kind, basis,
            price_cents, currency, shipping_cents, observed_at, sale_date,
            days_on_market, condition_declared_raw, condition_band, condition_source,
            listing_format, quantity, seller_type, retail_kind, source_authority,
            retrieval_method, adapter, query_text, raw_payload_hash,
            source_excerpt, model_visibility, retention_expires_at, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            obs.comp_id, obs.marketplace, obs.external_id, obs.url, obs.title,
            str(obs.price_kind), str(obs.basis), obs.price_cents, obs.currency,
            obs.shipping_cents, obs.observed_at.isoformat(),
            obs.sale_date.isoformat() if obs.sale_date else None,
            obs.days_on_market, obs.condition_declared_raw, str(obs.condition_band),
            str(obs.condition_source), obs.listing_format, obs.quantity,
            obs.seller_type, str(obs.retail_kind) if obs.retail_kind else None,
            obs.source_authority, str(obs.retrieval_method), obs.adapter,
            obs.query_text, obs.raw_payload_hash, obs.source_excerpt,
            str(obs.model_visibility),
            obs.retention_expires_at.isoformat() if obs.retention_expires_at else None,
            _now(),
        ),
    )
    conn.commit()
    return obs.comp_id


def record_comp_claim(
    conn: sqlite3.Connection, claim: CompClaim, *, identity_resolution: str
) -> str:
    ok, why = validate_claim(claim, identity_resolution=identity_resolution)
    if not ok:
        raise ValueError(f"refused comp claim: {why}")
    conn.execute(
        """
        INSERT INTO comp_claim (claim_id, sku, comp_id, comparability,
            item_citations_json, comp_citations_json, rationale, excluded_reason,
            created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            claim.claim_id, claim.sku, claim.comp_id, str(claim.comparability),
            json.dumps(list(claim.item_citations)),
            json.dumps(list(claim.comp_citations)),
            claim.rationale, claim.excluded_reason, _now(),
        ),
    )
    conn.commit()
    return claim.claim_id


def load_scored_comps(conn: sqlite3.Connection, sku: str) -> list[ScoredComp]:
    rows = conn.execute(
        """
        SELECT c.*, o.*
        FROM comp_claim c JOIN comp_observation o ON o.comp_id = c.comp_id
        WHERE c.sku = ? AND o.purged_at IS NULL
        ORDER BY c.created_at
        """,
        (sku,),
    ).fetchall()
    return [_scored_from_row(r) for r in rows]


def _scored_from_row(r: sqlite3.Row) -> ScoredComp:
    obs = CompObservation(
        comp_id=r["comp_id"],
        marketplace=r["marketplace"],
        external_id=r["external_id"],
        price_kind=PriceKind(r["price_kind"]),
        basis=CompBasis(r["basis"]),
        price_cents=r["price_cents"],
        observed_at=datetime.fromisoformat(r["observed_at"]),
        condition_band=ConditionBand(r["condition_band"]),
        condition_declared_raw=r["condition_declared_raw"],
        condition_source=ConditionSource(r["condition_source"]),
        shipping_cents=r["shipping_cents"],
        days_on_market=r["days_on_market"],
        url=r["url"],
        title=r["title"],
        currency=r["currency"],
        listing_format=r["listing_format"],
        quantity=r["quantity"],
        seller_type=r["seller_type"],
        retail_kind=RetailKind(r["retail_kind"]) if r["retail_kind"] else None,
        source_authority=r["source_authority"],
        retrieval_method=RetrievalMethod(r["retrieval_method"]),
        adapter=r["adapter"],
        query_text=r["query_text"],
        raw_payload_hash=r["raw_payload_hash"],
        model_visibility=ModelVisibility(r["model_visibility"]),
    )
    claim = CompClaim(
        claim_id=r["claim_id"],
        sku=r["sku"],
        comp_id=r["comp_id"],
        comparability=Comparability(r["comparability"]),
        item_citations=tuple(json.loads(r["item_citations_json"])),
        comp_citations=tuple(json.loads(r["comp_citations_json"])),
        rationale=r["rationale"],
        excluded_reason=r["excluded_reason"],
    )
    return ScoredComp(claim=claim, observation=obs)


def freeze_comp_set(
    conn: sqlite3.Connection,
    sku: str,
    scored: list[ScoredComp],
    *,
    window_days: int = 90,
    aggregate: dict | None = None,
) -> tuple[str, str]:
    """Freeze a bundle and hash it. Returns (set_id, content_hash).

    The hash covers the member claim ids and their inclusion, so a proposal can
    point at a set and prove later that the set has not been re-cut underneath it.
    """
    set_id = _uid("cset")
    members = sorted(
        (s.claim.claim_id, 1 if s.claim.contributes else 0, s.claim.excluded_reason)
        for s in scored
    )
    blob = json.dumps(
        {"sku": sku, "window_days": window_days, "members": members},
        sort_keys=True, separators=(",", ":"),
    )
    content_hash = hashlib.sha256(blob.encode()).hexdigest()
    conn.execute(
        """INSERT INTO comp_set (set_id, sku, window_days, comparison_basis,
               content_hash, aggregate_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (set_id, sku, window_days, "total_to_buyer", content_hash,
         json.dumps(aggregate or {}), _now()),
    )
    for claim_id, included, reason in members:
        conn.execute(
            """INSERT INTO comp_set_member (set_id, claim_id, included, exclusion_reason)
               VALUES (?,?,?,?)""",
            (set_id, claim_id, included,
             reason or (None if included else "comparability does not contribute")),
        )
    conn.commit()
    return set_id, content_hash


def aggregate_for_storage(rec: PriceRecommendation) -> dict:
    """What survives a retention purge: the statistics, never the rows."""
    def dist(d: Distribution | None) -> dict | None:
        return None if d is None else {
            "kind": str(d.kind), "n": d.n, "min": d.min_cents, "p25": d.p25_cents,
            "median": d.median_cents, "p75": d.p75_cents, "max": d.max_cents,
            "mad": d.mad_cents,
        }
    return {
        "realized": dist(rec.realized),
        "asking": dist(rec.asking),
        "n_included": rec.n_included,
        "n_excluded": rec.n_excluded,
        "comparability_profile": rec.comparability_profile,
        "qualifiers": [str(q) for q in rec.qualifiers],
    }


def purge_expired_comps(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Retention and reproducibility, reconciled.

    Raw rows are purgeable; the frozen set's aggregate and the proposal snapshot
    are durable. A purged comp keeps its id, its marketplace and its purge stamp
    and loses url, title, payload and price detail. The purge is an event, not a
    silent DELETE.
    """
    rows = conn.execute(
        """SELECT comp_id FROM comp_observation
           WHERE purged_at IS NULL AND retention_expires_at IS NOT NULL
             AND retention_expires_at < ?""",
        (now.isoformat(),),
    ).fetchall()
    for r in rows:
        conn.execute(
            """UPDATE comp_observation
               SET url = NULL, title = NULL, raw_payload_hash = NULL,
                   item_specifics_json = NULL, query_text = NULL, purged_at = ?
               WHERE comp_id = ?""",
            (now.isoformat(), r["comp_id"]),
        )
    conn.commit()
    return len(rows)


# --- fee schedules -----------------------------------------------------------


def upsert_fee_schedule(conn: sqlite3.Connection, s: FeeSchedule) -> str:
    conn.execute(
        """INSERT OR REPLACE INTO fee_schedule (version, marketplace, category_id,
               effective_from, rate, fixed_cents, cap_cents,
               includes_shipping_in_base, includes_tax_in_base, basis, source_url,
               captured_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (s.version, s.marketplace, s.category_id,
         s.effective_from.isoformat() if s.effective_from else None,
         s.rate, s.fixed_cents, s.cap_cents,
         int(s.includes_shipping_in_base), int(s.includes_tax_in_base),
         str(s.basis), s.source_url,
         s.captured_at.isoformat() if s.captured_at else None, _now()),
    )
    conn.commit()
    return s.version


def list_fee_schedules(conn: sqlite3.Connection) -> list[dict]:
    conn.execute("PRAGMA foreign_keys = ON")
    rows = conn.execute(
        """SELECT * FROM fee_schedule
           ORDER BY marketplace, category_id IS NULL, category_id, effective_from"""
    ).fetchall()
    return [dict(r) for r in rows]


def recorded_schedule(
    conn: sqlite3.Connection, *, marketplace: str, category_id: str | None
):
    """The active schedule and the version to record against it.

    Two values because they answer different questions. `PROVISIONAL_DEFAULT` gives
    numbers to compute with, but it is a placeholder rather than a stored schedule,
    and `price_proposal.fee_schedule_version` is a foreign key -- so recording its
    name broke the insert on any database with no schedule for the category.

    The version is therefore empty when nothing was consulted, and that is the
    truthful record. What the estimate rests on travels in `fee_basis`, which stays
    `provisional_estimate` and is what the production gate reads.

    Empty string rather than None, and the difference is load-bearing: it is the
    dataclass's own default, `record_proposal` stores it as SQL NULL, and
    `load_proposal` reads NULL back as empty. A proposal has to hash identically
    before and after a round trip or the approval bound to it stops covering it --
    which is what a None here produced, silently and only at approval time.

    Returned rather than fixed up at insert time on purpose, for the same reason:
    rewriting the field underneath the caller changes the hash it was given.
    """
    from .pricing.proceeds import PROVISIONAL_DEFAULT

    found = active_fee_schedule(conn, marketplace=marketplace, category_id=category_id)
    if found is not None:
        return found, found.version
    return PROVISIONAL_DEFAULT, ""


def active_fee_schedule(
    conn: sqlite3.Connection, *, marketplace: str, category_id: str | None
) -> FeeSchedule | None:
    """Most specific match wins: the category row, else the marketplace default."""
    row = conn.execute(
        """SELECT * FROM fee_schedule
           WHERE marketplace = ? AND (category_id = ? OR category_id IS NULL)
           ORDER BY category_id IS NULL, effective_from DESC LIMIT 1""",
        (marketplace, category_id),
    ).fetchone()
    if row is None:
        return None
    from datetime import date as _date
    return FeeSchedule(
        version=row["version"], marketplace=row["marketplace"],
        category_id=row["category_id"],
        effective_from=_date.fromisoformat(row["effective_from"]) if row["effective_from"] else None,
        rate=row["rate"], fixed_cents=row["fixed_cents"], cap_cents=row["cap_cents"],
        includes_shipping_in_base=bool(row["includes_shipping_in_base"]),
        includes_tax_in_base=bool(row["includes_tax_in_base"]),
        basis=FeeBasis(row["basis"]), source_url=row["source_url"],
        captured_at=_date.fromisoformat(row["captured_at"]) if row["captured_at"] else None,
    )


# --- price lifecycle ---------------------------------------------------------


def record_proposal(
    conn: sqlite3.Connection,
    p: PriceProposal,
    *,
    uncertainty_note: str = "",
    sold_evidence_note: str = "",
    sample_exclusions: tuple[str, ...] = (),
) -> str:
    """The notes are stored beside the number, so the proposal explains itself.

    A price recorded without its uncertainty is a price somebody reads six months
    later and simply believes.
    """
    conn.execute(
        """INSERT INTO price_proposal (proposal_id, sku, reason, price_cents,
               previous_price_cents, supersedes, basis, price_kind, comp_set_id,
               comp_set_hash, band_low_cents, band_central_cents, band_high_cents,
               adjustments_json, qualifiers_json, fee_schedule_version, fee_basis,
               net_proceeds_cents, floor_ok, rationale, objective, anchor_statistic,
               anchor_value_cents, uncertainty_note, sold_evidence_note,
               sample_exclusions_json, content_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p.proposal_id, p.sku, str(p.reason), p.price_cents, p.previous_price_cents,
         p.supersedes, str(p.basis) if p.basis else None,
         str(p.price_kind) if p.price_kind else None, p.comp_set_id, p.comp_set_hash,
         p.band_low_cents, p.band_central_cents, p.band_high_cents,
         json.dumps([_adj(a) for a in p.adjustments]),
         json.dumps([str(q) for q in p.qualifiers]),
         p.fee_schedule_version or None, str(p.fee_basis), p.net_proceeds_cents,
         int(p.floor_ok), p.rationale,
         str(p.objective) if p.objective else None, p.anchor_statistic,
         p.anchor_value_cents, uncertainty_note, sold_evidence_note,
         json.dumps(list(sample_exclusions)),
         p.content_hash(), p.created_at.isoformat()),
    )
    if p.supersedes:
        conn.execute(
            """INSERT INTO price_event (sku, proposal_id, event_type, occurred_at,
                   price_cents, detail_json)
               VALUES (?,?,?,?,?,?)""",
            (p.sku, p.supersedes, str(PriceEventType.SUPERSEDED), _now(),
             p.previous_price_cents, json.dumps({"by": p.proposal_id})),
        )
    _event(conn, p.sku, p.proposal_id, PriceEventType.PROPOSED, p.price_cents)
    _set_state(conn, p.sku, PriceState.PROPOSED, proposal_id=None, price_cents=None)
    conn.commit()
    return p.proposal_id


def _adj(a) -> dict:
    d = asdict(a)
    d["source"] = str(a.source)
    d["from_band"] = str(a.from_band)
    d["to_band"] = str(a.to_band)
    d["citations"] = list(a.citations)
    return d


def approve_proposal(
    conn: sqlite3.Connection, proposal: PriceProposal, *, approved_by: str = "operator"
) -> PriceApproval:
    """Binds to the content hash. Any later change to the number or its evidence voids it.

    Idempotent: approving an unchanged proposal twice returns the existing
    approval rather than creating a second live one. Two live approvals over one
    proposal is not a stricter gate, it is an ambiguous one.
    """
    existing = live_approval(conn, proposal.proposal_id)
    if existing is not None and existing.content_hash == proposal.content_hash():
        return existing

    approval = PriceApproval(
        approval_id=_uid("papp"),
        proposal_id=proposal.proposal_id,
        content_hash=proposal.content_hash(),
        approved_at=datetime.now(timezone.utc),
        approved_by=approved_by,
    )
    conn.execute(
        """INSERT INTO price_approval (approval_id, proposal_id, content_hash,
               approved_at, approved_by) VALUES (?,?,?,?,?)""",
        (approval.approval_id, approval.proposal_id, approval.content_hash,
         approval.approved_at.isoformat(), approved_by),
    )
    _event(conn, proposal.sku, proposal.proposal_id, PriceEventType.APPROVED,
           proposal.price_cents)
    _set_state(conn, proposal.sku, PriceState.APPROVED, proposal_id=None, price_cents=None)
    conn.commit()
    return approval


def live_approval(conn: sqlite3.Connection, proposal_id: str) -> PriceApproval | None:
    row = conn.execute(
        """SELECT * FROM price_approval
           WHERE proposal_id = ? AND voided_at IS NULL
           ORDER BY approved_at DESC LIMIT 1""",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return None
    return PriceApproval(
        approval_id=row["approval_id"], proposal_id=row["proposal_id"],
        content_hash=row["content_hash"],
        approved_at=datetime.fromisoformat(row["approved_at"]),
        approved_by=row["approved_by"],
    )


def void_approval(conn: sqlite3.Connection, approval_id: str, reason: str) -> None:
    row = conn.execute(
        "SELECT proposal_id FROM price_approval WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    conn.execute(
        "UPDATE price_approval SET voided_at = ?, void_reason = ? WHERE approval_id = ?",
        (_now(), reason, approval_id),
    )
    if row:
        p = conn.execute(
            "SELECT sku, price_cents FROM price_proposal WHERE proposal_id = ?",
            (row["proposal_id"],),
        ).fetchone()
        if p:
            _event(conn, p["sku"], row["proposal_id"], PriceEventType.VOIDED,
                   p["price_cents"], detail={"reason": reason})
            # the missing-state-reversion bug, not repeated here
            _set_state(conn, p["sku"], PriceState.PROPOSED, proposal_id=None,
                       price_cents=None)
    conn.commit()


def record_applied(
    conn: sqlite3.Connection, proposal: PriceProposal, *, marketplace_ref: str | None
) -> None:
    """The price is now the one on the marketplace."""
    _event(conn, proposal.sku, proposal.proposal_id, PriceEventType.APPLIED,
           proposal.price_cents, marketplace_ref=marketplace_ref)
    _set_state(conn, proposal.sku, PriceState.LIVE,
               proposal_id=proposal.proposal_id, price_cents=proposal.price_cents)
    _sync_listing_price(conn, proposal.sku, proposal.price_cents)
    conn.commit()


def approved_price_cents(conn: sqlite3.Connection, sku: str) -> int | None:
    """The price this item currently has approval to charge, or None.

    The most recent proposal carrying a live approval that still covers it. A
    proposal whose content changed after approval does not count -- the approval
    is bound to a hash, and a hash that no longer matches is not an approval.
    """
    rows = conn.execute(
        "SELECT proposal_id FROM price_proposal WHERE sku = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (sku,),
    ).fetchall()
    for row in rows:
        proposal = load_proposal(conn, row["proposal_id"])
        if proposal is None:
            continue
        approval = live_approval(conn, proposal.proposal_id)
        if approval is not None and approval.covers(proposal):
            return proposal.price_cents
    return None


def listing_price_drift(conn: sqlite3.Connection, sku: str) -> tuple[int, int] | None:
    """(authoritative, cached) when the listing row disagrees, else None.

    `item_price_state.live_price_cents` is what the pricing layer confirmed against
    the marketplace. `listing.price_cents` is a cache of it. Read-only.
    """
    state = conn.execute(
        "SELECT live_price_cents FROM item_price_state WHERE sku = ?", (sku,)
    ).fetchone()
    row = conn.execute(
        "SELECT price_cents FROM listing WHERE sku = ? AND active = 1", (sku,)
    ).fetchone()
    if state is None or row is None or state["live_price_cents"] is None:
        return None
    if state["live_price_cents"] == row["price_cents"]:
        return None
    return state["live_price_cents"], row["price_cents"]


def reconcile_listing_price(conn: sqlite3.Connection, sku: str) -> str:
    """Bring the listing cache into line with the confirmed live price.

    Needed because `record_applied` only gained the sync after prices had already
    been applied, leaving rows written before that stale -- and `apply` correctly
    refuses to do anything for a proposal already marked applied. Weakening that
    idempotency to force a cache update would trade a real guarantee for a
    migration convenience; a separate operation costs nothing and says what it is.

    Purely local. The authoritative value was already confirmed against eBay when
    it was applied; this does not call the marketplace and cannot invent a price.
    """
    drift = listing_price_drift(conn, sku)
    if drift is None:
        return f"{sku}: listing cache already agrees"
    authoritative, cached = drift
    _sync_listing_price(conn, sku, authoritative)
    conn.commit()
    return (
        f"{sku}: listing cache {cached} -> {authoritative} "
        f"(from the confirmed live price)"
    )


def skus_with_listing_price_drift(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT sku FROM item_price_state WHERE live_price_cents IS NOT NULL"
    ).fetchall()
    return [r["sku"] for r in rows if listing_price_drift(conn, r["sku"])]


def _sync_listing_price(conn: sqlite3.Connection, sku: str, price_cents: int) -> None:
    """Keep listing.price_cents meaning "the price currently on the marketplace".

    That column is read by the publisher when building an offer, so leaving it at
    whatever was proposed originally means a relist would send a price the item
    has not carried for weeks. It is a derived cache of the pricing layer's
    answer, and this is its only writer.

    estimated_fees_cents is recomputed from fee_rate_used and fee_fixed_cents_used
    -- the columns stored beside it at proposal time -- so the row stays internally
    consistent rather than pairing a new price with fees for the old one.
    """
    row = conn.execute(
        "SELECT buyer_shipping_charge_cents, fee_rate_used, fee_fixed_cents_used "
        "FROM listing WHERE sku = ? AND active = 1",
        (sku,),
    ).fetchone()
    if row is None:
        return

    fees = None
    if row["fee_rate_used"] is not None:
        base = price_cents + (row["buyer_shipping_charge_cents"] or 0)
        fees = round(base * row["fee_rate_used"]) + (row["fee_fixed_cents_used"] or 0)

    conn.execute(
        "UPDATE listing SET price_cents = ?, estimated_fees_cents = ?, updated_at = ? "
        "WHERE sku = ? AND active = 1",
        (price_cents, fees, _now(), sku),
    )


def already_applied(conn: sqlite3.Connection, proposal_id: str) -> bool:
    """Idempotency: rerunning apply for the same proposal costs zero calls.

    Keyed on the proposal, not on its content hash. Those are different questions
    and conflating them was a bug: the hash excludes `reason`, so repricing back
    to an earlier price produced the same hash as the original proposal, and the
    executor concluded it was already live while the listing sat at the newer
    price. The hash binds the approval; the proposal id bounds the action.
    """
    row = conn.execute(
        """SELECT 1 FROM price_event
           WHERE proposal_id = ? AND event_type = 'applied' LIMIT 1""",
        (proposal_id,),
    ).fetchone()
    return row is not None


def item_state(conn: sqlite3.Connection, sku: str) -> ItemState:
    """The item's current state, read from the item record.

    Read-only, so it does not go through the gateway -- the gateway owns
    transitions, not lookups. What matters is that the pricing layer stops
    trusting a caller-supplied string: an operator asserting `listed` for an item
    the system considers `pricing` was a gate that could be talked around.
    """
    row = conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        raise LookupError(f"no item {sku!r}")
    return coerce_item_state(row["state"])


def offer_id_for(conn: sqlite3.Connection, sku: str) -> str:
    """The offerId `record_publish_progress` persisted at publish time.

    Typing `--offer-id` by hand invites pasting the wrong one, and the system
    already knows it. Raises rather than returning None: an item with no offer id
    has not been published, and the caller should say so plainly.

    Ambiguity is refused rather than resolved. The listing table carries
    marketplace and environment, so one SKU can eventually hold more than one
    active listing, and `fetchone()` would quietly pick whichever came first --
    pointing a sandbox reprice at a production offer, or the reverse. There is
    exactly one active listing today; the day there are two, this says so.
    """
    rows = conn.execute(
        "SELECT * FROM listing WHERE sku = ? AND active = 1", (sku,)
    ).fetchall()

    if not rows:
        raise LookupError(f"no active listing for {sku}; publish it before repricing")

    with_offers = [r for r in rows if r["offer_id"]]
    if not with_offers:
        raise LookupError(
            f"{sku} has an active listing but no offer id recorded; "
            "publish it before repricing"
        )
    if len(with_offers) > 1:
        detail = ", ".join(_describe_listing(r) for r in with_offers)
        raise LookupError(
            f"{sku} has {len(with_offers)} active listings ({detail}); "
            "pass --offer-id to say which one"
        )
    return with_offers[0]["offer_id"]


def _describe_listing(row: sqlite3.Row) -> str:
    """Name the columns that distinguish listings, without assuming they exist."""
    keys = row.keys()
    parts = [str(row[k]) for k in ("environment", "marketplace") if k in keys]
    parts.append(str(row["offer_id"]))
    return "/".join(parts)


def record_apply_failed(
    conn: sqlite3.Connection, proposal: PriceProposal, *, detail: str, stage: str
) -> None:
    """A failed attempt is history too. The approval is untouched."""
    _event(conn, proposal.sku, proposal.proposal_id, PriceEventType.APPLY_FAILED,
           proposal.price_cents, detail={"stage": stage, "detail": detail})
    conn.commit()


def revisions_today(conn: sqlite3.Connection, sku: str, *, now: datetime) -> int:
    """Applies counted against eBay's 250-revisions-per-listing-per-day ceiling.

    Counted on UTC days while eBay counts on its own clock, so the budget is set
    below the real limit rather than trying to align two calendars.
    """
    day = now.astimezone(timezone.utc).date().isoformat()
    row = conn.execute(
        """SELECT COUNT(*) n FROM price_event
           WHERE sku = ? AND event_type = 'applied' AND occurred_at >= ?""",
        (sku, day),
    ).fetchone()
    return int(row["n"])


def current_price_state(conn: sqlite3.Connection, sku: str) -> dict:
    row = conn.execute(
        "SELECT * FROM item_price_state WHERE sku = ?", (sku,)
    ).fetchone()
    if row is None:
        return {"sku": sku, "state": str(PriceState.UNPRICED),
                "live_proposal_id": None, "live_price_cents": None,
                "last_change_at": None}
    return dict(row)


def price_history(conn: sqlite3.Connection, sku: str) -> list[dict]:
    """The append-only record. This is what makes repricing legible after the fact."""
    rows = conn.execute(
        """SELECT e.*, p.reason, p.basis, p.price_kind
           FROM price_event e LEFT JOIN price_proposal p
             ON p.proposal_id = e.proposal_id
           WHERE e.sku = ? ORDER BY e.event_id""",
        (sku,),
    ).fetchall()
    return [dict(r) for r in rows]


def load_proposal(conn: sqlite3.Connection, proposal_id: str) -> PriceProposal | None:
    row = conn.execute(
        "SELECT * FROM price_proposal WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        return None
    return PriceProposal(
        proposal_id=row["proposal_id"], sku=row["sku"],
        reason=PriceReason(row["reason"]), price_cents=row["price_cents"],
        created_at=datetime.fromisoformat(row["created_at"]),
        basis=CompBasis(row["basis"]) if row["basis"] else None,
        price_kind=PriceKind(row["price_kind"]) if row["price_kind"] else None,
        comp_set_id=row["comp_set_id"], comp_set_hash=row["comp_set_hash"],
        band_low_cents=row["band_low_cents"],
        band_central_cents=row["band_central_cents"],
        band_high_cents=row["band_high_cents"],
        qualifiers=tuple(json.loads(row["qualifiers_json"])),
        fee_schedule_version=row["fee_schedule_version"] or "",
        fee_basis=FeeBasis(row["fee_basis"]),
        net_proceeds_cents=row["net_proceeds_cents"],
        floor_ok=bool(row["floor_ok"]), rationale=row["rationale"],
        supersedes=row["supersedes"],
        previous_price_cents=row["previous_price_cents"],
        objective=SellerObjective(row["objective"]) if row["objective"] else None,
        anchor_statistic=row["anchor_statistic"],
        anchor_value_cents=row["anchor_value_cents"],
    )


def latest_proposal(conn: sqlite3.Connection, sku: str) -> PriceProposal | None:
    row = conn.execute(
        "SELECT proposal_id FROM price_proposal WHERE sku = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (sku,),
    ).fetchone()
    return load_proposal(conn, row["proposal_id"]) if row else None


def _event(conn, sku, proposal_id, event_type, price_cents, *,
           marketplace_ref=None, detail=None) -> None:
    conn.execute(
        """INSERT INTO price_event (sku, proposal_id, event_type, occurred_at,
               price_cents, marketplace_ref, detail_json) VALUES (?,?,?,?,?,?,?)""",
        (sku, proposal_id, str(event_type), _now(), price_cents, marketplace_ref,
         json.dumps(detail or {})),
    )


def _set_state(conn, sku, state, *, proposal_id, price_cents) -> None:
    existing = conn.execute(
        "SELECT live_proposal_id, live_price_cents FROM item_price_state WHERE sku = ?",
        (sku,),
    ).fetchone()
    keep_id = proposal_id if proposal_id is not None else (
        existing["live_proposal_id"] if existing else None)
    keep_price = price_cents if price_cents is not None else (
        existing["live_price_cents"] if existing else None)
    conn.execute(
        """INSERT INTO item_price_state (sku, state, live_proposal_id,
               live_price_cents, last_change_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(sku) DO UPDATE SET state = excluded.state,
               live_proposal_id = excluded.live_proposal_id,
               live_price_cents = excluded.live_price_cents,
               last_change_at = excluded.last_change_at""",
        (sku, str(state), keep_id, keep_price,
         _now() if state is PriceState.LIVE else (
             existing["last_change_at"] if existing and "last_change_at" in existing.keys()
             else None)),
    )


# --- source policy -----------------------------------------------------------


def set_source_policy(
    conn: sqlite3.Connection,
    *,
    source: str,
    model_visibility: str,
    policy_version: str,
    licence_ref: str | None = None,
    note: str = "",
) -> None:
    """Record what a data source's licence permits. One row per source.

    The `source_policy` table shipped with the pricing schema and nothing wrote to
    it, which meant `model_visibility` was whatever a caller happened to pass --
    a licence term enforced by remembering to. It is now read at the point comps
    are recorded and again before anything reaches a prompt.
    """
    if model_visibility not in ("full", "derived_only", "none"):
        raise ValueError(f"unknown model_visibility {model_visibility!r}")
    conn.execute(
        "INSERT INTO source_policy (source, policy_version, model_visibility, "
        "licence_ref, decided_at, note) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET policy_version=excluded.policy_version, "
        "model_visibility=excluded.model_visibility, licence_ref=excluded.licence_ref, "
        "decided_at=excluded.decided_at, note=excluded.note",
        (source, policy_version, model_visibility, licence_ref, _now(), note),
    )
    conn.commit()


def source_policy(conn: sqlite3.Connection, source: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM source_policy WHERE source = ?", (source,)
    ).fetchone()
    return dict(row) if row else None


def list_source_policies(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r) for r in conn.execute("SELECT * FROM source_policy ORDER BY source")
    ]


def visibility_for_source(conn: sqlite3.Connection, source: str) -> ModelVisibility:
    """What a source's rows may be shown to, defaulting to the strictest answer.

    An unregistered source resolves to `derived_only`: code may compute statistics
    from its rows and a model may see the statistics, but the rows never enter a
    prompt. That is the conservative direction, and because the central estimate is
    computed deterministically it costs nothing but the model's view of the raw
    listings.
    """
    row = source_policy(conn, source)
    if row is None:
        return ModelVisibility.DERIVED_ONLY
    try:
        return ModelVisibility(row["model_visibility"])
    except ValueError:
        return ModelVisibility.DERIVED_ONLY


def promptable_comps(conn: sqlite3.Connection, sku: str) -> tuple[list, list]:
    """Split this item's comps into those a prompt may contain and those it may not.

    The licence question this exists for: eBay's API agreement forbids ingesting
    Restricted API data -- which it defines to include pricing and sales-volume
    data -- into a third-party AI without written consent. A comp obtained that way
    can still price the item, because `estimate.py` is arithmetic; what it cannot do
    is appear in a model's context.

    Returns (promptable, withheld), both as ScoredComp.
    """
    promptable, withheld = [], []
    for entry in load_scored_comps(conn, sku):
        if entry.observation.model_visibility is ModelVisibility.FULL:
            promptable.append(entry)
        else:
            withheld.append(entry)
    return promptable, withheld


def unclaimed_observations(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Comp observations that no claim connects to any SKU.

    `comp-add` records an observation and `claim` attaches it to an item; the join
    in `load_scored_comps` means an observation without a claim is invisible to
    pricing. That is the right data model -- the same listing can be a comp for
    several items, at different rungs -- but it makes "recorded" and "counted" two
    different states, and nothing said so. Two eBay comps were added and the band
    did not move.
    """
    return [
        dict(r) for r in conn.execute(
            """
            SELECT o.comp_id, o.price_kind, o.price_cents, o.condition_band,
                   o.title, o.created_at
              FROM comp_observation o
             WHERE o.purged_at IS NULL
               AND NOT EXISTS (SELECT 1 FROM comp_claim c WHERE c.comp_id = o.comp_id)
             ORDER BY o.created_at DESC LIMIT ?
            """,
            (limit,),
        )
    ]


# --- comp candidates ---------------------------------------------------------
#
# The third state between "recorded" and "counted". Everything here exists so an
# operator performs one action -- accept or reject -- and never learns that an
# observation and a claim are different rows.


def record_comp_candidate(
    conn: sqlite3.Connection,
    *,
    sku: str,
    comp_id: str,
    proposed_comparability: str,
    item_citations: tuple[str, ...] = (),
    comp_citations: tuple[str, ...] = (),
    rationale: str = "",
) -> str:
    """Offer a discovered comp for a decision. Idempotent per (sku, comp_id).

    Re-proposing a comp already decided does nothing: a rejected comp stays
    rejected until someone changes their mind explicitly, and re-running research
    must not resurrect it.
    """
    candidate_id = _uid("cand")
    conn.execute(
        "INSERT OR IGNORE INTO comp_candidate (candidate_id, sku, comp_id, "
        "proposed_comparability, item_citations_json, comp_citations_json, "
        "rationale, status, created_at) VALUES (?,?,?,?,?,?,?,'pending',?)",
        (candidate_id, sku, comp_id, proposed_comparability,
         json.dumps(list(item_citations)), json.dumps(list(comp_citations)),
         rationale, _now()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT candidate_id FROM comp_candidate WHERE sku = ? AND comp_id = ?",
        (sku, comp_id),
    ).fetchone()
    return row["candidate_id"]


def pending_comp_candidates(conn: sqlite3.Connection, sku: str) -> list[dict]:
    """What the operator is being asked about, with the listing beside it."""
    return [
        dict(r) for r in conn.execute(
            """
            SELECT k.candidate_id, k.comp_id, k.proposed_comparability, k.rationale,
                   k.item_citations_json, k.comp_citations_json,
                   o.price_cents, o.shipping_cents, o.price_kind, o.condition_band,
                   o.condition_declared_raw, o.title, o.url, o.marketplace,
                   o.days_on_market, o.source_excerpt
              FROM comp_candidate k JOIN comp_observation o ON o.comp_id = k.comp_id
             WHERE k.sku = ? AND k.status = 'pending' AND o.purged_at IS NULL
             ORDER BY o.price_cents
            """,
            (sku,),
        )
    ]


def accept_comp_candidate(
    conn: sqlite3.Connection, candidate_id: str, *, identity_resolution: str
) -> str:
    """Turn a candidate into a comp claim. One action, both rows.

    The ladder ceiling still applies: `record_comp_claim` refuses a rung above what
    the item's identity resolution supports, and the refusal surfaces rather than
    being demoted quietly. A candidate that cannot be accepted at its proposed rung
    stays pending, so the operator sees why instead of finding it silently gone.
    """
    row = conn.execute(
        "SELECT * FROM comp_candidate WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no comp candidate {candidate_id}")
    if row["status"] != "pending":
        raise ValueError(f"candidate {candidate_id} is already {row['status']}")

    item_citations = tuple(json.loads(row["item_citations_json"]))
    rationale = row["rationale"]
    if not item_citations:
        # A candidate the agent was not allowed to assess arrives with nothing on
        # the item side, because the agent made no match to cite. Accepting it is
        # the operator making that match, so the claim cites what describes this
        # item and records whose assertion it is. Without this the accept button
        # produced a claim the validator refused -- correctly, since a claim with
        # no item citation is untraceable.
        item_citations = _item_observation_ids(conn, row["sku"])
        rationale = (
            f"accepted by the operator, who assessed this listing themselves. "
            f"{rationale}"
        ).strip()
        if not item_citations:
            raise ValueError(
                f"{row['sku']} has no observations to cite, so no comp claim about "
                f"it can be traced back to anything"
            )

    claim = CompClaim(
        claim_id=_uid("claim"),
        sku=row["sku"],
        comp_id=row["comp_id"],
        comparability=Comparability(row["proposed_comparability"]),
        item_citations=item_citations,
        comp_citations=tuple(json.loads(row["comp_citations_json"])),
        rationale=rationale,
    )
    record_comp_claim(conn, claim, identity_resolution=identity_resolution)
    conn.execute(
        "UPDATE comp_candidate SET status = 'accepted', decided_at = ? "
        "WHERE candidate_id = ?",
        (_now(), candidate_id),
    )
    conn.commit()
    return claim.claim_id


def reject_comp_candidate(
    conn: sqlite3.Connection, candidate_id: str, *, reason: str
) -> str:
    """Reject a candidate, which records an excluded claim rather than a deletion.

    An exclusion with a reason is evidence; a deletion is a silent assumption. The
    excluded claim is what stops the same listing being proposed again and what
    lets `price recommend` account for it under "excluded by the operator".
    """
    row = conn.execute(
        "SELECT * FROM comp_candidate WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no comp candidate {candidate_id}")
    if row["status"] != "pending":
        raise ValueError(f"candidate {candidate_id} is already {row['status']}")
    if not reason.strip():
        raise ValueError("a rejected comp must record why")

    claim = CompClaim(
        claim_id=_uid("claim"),
        sku=row["sku"],
        comp_id=row["comp_id"],
        comparability=Comparability.EXCLUDED,
        item_citations=tuple(json.loads(row["item_citations_json"])),
        comp_citations=tuple(json.loads(row["comp_citations_json"])),
        rationale=row["rationale"],
        excluded_reason=reason,
    )
    # Exclusion is legal at any identity resolution; validate_claim short-circuits
    # on it, so the ceiling never blocks a rejection.
    record_comp_claim(conn, claim, identity_resolution="resolved")
    conn.execute(
        "UPDATE comp_candidate SET status = 'rejected', decided_at = ?, "
        "decided_reason = ? WHERE candidate_id = ?",
        (_now(), reason, candidate_id),
    )
    conn.commit()
    return claim.claim_id


def already_offered(conn: sqlite3.Connection, sku: str) -> set[str]:
    """Comp ids this item has already been offered or claimed, at any status."""
    return {
        r["comp_id"] for r in conn.execute(
            "SELECT comp_id FROM comp_candidate WHERE sku = ? "
            "UNION SELECT comp_id FROM comp_claim WHERE sku = ?",
            (sku, sku),
        )
    }


def _item_observation_ids(conn: sqlite3.Connection, sku: str) -> tuple[str, ...]:
    """The observations that describe this item, as claim citations.

    The same pool the judging stage draws its citations from, so an
    operator-accepted claim is traceable exactly the way an agent-judged one is.
    """
    return tuple(
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM evidence WHERE sku = ? AND send_to_model = 1 "
            "AND subject = 'this_item' ORDER BY id", (sku,),
        )
    )
