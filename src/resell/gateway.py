"""The deterministic effect gateway.

The reasoning plane proposes; this executes. A command is accepted only when the
transition is legal AND every precondition holds. Preconditions are queries over
stored facts -- never model judgment, never a confidence number.

Two commands are structurally unreachable by the model: AnswerQuestion and
Approve. They require operator=True, and nothing in the reasoning plane can set
that flag. This is the boundary the whole design rests on, so it is enforced by
type rather than by convention.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from resell.db import log_event, now_iso, transaction
from resell.domain import (
    DEFAULT_MINIMUM_NET_PROCEEDS_CENTS,
    TERMINAL_STATES,
    FeeModel,
    ItemState,
    Proposal,
    ShippingTerms,
    format_sku,
    is_legal_transition,
)


class Rejected(RuntimeError):
    """A command failed validation. Carries every reason, not just the first."""

    def __init__(self, command: str, reasons: list[str]):
        self.command = command
        self.reasons = reasons
        super().__init__(f"{command} rejected:\n" + "\n".join(f"  - {r}" for r in reasons))


@dataclass
class Accepted:
    command: str
    sku: str
    from_state: ItemState | None
    to_state: ItemState | None
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


# --- reads -------------------------------------------------------------------


def get_item(conn: sqlite3.Connection, sku: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM item WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        raise Rejected("lookup", [f"no item with sku {sku}"])
    return row


def current_state(conn: sqlite3.Connection, sku: str) -> ItemState:
    return ItemState(get_item(conn, sku)["state"])


def validated_photos(conn: sqlite3.Connection, sku: str) -> list[sqlite3.Row]:
    """Photos that passed local validation. Upload status is irrelevant here.

    Proposal no longer requires hosted images: uploading at proposal time would
    burn EPS uploads on items that are never approved and start a 30-day expiry
    clock during an open-ended human review. The Media layer guarantees fresh
    hosted URLs at publish instead.
    """
    return list(
        conn.execute(
            "SELECT * FROM photo WHERE sku = ? AND validated_at IS NOT NULL "
            "AND (validation_errors IS NULL OR validation_errors IN ('', '[]')) "
            "ORDER BY position",
            (sku,),
        ).fetchall()
    )


def unresolved_blocking_questions(conn: sqlite3.Connection, sku: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM open_question WHERE sku = ? AND blocking = 1 "
            "AND answered_at IS NULL ORDER BY asked_at",
            (sku,),
        ).fetchall()
    )


def current_identification(conn: sqlite3.Connection, sku: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM identification WHERE sku = ? AND superseded_at IS NULL "
        "ORDER BY version DESC LIMIT 1",
        (sku,),
    ).fetchone()


def active_listing(conn: sqlite3.Connection, sku: str, marketplace: str, environment: str):
    return conn.execute(
        "SELECT * FROM listing WHERE sku = ? AND marketplace = ? AND environment = ? "
        "AND active = 1",
        (sku, marketplace, environment),
    ).fetchone()


def live_approval(conn: sqlite3.Connection, sku: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM approval WHERE sku = ? AND voided_at IS NULL "
        "ORDER BY approved_at DESC LIMIT 1",
        (sku,),
    ).fetchone()


# --- the gateway -------------------------------------------------------------


class Gateway:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        marketplace: str = "EBAY_US",
        environment: str = "sandbox",
        minimum_net_proceeds_cents: int = DEFAULT_MINIMUM_NET_PROCEEDS_CENTS,
        fees: FeeModel | None = None,
    ):
        self.conn = conn
        self.marketplace = marketplace
        self.environment = environment
        self.minimum_net_proceeds_cents = minimum_net_proceeds_cents
        self.fees = fees or FeeModel()

    # --- internals -----------------------------------------------------------

    def _entry_preconditions(self, sku: str, target: ItemState) -> list[str]:
        """Conditions that must hold to ENTER a state, regardless of caller.

        These duplicate checks the public commands already make, deliberately. The
        earlier design checked preconditions in the command and then called
        _transition, which meant the invariant held only as long as every caller
        remembered -- and a direct _transition call moved an item to `publishing`
        with a voided approval. Enforcing on entry closes that by construction:
        there is no code path into a state that skips its conditions.
        """
        reasons: list[str] = []

        if target == ItemState.IDENTIFYING:
            if not validated_photos(self.conn, sku):
                reasons.append("no photos have passed local validation")

        elif target == ItemState.PRICING:
            identification = current_identification(self.conn, sku)
            if identification is None:
                reasons.append("no identification recorded")
            else:
                for column in ("title", "category_id", "condition_id"):
                    if not identification[column]:
                        reasons.append(f"identification is missing {column}")
            for question in unresolved_blocking_questions(self.conn, sku):
                reasons.append(f"blocking question unanswered: {question['question'][:80]}")

        elif target in (ItemState.APPROVED, ItemState.PUBLISHING):
            approval = live_approval(self.conn, sku)
            listing = active_listing(self.conn, sku, self.marketplace, self.environment)
            if approval is None:
                reasons.append("no live approval; it was voided or never granted")
            if listing is None:
                reasons.append("no active listing row")
            if listing is not None:
                snapshot = self._proposal_from_listing(sku, listing)
                # Re-validate the content, not only the hash. A hash match proves the
                # proposal has not changed since approval; it does not prove the
                # proposal is still valid. Deleting every photo, for instance, changes
                # the hash and voids the old approval -- but nothing would stop a fresh
                # approval of a now-photoless listing without this check.
                for problem in snapshot.validate(
                    minimum_net_proceeds_cents=self.minimum_net_proceeds_cents,
                    fees=self.fees,
                ):
                    reasons.append(f"proposal is no longer valid: {problem}")
                if approval is not None:
                    actual = snapshot.content_hash()
                    if actual != approval["proposal_hash"]:
                        reasons.append(
                            "the live approval does not match the current proposal; "
                            "re-approval is required"
                        )
            if target == ItemState.PUBLISHING and self.environment == "production" and not self.fees.is_authoritative:
                reasons.append(
                    f"fee basis is {self.fees.basis}, which cannot back a "
                    "minimum-net-proceeds guarantee in production"
                )

        elif target == ItemState.LISTED:
            listing = active_listing(self.conn, sku, self.marketplace, self.environment)
            if listing is None or not listing["listing_id"]:
                reasons.append(
                    "no listing_id recorded; an HTTP success alone does not prove publication"
                )

        return reasons

    def _transition(self, sku: str, target: ItemState, *, command: str, detail: str = "") -> Accepted:
        """Apply a state change. Legality AND entry preconditions are enforced here."""
        item = get_item(self.conn, sku)
        source = ItemState(item["state"])
        if not is_legal_transition(source, target):
            raise Rejected(command, [f"{source} -> {target} is not a legal transition"])

        blockers = self._entry_preconditions(sku, target)
        if blockers:
            raise Rejected(command, blockers)

        stamp = now_iso()
        self.conn.execute(
            "UPDATE item SET state = ?, state_changed_at = ?, updated_at = ? WHERE sku = ?",
            (str(target), stamp, stamp, sku),
        )
        log_event(
            self.conn,
            "item.state_changed",
            {"command": command, "from": str(source), "to": str(target), "detail": detail},
            item_id=sku,
        )
        return Accepted(command, sku, source, target, detail)

    def _void_approvals(self, sku: str, reason: str) -> int:
        """Void live approvals, and revert the state along with them.

        An item left in `approved` after its approval was voided is safe -- the
        entry precondition blocks publishing -- but the state label lies. Reverting
        to `proposed` keeps the invariant "state == approved implies a live matching
        approval" actually true, and leaves the item one re-approval away rather
        than sending it back through pricing.
        """
        cursor = self.conn.execute(
            "UPDATE approval SET voided_at = ?, voided_reason = ? "
            "WHERE sku = ? AND voided_at IS NULL",
            (now_iso(), reason, sku),
        )
        if not cursor.rowcount:
            return 0

        log_event(
            self.conn,
            "approval.voided",
            {"count": cursor.rowcount, "reason": reason},
            item_id=sku,
        )
        if current_state(self.conn, sku) == ItemState.APPROVED:
            self._transition(
                sku, ItemState.PROPOSED, command="ApprovalVoided", detail=reason
            )
        return cursor.rowcount

    # --- commands the reasoning plane may propose ----------------------------

    def ingest_item(
        self,
        *,
        purchase_cost_cents: int | None,
        acquisition_intent: str = "unknown",
        acquired_on: str | None = None,
        notes: str | None = None,
    ) -> Accepted:
        """Allocate a SKU and create the item. The only command without one."""
        if acquisition_intent not in ("resale", "declutter", "unknown"):
            raise Rejected("IngestItem", [f"unknown acquisition_intent {acquisition_intent!r}"])
        if purchase_cost_cents is not None and purchase_cost_cents < 0:
            raise Rejected("IngestItem", ["purchase cost cannot be negative"])

        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO sku_sequence (allocated_at) VALUES (?)", (now_iso(),)
            )
            seq = cursor.lastrowid
            sku = format_sku(seq)
            stamp = now_iso()
            self.conn.execute(
                "INSERT INTO item (sku, seq, state, purchase_cost_cents, "
                "acquisition_intent, acquired_on, notes, created_at, updated_at, "
                "state_changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sku, seq, str(ItemState.INTAKE), purchase_cost_cents,
                    acquisition_intent, acquired_on, notes, stamp, stamp, stamp,
                ),
            )
            log_event(
                self.conn,
                "item.created",
                {"sku": sku, "intent": acquisition_intent,
                 "purchase_cost_cents": purchase_cost_cents},
                item_id=sku,
            )
        return Accepted("IngestItem", sku, None, ItemState.INTAKE, f"allocated {sku}")

    def _require_mutable_photos(self, sku: str, command: str) -> None:
        """Photo changes are refused once the item is terminal.

        A published listing lives on eBay; changing the local photo set would
        silently desync the two, and listing revision is not implemented. Better to
        refuse than to diverge.
        """
        state = current_state(self.conn, sku)
        if state in TERMINAL_STATES:
            raise Rejected(
                command,
                [
                    f"item is {state}; the photo set cannot change. "
                    "Revising a published listing is not implemented."
                ],
            )

    def attach_photo(
        self,
        sku: str,
        *,
        source_path: str,
        content_sha256: str,
        image_format: str | None,
        size_bytes: int | None,
        validation_errors: list[str] | None,
    ) -> Accepted:
        get_item(self.conn, sku)
        self._require_mutable_photos(sku, "AttachPhoto")
        position = (
            self.conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM photo WHERE sku = ?", (sku,)
            ).fetchone()[0]
        )
        try:
            self.conn.execute(
                "INSERT INTO photo (sku, position, source_path, content_sha256, "
                "image_format, size_bytes, validated_at, validation_errors, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sku, position, source_path, content_sha256, image_format, size_bytes,
                    now_iso(), json.dumps(validation_errors or []), now_iso(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise Rejected("AttachPhoto", [f"photo already attached to {sku}: {exc}"]) from exc

        # Changing the photo set changes what an approval covered.
        self._void_approvals(sku, "photo set changed")
        return Accepted("AttachPhoto", sku, None, None, f"position {position}")

    def remove_photo(
        self, sku: str, *, position: int | None = None, content_sha256: str | None = None
    ) -> Accepted:
        """Detach a photo. Voids approvals exactly as attaching one does.

        Positions are compacted afterwards so they stay 1..N with no gaps -- eBay
        treats the first image as the gallery photo, so a stable contiguous order
        is part of the listing's meaning, not just tidiness.
        """
        get_item(self.conn, sku)
        self._require_mutable_photos(sku, "RemovePhoto")

        if (position is None) == (content_sha256 is None):
            raise Rejected("RemovePhoto", ["give exactly one of position or content_sha256"])

        if position is not None:
            row = self.conn.execute(
                "SELECT * FROM photo WHERE sku = ? AND position = ?", (sku, position)
            ).fetchone()
            identifier = f"position {position}"
        else:
            row = self.conn.execute(
                "SELECT * FROM photo WHERE sku = ? AND content_sha256 = ?",
                (sku, content_sha256),
            ).fetchone()
            identifier = f"sha {content_sha256[:12]}"

        if row is None:
            raise Rejected("RemovePhoto", [f"no photo at {identifier} for {sku}"])

        with transaction(self.conn):
            self.conn.execute("DELETE FROM photo WHERE id = ?", (row["id"],))
            # Renumber ascending so each UPDATE moves into a vacated slot and the
            # (sku, position) uniqueness constraint is never violated mid-compaction.
            remaining = self.conn.execute(
                "SELECT id FROM photo WHERE sku = ? ORDER BY position", (sku,)
            ).fetchall()
            for index, photo in enumerate(remaining, start=1):
                self.conn.execute(
                    "UPDATE photo SET position = ? WHERE id = ?", (index, photo["id"])
                )
            log_event(
                self.conn,
                "photo.removed",
                {"removed": identifier, "source_path": row["source_path"],
                 "remaining": len(remaining)},
                item_id=sku,
            )
            self._void_approvals(sku, "photo set changed")

        return Accepted(
            "RemovePhoto", sku, None, None,
            f"removed {identifier}; {len(remaining)} photo(s) remain",
        )

    def record_evidence(
        self,
        sku: str,
        *,
        kind: str,
        source: str,
        payload: dict,
        confidence: float | None = None,
        send_to_model: bool = True,
    ) -> Accepted:
        """Append-only. Provenance is mandatory; the database enforces immutability."""
        get_item(self.conn, sku)
        if not source:
            raise Rejected("RecordEvidence", ["source (provenance) is required"])
        self.conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, confidence, "
            "send_to_model, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sku, kind, source, json.dumps(payload), confidence,
             1 if send_to_model else 0, now_iso()),
        )
        return Accepted("RecordEvidence", sku, None, None, f"{kind} from {source}")

    def begin_identification(self, sku: str) -> Accepted:
        """intake -> identifying. Requires at least one locally valid photo."""
        reasons = []
        if not validated_photos(self.conn, sku):
            reasons.append("no photos have passed local validation")
        if reasons:
            raise Rejected("BeginIdentification", reasons)
        return self._transition(sku, ItemState.IDENTIFYING, command="BeginIdentification")

    def propose_identification(
        self,
        sku: str,
        *,
        brand: str | None = None,
        model: str | None = None,
        variant: str | None = None,
        title: str | None = None,
        description: str | None = None,
        condition_id: str | None = None,
        category_id: str | None = None,
        aspect_schema: dict | None = None,
        aspects: dict | None = None,
        confidence: float | None = None,
        reasoning: str | None = None,
    ) -> Accepted:
        """Supersede the previous belief rather than editing it.

        confidence is stored for diagnostics and evaluation. It is never consulted
        by any precondition -- a number the model chooses should not decide whether
        real money moves.
        """
        get_item(self.conn, sku)
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM identification WHERE sku = ?",
                (sku,),
            ).fetchone()
            version = row[0]
            self.conn.execute(
                "UPDATE identification SET superseded_at = ? WHERE sku = ? AND superseded_at IS NULL",
                (now_iso(), sku),
            )
            self.conn.execute(
                "INSERT INTO identification (sku, version, brand, model, variant, title, "
                "description, condition_id, category_id, aspect_schema, aspects, "
                "confidence, reasoning, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sku, version, brand, model, variant, title, description, condition_id,
                    category_id, json.dumps(aspect_schema) if aspect_schema else None,
                    json.dumps(aspects) if aspects else None, confidence, reasoning, now_iso(),
                ),
            )
        self._void_approvals(sku, "identification revised")
        return Accepted("ProposeIdentification", sku, None, None, f"version {version}")

    def ask_operator(self, sku: str, *, question: str, why_it_matters: str = "", blocking: bool = True) -> Accepted:
        """The operator-as-tool call. A blocking question moves the item to needs_info."""
        if not question.strip():
            raise Rejected("AskOperator", ["question is empty"])
        get_item(self.conn, sku)
        self.conn.execute(
            "INSERT INTO open_question (sku, question, why_it_matters, blocking, asked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sku, question, why_it_matters, 1 if blocking else 0, now_iso()),
        )
        state = current_state(self.conn, sku)
        if blocking and state == ItemState.IDENTIFYING:
            return self._transition(
                sku, ItemState.NEEDS_INFO, command="AskOperator", detail=question[:120]
            )
        return Accepted("AskOperator", sku, state, state, question[:120])

    def begin_pricing(self, sku: str) -> Accepted:
        """identifying -> pricing.

        The gate is structural, not numeric: required information present and no
        unresolved blocking unknowns. If the model is unsure it should open a
        question, which is a fact the gateway can check, rather than lower a
        confidence score, which is not.
        """
        reasons = []
        identification = current_identification(self.conn, sku)
        if identification is None:
            reasons.append("no identification recorded")
        else:
            for column in ("title", "category_id", "condition_id"):
                if not identification[column]:
                    reasons.append(f"identification is missing {column}")
        for question in unresolved_blocking_questions(self.conn, sku):
            reasons.append(f"blocking question unanswered: {question['question'][:80]}")
        if reasons:
            raise Rejected("BeginPricing", reasons)
        return self._transition(sku, ItemState.PRICING, command="BeginPricing")

    def propose_listing(self, sku: str, proposal: Proposal, *, required_aspects: set[str] | None = None) -> Accepted:
        """pricing -> proposed. This is the deterministic validation gate."""
        reasons = list(
            proposal.validate(
                required_aspects=required_aspects,
                minimum_net_proceeds_cents=self.minimum_net_proceeds_cents,
                fees=self.fees,
            )
        )
        if proposal.sku != sku:
            reasons.append(f"proposal sku {proposal.sku} does not match {sku}")

        photos = validated_photos(self.conn, sku)
        known = {photo["content_sha256"] for photo in photos}
        for digest in proposal.photo_hashes:
            if digest not in known:
                reasons.append(f"photo {digest[:12]} is not an attached validated photo of {sku}")
        for question in unresolved_blocking_questions(self.conn, sku):
            reasons.append(f"blocking question unanswered: {question['question'][:80]}")
        if reasons:
            raise Rejected("ProposeListing", reasons)

        stamp = now_iso()
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE listing SET active = 0, updated_at = ? WHERE sku = ? AND "
                "marketplace = ? AND environment = ? AND active = 1 AND listing_id IS NULL",
                (stamp, sku, proposal.marketplace, self.environment),
            )
            self.conn.execute(
                "INSERT INTO listing (sku, marketplace, environment, title, description, "
                "category_id, condition_id, aspects, price_cents, currency, "
                "shipping_terms, seller_shipping_cost_cents, buyer_shipping_charge_cents, "
                "estimated_fees_cents, fee_basis, fee_rate_used, fee_fixed_cents_used, "
                "fulfillment_policy_id, "
                "payment_policy_id, return_policy_id, merchant_location_key, "
                "created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sku, proposal.marketplace, self.environment, proposal.title,
                    proposal.description, proposal.category_id, proposal.condition_id,
                    json.dumps(proposal.aspects), proposal.price_cents, proposal.currency,
                    str(proposal.shipping_terms), proposal.seller_shipping_cost_cents,
                    proposal.buyer_shipping_charge_cents,
                    self.fees.fees_for(
                        proposal.price_cents + proposal.buyer_shipping_charge_cents
                    ),
                    str(self.fees.basis), self.fees.rate, self.fees.fixed_cents,
                    proposal.fulfillment_policy_id, proposal.payment_policy_id,
                    proposal.return_policy_id, proposal.merchant_location_key, stamp, stamp,
                ),
            )
            self._void_approvals(sku, "new proposal supersedes it")
            result = self._transition(
                sku, ItemState.PROPOSED, command="ProposeListing",
                detail=f"hash {proposal.content_hash()[:12]}",
            )
        result.data["proposal_hash"] = proposal.content_hash()
        return result

    def revise(self, sku: str, reason: str = "operator requested revision") -> Accepted:
        """Back to pricing from proposed / approved / publish_failed. Voids approvals."""
        self._void_approvals(sku, reason)
        return self._transition(sku, ItemState.PRICING, command="Revise", detail=reason)

    def abandon(self, sku: str, reason: str) -> Accepted:
        if not reason.strip():
            raise Rejected("Abandon", ["a reason is required"])
        self._void_approvals(sku, "item abandoned")
        self.conn.execute(
            "UPDATE listing SET active = 0, updated_at = ? WHERE sku = ? AND active = 1",
            (now_iso(), sku),
        )
        return self._transition(sku, ItemState.ABANDONED, command="Abandon", detail=reason)

    # --- operator-only commands ---------------------------------------------

    def answer_question(self, question_id: int, answer: str, *, operator: bool = False) -> Accepted:
        """Operator-only. Answering own questions would defeat the whole loop."""
        if not operator:
            raise Rejected("AnswerQuestion", ["only the operator may answer a question"])
        if not answer.strip():
            raise Rejected("AnswerQuestion", ["answer is empty"])

        row = self.conn.execute(
            "SELECT * FROM open_question WHERE id = ?", (question_id,)
        ).fetchone()
        if row is None:
            raise Rejected("AnswerQuestion", [f"no question with id {question_id}"])
        if row["answered_at"]:
            raise Rejected("AnswerQuestion", [f"question {question_id} is already answered"])

        sku = row["sku"]
        self.conn.execute(
            "UPDATE open_question SET answer = ?, answered_at = ? WHERE id = ?",
            (answer, now_iso(), question_id),
        )
        self.conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, send_to_model, recorded_at) "
            "VALUES (?, 'operator_answer', 'operator', ?, 1, ?)",
            (sku, json.dumps({"question": row["question"], "answer": answer}), now_iso()),
        )

        state = current_state(self.conn, sku)
        if state == ItemState.NEEDS_INFO and not unresolved_blocking_questions(self.conn, sku):
            return self._transition(
                sku, ItemState.IDENTIFYING, command="AnswerQuestion",
                detail="all blocking questions answered",
            )
        return Accepted("AnswerQuestion", sku, state, state, f"question {question_id} answered")

    def approve(self, sku: str, proposal_hash: str, *, operator: bool = False) -> Accepted:
        """Operator-only. proposed -> approved.

        The hash is required and must match the current proposal. Approving by SKU
        alone would let content drift between approval and publish; binding to the
        hash makes the approval an approval of specific bytes.
        """
        if not operator:
            raise Rejected("Approve", ["only the operator may approve a listing"])

        state = current_state(self.conn, sku)
        if state != ItemState.PROPOSED:
            raise Rejected("Approve", [f"item is {state}, not proposed"])

        listing = active_listing(self.conn, sku, self.marketplace, self.environment)
        if listing is None:
            raise Rejected("Approve", ["no active proposal to approve"])

        snapshot = self._proposal_from_listing(sku, listing)
        actual = snapshot.content_hash()
        if actual != proposal_hash:
            supplied = proposal_hash.strip()
            # Truncating both sides for display made a length mismatch render as two
            # identical strings that "do not match", which reads as a broken program.
            # Distinguish a truncated paste from genuine content drift, and never
            # abbreviate the two values being compared.
            if actual.startswith(supplied) and len(supplied) < len(actual):
                raise Rejected(
                    "Approve",
                    [
                        f"the hash is truncated: {len(supplied)} characters supplied, "
                        f"{len(actual)} required.",
                        "It matches as far as it goes, so this is a copy/paste issue "
                        "rather than a changed proposal.",
                        "Full hash:",
                        f"  {actual}",
                        "Retrieve it any time with: resell item show " + sku,
                    ],
                )
            raise Rejected(
                "Approve",
                [
                    "proposal hash does not match the current proposal -- it changed "
                    "after you saw it.",
                    f"  you supplied ({len(supplied)} chars): {supplied}",
                    f"  current      ({len(actual)} chars): {actual}",
                ],
            )

        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO approval (sku, proposal_hash, proposal_snapshot, "
                "approved_by, approved_at) VALUES (?, ?, ?, 'operator', ?)",
                (sku, actual, snapshot.canonical(), now_iso()),
            )
            result = self._transition(
                sku, ItemState.APPROVED, command="Approve", detail=f"hash {actual[:12]}"
            )
        result.data["proposal_hash"] = actual
        return result

    # --- publishing ----------------------------------------------------------

    def begin_publishing(self, sku: str) -> Accepted:
        """approved -> publishing. Entry preconditions do the enforcing."""
        return self._transition(sku, ItemState.PUBLISHING, command="BeginPublishing")

    def record_publish_progress(
        self,
        sku: str,
        *,
        has_inventory_item: bool | None = None,
        offer_id: str | None = None,
        listing_id: str | None = None,
    ) -> Accepted:
        """Fine-grained progress within the three eBay calls.

        Kept on the listing row rather than as extra item states so a partial
        failure resumes at the right call. createOrReplaceInventoryItem is an
        idempotent PUT; createOffer is not, which is why offer_id is persisted the
        moment it exists.
        """
        listing = active_listing(self.conn, sku, self.marketplace, self.environment)
        if listing is None:
            raise Rejected("RecordPublishProgress", ["no active listing row"])

        updates, params = [], []
        if has_inventory_item is not None:
            updates.append("has_inventory_item = ?")
            params.append(1 if has_inventory_item else 0)
        if offer_id is not None:
            updates.append("offer_id = ?")
            params.append(offer_id)
        if listing_id is not None:
            updates.extend(["listing_id = ?", "published_at = ?"])
            params.extend([listing_id, now_iso()])
        if not updates:
            raise Rejected("RecordPublishProgress", ["nothing to record"])

        updates.append("updated_at = ?")
        params.extend([now_iso(), listing["id"]])
        self.conn.execute(f"UPDATE listing SET {', '.join(updates)} WHERE id = ?", params)
        log_event(
            self.conn,
            "listing.progress",
            {"has_inventory_item": has_inventory_item, "offer_id": offer_id,
             "listing_id": listing_id},
            item_id=sku,
        )
        return Accepted("RecordPublishProgress", sku, None, None, "recorded")

    def mark_listed(self, sku: str) -> Accepted:
        """publishing -> listed. Requires a real listing_id.

        An HTTP 2xx is not proof of publication -- during the spike a stubbed 2xx
        with no listingId looked exactly like success. The identifier is the
        evidence.
        """
        listing = active_listing(self.conn, sku, self.marketplace, self.environment)
        listing_id = listing["listing_id"] if listing else None
        return self._transition(
            sku, ItemState.LISTED, command="MarkListed",
            detail=f"listingId={listing_id}",
        )

    def mark_publish_failed(self, sku: str, *, error_ids: list[int], detail: str) -> Accepted:
        return self._transition(
            sku, ItemState.PUBLISH_FAILED, command="MarkPublishFailed",
            detail=f"errorIds={error_ids} {detail[:200]}",
        )

    # --- helpers -------------------------------------------------------------

    def _proposal_from_listing(self, sku: str, listing: sqlite3.Row) -> Proposal:
        photos = validated_photos(self.conn, sku)
        return Proposal(
            sku=sku,
            marketplace=listing["marketplace"],
            title=listing["title"] or "",
            description=listing["description"] or "",
            category_id=listing["category_id"] or "",
            condition_id=listing["condition_id"] or "",
            aspects=json.loads(listing["aspects"]) if listing["aspects"] else {},
            price_cents=listing["price_cents"] or 0,
            currency=listing["currency"],
            shipping_terms=ShippingTerms(listing["shipping_terms"]),
            seller_shipping_cost_cents=listing["seller_shipping_cost_cents"],
            buyer_shipping_charge_cents=listing["buyer_shipping_charge_cents"],
            photo_hashes=tuple(photo["content_sha256"] for photo in photos),
            fulfillment_policy_id=listing["fulfillment_policy_id"] or "",
            payment_policy_id=listing["payment_policy_id"] or "",
            return_policy_id=listing["return_policy_id"] or "",
            merchant_location_key=listing["merchant_location_key"] or "",
        )

    def model_context(self, sku: str) -> dict:
        """Everything the reasoning plane is allowed to see about an item.

        Filters on send_to_model, so a record can be retained for audit without
        being eligible for a prompt.
        """
        item = get_item(self.conn, sku)
        evidence = self.conn.execute(
            "SELECT kind, source, payload, confidence, recorded_at FROM evidence "
            "WHERE sku = ? AND send_to_model = 1 ORDER BY recorded_at",
            (sku,),
        ).fetchall()
        identification = current_identification(self.conn, sku)
        return {
            "sku": sku,
            "state": item["state"],
            "acquisition_intent": item["acquisition_intent"],
            "purchase_cost_cents": item["purchase_cost_cents"],
            "photos": [
                {"position": p["position"], "format": p["image_format"]}
                for p in validated_photos(self.conn, sku)
            ],
            "evidence": [dict(row) for row in evidence],
            "identification": dict(identification) if identification else None,
            "open_questions": [
                {"id": q["id"], "question": q["question"], "blocking": bool(q["blocking"])}
                for q in unresolved_blocking_questions(self.conn, sku)
            ],
        }
