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
from resell.reasoning.gaps import EscalationDecision, escalation_policy
from resell.reasoning.schema import Basis, IdentificationEffort, Subject
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


def settle_answered_aspect_questions(conn: sqlite3.Connection, sku: str) -> list[int]:
    """Close blocking questions about aspects the item already has a value for.

    A question is a request for something missing. Once the value is on the
    identification the request is met, whoever met it -- so leaving it open holds
    the item in `needs_info` over a thing that is no longer absent, and shows the
    operator a prompt they have already answered.

    MP-000016 is the case: publish computes what is missing from the *frozen*
    listing, so an aspect supplied after the proposal was made looked missing for
    ever, and each answer was followed by a fresh pair of questions.
    """
    import json as _json

    row = conn.execute(
        "SELECT aspects FROM identification WHERE sku = ? AND superseded_at IS NULL",
        (sku,),
    ).fetchone()
    if row is None or not row["aspects"]:
        return []
    try:
        aspects = _json.loads(row["aspects"])
    except (TypeError, ValueError):
        return []
    known = {
        name for name, vals in aspects.items()
        if vals and any(str(v).strip() for v in vals)
    }
    if not known:
        return []

    settled = []
    for question in conn.execute(
        "SELECT id, aspect_name FROM open_question "
        "WHERE sku = ? AND answered_at IS NULL AND aspect_name IS NOT NULL",
        (sku,),
    ).fetchall():
        if question["aspect_name"] not in known:
            continue
        value = ", ".join(str(v) for v in aspects[question["aspect_name"]])
        conn.execute(
            "UPDATE open_question SET answer = ?, answered_at = ? WHERE id = ?",
            (value, now_iso(), question["id"]),
        )
        settled.append(question["id"])
    if settled:
        conn.commit()
    return settled


def _populated_aspects(conn: sqlite3.Connection, sku: str) -> set[str]:
    """Aspect names the live identification has a usable value for."""
    import json as _json

    row = conn.execute(
        "SELECT aspects FROM identification WHERE sku = ? AND superseded_at IS NULL",
        (sku,),
    ).fetchone()
    if row is None or not row["aspects"]:
        return set()
    try:
        aspects = _json.loads(row["aspects"])
    except (TypeError, ValueError):
        return set()
    return {
        name for name, values in aspects.items()
        if values and any(str(v).strip() for v in values)
    }


def unresolved_blocking_questions(conn: sqlite3.Connection, sku: str) -> list[sqlite3.Row]:
    """Blocking questions still genuinely outstanding.

    A question is a request for something missing, so one about an aspect the
    item now has a value for is met -- whoever met it. Filtered on read rather
    than settled on write, because the callers that matter (`next_step`, the
    inventory table, the card) must not write, and because the value can arrive
    from any direction: an answer, a mapping run, a correction.

    MP-000016 is why. Publish computes what is missing from the *frozen* proposal,
    so an aspect supplied afterwards looked missing for ever: every answer was
    followed by a fresh pair of questions, and the item collected twelve.
    """
    rows = conn.execute(
        "SELECT * FROM open_question WHERE sku = ? AND blocking = 1 "
        "AND answered_at IS NULL ORDER BY asked_at",
        (sku,),
    ).fetchall()
    if not rows:
        return []
    populated = _populated_aspects(conn, sku)
    return [
        row for row in rows
        if not (row["aspect_name"] and row["aspect_name"] in populated)
    ]


def observations_in_scope(conn: sqlite3.Connection, sku: str) -> list[sqlite3.Row]:
    """Evidence eligible for citation during mapping.

    Model observations are scoped to the most recent completed run: two runs of the
    same photos produce near-duplicate claims, and citing one of two near-identical
    rows is arbitrary. Earlier runs stay in the database as append-only evidence for
    audit and cross-provider evaluation -- they are simply not citable.

    Operator evidence belongs to no run and is always in scope. It is also the
    strongest source available, so excluding it would be perverse.
    """
    latest = conn.execute(
        "SELECT MAX(id) FROM model_call WHERE sku = ? AND purpose = 'observe' "
        "AND status = 'completed'",
        (sku,),
    ).fetchone()[0]

    # subject='this_item' only. Candidate-product facts reach an aspect through the
    # donation gate or not at all; letting them in here would route around it.
    return list(
        conn.execute(
            "SELECT * FROM evidence WHERE sku = ? AND send_to_model = 1 "
            "AND subject = 'this_item' "
            "AND (model_call_id IS NULL OR model_call_id = ?) ORDER BY id",
            (sku, latest),
        ).fetchall()
    )


def candidate_evidence(
    conn: sqlite3.Connection, sku: str, candidate_ref: str | None = None
) -> list[sqlite3.Row]:
    """Facts about candidate products. Never about the item."""
    if candidate_ref:
        return list(conn.execute(
            "SELECT * FROM evidence WHERE sku = ? AND subject = 'candidate_product' "
            "AND candidate_ref = ? ORDER BY id", (sku, candidate_ref),
        ).fetchall())
    return list(conn.execute(
        "SELECT * FROM evidence WHERE sku = ? AND subject = 'candidate_product' "
        "ORDER BY id", (sku,),
    ).fetchall())


def citable_candidate_evidence(conn: sqlite3.Connection, sku: str) -> dict[int, str]:
    """Candidate evidence ids an aspect may cite, mapped to what they permit.

    The donation gate as a query. An aspect value citing candidate evidence absent
    from this mapping is dropped exactly as an invented citation is -- the rule is
    enforced the same way, not by a different mechanism that could disagree.
    """
    permitted: dict[int, str] = {}
    for match in conn.execute(
        "SELECT candidate_ref, donation_scope FROM product_match "
        "WHERE sku = ? AND is_match = 1 AND donation_scope IS NOT NULL "
        "AND donation_scope != 'none'",
        (sku,),
    ):
        for row in candidate_evidence(conn, sku, match["candidate_ref"]):
            # Identity facts only. Retail facts belong to pricing, under different
            # rules and possibly a different licence.
            if row["fact_domain"] in (None, "identity"):
                permitted[row["id"]] = match["donation_scope"]
    return permitted


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
        # Provenance for model-produced evidence. Overridable per call, because a
        # cross-provider comparison needs each observation attributable to the
        # provider and model that actually made it.
        model_source: str = "model",
    ):
        self.conn = conn
        self.model_source = model_source
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
        # Every caller is already blocked on terminal states, so this is belt and
        # braces -- but the approval that authorised a live listing is the record of
        # what was published, and no future code path should be able to erase it.
        if current_state(self.conn, sku) in TERMINAL_STATES:
            log_event(
                self.conn,
                "approval.void_refused",
                {"reason": reason, "why": "item is terminal; approval is a historical record"},
                item_id=sku,
            )
            return 0

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
        identification_effort: str = str(IdentificationEffort.STANDARD),
        owner_email: str | None = None,
    ) -> Accepted:
        """Allocate a SKU and create the item. The only command without one.

        `owner_email` is whose consumer shelf this belongs on during the private
        beta -- a label, not an account. Absent for CLI intake and for the
        operator's own form, which is why it is optional and why NULL appears on
        nobody's shelf rather than everybody's.
        """
        if acquisition_intent not in ("resale", "declutter", "unknown"):
            raise Rejected("IngestItem", [f"unknown acquisition_intent {acquisition_intent!r}"])
        if purchase_cost_cents is not None and purchase_cost_cents < 0:
            raise Rejected("IngestItem", ["purchase cost cannot be negative"])
        try:
            IdentificationEffort(identification_effort)
        except ValueError as exc:
            raise Rejected(
                "IngestItem", [f"unknown identification_effort {identification_effort!r}"]
            ) from exc

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
                "state_changed_at, identification_effort, owner_email) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sku, seq, str(ItemState.INTAKE), purchase_cost_cents,
                    acquisition_intent, acquired_on, notes, stamp, stamp, stamp,
                    identification_effort, (owner_email or "").casefold() or None,
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

    def _require_not_terminal(self, sku: str, command: str, what: str) -> None:
        """Refuse authoritative mutations once the item is terminal.

        A published listing lives on eBay; changing the local record of what the
        item *is* would silently desync the two, and listing revision is not
        implemented. Worse, voiding the approval afterwards destroys the record of
        what was actually agreed and published -- at that point the approval is
        historical evidence, not a pending permission.

        Note what is deliberately still allowed: record_evidence. Observations are
        append-only and non-authoritative, so a fact learned about a listed item has
        somewhere to go. It is promoted into a new identification only once a
        revision workflow exists to carry it to eBay.
        """
        state = current_state(self.conn, sku)
        if state in TERMINAL_STATES:
            raise Rejected(
                command,
                [
                    f"item is {state}; {what} cannot change.",
                    "Revising a published listing is not implemented, and changing "
                    "this would void the approval recording what was published.",
                    "New facts can still be recorded as evidence.",
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
        self._require_not_terminal(sku, "AttachPhoto", "the photo set")
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
        self._require_not_terminal(sku, "RemovePhoto", "the photo set")

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

    def record_observation(
        self, sku: str, observation, *, source: str | None = None,
        model_call_id: int | None = None,
    ) -> Accepted:
        """Record one model observation as evidence.

        The observation validates itself first: a text_read without a photo
        citation, or a measurement without a method, is refused. Those are the
        properties that let an operator check the claim later, so an observation
        lacking them is not evidence, it is an assertion.
        """
        problems = observation.problems()
        if problems:
            raise Rejected("RecordObservation", problems)
        get_item(self.conn, sku)

        cursor = self.conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, confidence, "
            "send_to_model, recorded_at, basis, subject, model_call_id) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                sku,
                "vision_observation",
                source or self.model_source,
                json.dumps(
                    {
                        "claim": observation.claim,
                        "photo_positions": list(observation.photo_positions),
                        "surface": observation.surface,
                        "measurement_method": (
                            str(observation.measurement_method)
                            if observation.measurement_method else None
                        ),
                    }
                ),
                observation.confidence,
                now_iso(),
                str(observation.basis),
                str(observation.subject),
                model_call_id,
            ),
        )
        return Accepted(
            "RecordObservation", sku, None, None,
            f"{observation.basis}: {observation.claim[:70]}",
            {"evidence_id": cursor.lastrowid},
        )

    def record_identifier(
        self, sku: str, identifier, *, source: str | None = None,
        model_call_id: int | None = None, basis: str | None = None,
    ) -> Accepted:
        """Record a product identifier read off the object.

        A failed check digit refuses the record rather than storing it. The attempt
        is logged to events so the audit trail keeps what was read, but evidence
        stays clean -- a transcription that is arithmetically impossible should not
        be citable, and a suggested correction is more useful than a stored wrong
        number.
        """
        get_item(self.conn, sku)
        if not identifier.usable:
            log_event(
                self.conn,
                "identifier.rejected",
                {
                    "scheme": str(identifier.scheme),
                    "raw": identifier.raw_transcription,
                    "why": identifier.check_explanation,
                },
                item_id=sku,
            )
            raise Rejected("RecordIdentifier", [identifier.check_explanation])

        cursor = self.conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
            "recorded_at, basis, subject, model_call_id) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                sku,
                "identifier_observation",
                source or self.model_source,
                json.dumps(
                    {
                        "scheme": str(identifier.scheme),
                        "raw_transcription": identifier.raw_transcription,
                        "normalized": identifier.normalized,
                        "photo_position": identifier.photo_position,
                        "surface": identifier.surface,
                        "check_digit_valid": identifier.check_digit_valid,
                        "check_explanation": identifier.check_explanation,
                    }
                ),
                now_iso(),
                basis or str(Basis.TEXT_READ),
                str(Subject.THIS_ITEM),
                model_call_id,
            ),
        )
        return Accepted(
            "RecordIdentifier", sku, None, None,
            f"{identifier.scheme}={identifier.normalized} ({identifier.check_explanation})",
            {"evidence_id": cursor.lastrowid},
        )

    def request_effort_escalation(
        self, sku: str, *, to_effort: str, rationale: str, evidence_ids: tuple[int, ...]
    ) -> Accepted:
        """The model asks for more identification budget. It cannot grant its own.

        A one-step rise from minimal is auto-granted when cited, because routing
        that through a human would waste the human. Anything reaching `thorough`
        costs the operator time and photographs, so it waits for them.
        """
        item = get_item(self.conn, sku)
        current = IdentificationEffort(item["identification_effort"])
        try:
            requested = IdentificationEffort(to_effort)
        except ValueError as exc:
            raise Rejected("RequestEffortEscalation", [f"unknown effort {to_effort!r}"]) from exc
        if not rationale.strip():
            raise Rejected("RequestEffortEscalation", ["a rationale is required"])

        for evidence_id in evidence_ids:
            row = self.conn.execute(
                "SELECT 1 FROM evidence WHERE id = ? AND sku = ?", (evidence_id, sku)
            ).fetchone()
            if row is None:
                raise Rejected(
                    "RequestEffortEscalation",
                    [f"cited evidence {evidence_id} does not belong to {sku}"],
                )

        decision, why = escalation_policy(current, requested, cited_evidence=tuple(evidence_ids))
        if decision is EscalationDecision.REFUSE:
            raise Rejected("RequestEffortEscalation", [why])

        cursor = self.conn.execute(
            "INSERT INTO effort_escalation (sku, scope, from_effort, to_effort, "
            "rationale, evidence_ids, requested_at) VALUES (?, 'identity', ?, ?, ?, ?, ?)",
            (sku, str(current), str(requested), rationale,
             json.dumps(sorted(evidence_ids)), now_iso()),
        )
        request_id = cursor.lastrowid

        if decision is EscalationDecision.AUTO_GRANT:
            self._apply_escalation(sku, request_id, requested, decided_by="policy")
            return Accepted(
                "RequestEffortEscalation", sku, None, None,
                f"granted by policy: {current} -> {requested}. {why}",
                {"request_id": request_id, "granted": True},
            )

        log_event(
            self.conn,
            "effort.escalation_requested",
            {"request_id": request_id, "from": str(current), "to": str(requested),
             "rationale": rationale[:200]},
            item_id=sku,
        )
        return Accepted(
            "RequestEffortEscalation", sku, None, None,
            f"awaiting operator: {current} -> {requested}. {why}",
            {"request_id": request_id, "granted": False},
        )

    def decide_effort_escalation(
        self, request_id: int, *, granted: bool, operator: bool = False
    ) -> Accepted:
        """Operator-only. The budget holder decides."""
        if not operator:
            raise Rejected(
                "DecideEffortEscalation",
                ["only the operator may grant additional identification budget"],
            )
        row = self.conn.execute(
            "SELECT * FROM effort_escalation WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise Rejected("DecideEffortEscalation", [f"no request {request_id}"])
        if row["decision"]:
            raise Rejected(
                "DecideEffortEscalation",
                [f"request {request_id} was already {row['decision']}"],
            )

        if not granted:
            self.conn.execute(
                "UPDATE effort_escalation SET decision = 'denied', decided_by = 'operator', "
                "decided_at = ? WHERE id = ?",
                (now_iso(), request_id),
            )
            return Accepted(
                "DecideEffortEscalation", row["sku"], None, None,
                f"denied; effort stays at {row['from_effort']}",
            )

        self._apply_escalation(
            row["sku"], request_id, IdentificationEffort(row["to_effort"]), decided_by="operator"
        )
        return Accepted(
            "DecideEffortEscalation", row["sku"], None, None,
            f"granted; effort is now {row['to_effort']}",
        )

    def _apply_escalation(
        self, sku: str, request_id: int, to_effort, *, decided_by: str
    ) -> None:
        self.conn.execute(
            "UPDATE effort_escalation SET decision = 'granted', decided_by = ?, "
            "decided_at = ? WHERE id = ?",
            (decided_by, now_iso(), request_id),
        )
        self.conn.execute(
            "UPDATE item SET identification_effort = ?, updated_at = ? WHERE sku = ?",
            (str(to_effort), now_iso(), sku),
        )
        log_event(
            self.conn,
            "effort.escalated",
            {"request_id": request_id, "to": str(to_effort), "decided_by": decided_by},
            item_id=sku,
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
        basis: str | None = None,
        subject: str = str(Subject.THIS_ITEM),
    ) -> Accepted:
        """Append-only. Provenance is mandatory; the database enforces immutability.

        `basis` matters more than it looks: resolution treats an operator statement
        as adjudicating a contradiction, and it reads the basis back from storage
        rather than trusting whatever a model asserted. Evidence written without one
        falls back to `inference` and silently loses that authority.
        """
        get_item(self.conn, sku)
        if not source:
            raise Rejected("RecordEvidence", ["source (provenance) is required"])
        cursor = self.conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, confidence, "
            "send_to_model, recorded_at, basis, subject) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sku, kind, source, json.dumps(payload), confidence,
             1 if send_to_model else 0, now_iso(), basis, subject),
        )
        return Accepted(
            "RecordEvidence", sku, None, None, f"{kind} from {source}",
            {"evidence_id": cursor.lastrowid},
        )

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
        category_path: str | None = None,
        aspect_schema: dict | None = None,
        aspects: dict | None = None,
        confidence: float | None = None,
        reasoning: str | None = None,
        mode: str | None = None,
        mode_rationale: str | None = None,
        identity_resolution: str | None = None,
    ) -> Accepted:
        """Supersede the previous belief rather than editing it.

        confidence is stored for diagnostics and evaluation. It is never consulted
        by any precondition -- a number the model chooses should not decide whether
        real money moves.
        """
        get_item(self.conn, sku)
        self._require_not_terminal(sku, "ProposeIdentification", "the identification")
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
                "description, condition_id, category_id, category_path, aspect_schema, "
                "aspects, confidence, reasoning, mode, mode_rationale, "
                "identity_resolution, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sku, version, brand, model, variant, title, description, condition_id,
                    category_id, category_path,
                    json.dumps(aspect_schema) if aspect_schema else None,
                    json.dumps(aspects) if aspects else None, confidence, reasoning,
                    # Both columns are NOT NULL with a default, and passing None
                    # would insert NULL rather than fall back to it. The defaults
                    # are spelled out here so a caller that says nothing gets the
                    # same value the schema would have given it.
                    mode or "unresolved", mode_rationale,
                    identity_resolution or "unattempted",
                    now_iso(),
                ),
            )
        self._void_approvals(sku, "identification revised")
        return Accepted("ProposeIdentification", sku, None, None, f"version {version}")

    def record_candidate_facts(
        self, sku: str, *, candidate_ref: str, source_url: str, authority: str,
        facts: list[tuple[str, str] | tuple[str, str, str | None]],
        restriction: str | None = None,
        title: str = "", retrieval_method: str = "automated_fetch",
    ) -> list[int]:
        """Record facts about a candidate product. Never about this item.

        subject is fixed to candidate_product here rather than passed in, because a
        retrieval path that could write this_item evidence would bypass the entire
        donation gate.

        Each fact is `(claim, domain)` or `(claim, domain, excerpt)`. The excerpt is
        the source text the claim was extracted from, and it is what makes an
        automated extraction checkable: the operator who transcribes a page is the
        witness to what it said, and a model reading fetched HTML is not. A
        transcription passes no excerpt, and that absence is the honest record.
        """
        get_item(self.conn, sku)
        ids = []
        for fact in facts:
            claim, domain = fact[0], fact[1]
            excerpt = fact[2] if len(fact) > 2 else None
            cursor = self.conn.execute(
                "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
                "recorded_at, basis, subject, candidate_ref, fact_domain, "
                "source_authority, source_url, retrieved_at, source_restriction, "
                "retrieval_method, source_excerpt) "
                "VALUES (?, 'candidate_product_fact', ?, ?, ?, ?, ?, "
                "'candidate_product', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sku,
                    # The provenance the system can vouch for: who supplied it.
                    "operator" if retrieval_method == "operator_transcribed" else (source_url or "research"),
                    json.dumps({"claim": claim, "title": title}),
                    0 if restriction else 1,
                    now_iso(), str(Basis.EXTERNAL_SOURCE), candidate_ref, domain,
                    authority, source_url, now_iso(), restriction, retrieval_method,
                    excerpt or None,
                ),
            )
            ids.append(cursor.lastrowid)
        log_event(
            self.conn, "research.candidate_recorded",
            {"candidate_ref": candidate_ref, "url": source_url, "authority": authority,
             "facts": len(ids), "restricted": bool(restriction),
             "retrieval_method": retrieval_method},
            item_id=sku,
        )
        return ids

    def record_product_match(self, sku: str, claim, *, authority: str, donation_scope: str) -> int:
        """Store a match or non-match claim with the permission it carries.

        donation_scope is computed from identifier strength, source authority and the
        cited evidence -- never from how confident the claim sounded. A model that
        writes a persuasive rationale has not thereby earned the right to attach a
        web page's attributes to a physical object.
        """
        get_item(self.conn, sku)
        cursor = self.conn.execute(
            "INSERT INTO product_match (sku, candidate_ref, strength, source_authority, "
            "rationale, item_evidence, candidate_evidence, is_match, donation_scope, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sku, claim.candidate_ref, str(claim.strength), authority, claim.rationale,
                json.dumps(sorted(claim.item_evidence)),
                json.dumps(sorted(claim.candidate_evidence)),
                1 if claim.is_match else 0, donation_scope, now_iso(),
            ),
        )
        return cursor.lastrowid

    def record_lookup(
        self, sku: str, *, provider: str, query: str, motivation: str,
        evidence_ids: list[int], result_count: int, scope: str = "identity",
        cost_micros: int | None = None,
    ) -> None:
        """What was searched, and what it cost.

        `cost_micros` is None for a retrieval that was genuinely free -- an
        operator pasting a URL -- and zero is not the same answer.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO research_lookup (sku, scope, provider, query, "
            "motivation, evidence_ids, result_count, performed_at, cost_micros) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sku, scope, provider, query, motivation, json.dumps(evidence_ids),
             result_count, now_iso(), cost_micros),
        )

    def record_research_negative(self, sku: str, *, summary: str, detail: dict) -> Accepted:
        """A recorded absence: searched, found nothing.

        The mirror of a negative observation. An item whose identifiers were looked
        up and matched nothing is in a different position from one nobody
        researched, and only a record tells them apart.
        """
        return self.record_evidence(
            sku, kind="research_negative", source="research",
            payload={"summary": summary, **detail},
            basis=str(Basis.EXTERNAL_SOURCE), subject=str(Subject.THIS_ITEM),
        )

    def _upsert_aspect_candidate(
        self, identification_id: int, aspect_name: str, value: str
    ) -> int:
        """The candidate's id, inserting it first if it is not already there.

        The id is always read back with a SELECT, and that is the whole point of
        this existing. `cursor.lastrowid` after an `INSERT OR IGNORE` that ignored
        is not zero and not None -- it is the rowid of whatever was last inserted,
        on any table. Two call sites relied on `lastrowid or SELECT ...`, so the
        fallback never ran and a stale id went into the foreign key.

        How it showed up: answering a duplicated question about Model, where the
        first answer had already created the candidate. The preceding statement was
        the evidence insert, so `candidate_id` became an evidence id. It was larger
        than any aspect_candidate id, so the foreign key rejected it and
        `answer_question` raised -- after its UPDATE had committed, leaving the
        question answered and the item stuck in needs_info with nothing outstanding.

        The crash was the good outcome. Had the evidence id happened to be a real
        aspect_candidate id, the citation would have been filed silently against
        somebody else's candidate, and "this value is supported" would have been a
        lie the database was happy with.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO aspect_candidate "
            "(identification_id, aspect_name, value, created_at) VALUES (?, ?, ?, ?)",
            (identification_id, aspect_name, value, now_iso()),
        )
        return self.conn.execute(
            "SELECT id FROM aspect_candidate WHERE identification_id = ? "
            "AND aspect_name = ? AND value = ?",
            (identification_id, aspect_name, value),
        ).fetchone()[0]

    def record_aspect_candidates(self, sku: str, identification_id: int, outcomes) -> int:
        """Store candidate sets with their citations.

        The foreign key to evidence is the point: a citation to a record that does
        not exist fails in the database rather than in application code, so "this
        value is supported" is checkable rather than asserted.
        """
        get_item(self.conn, sku)
        stored = 0
        with transaction(self.conn):
            for outcome in outcomes:
                for candidate in outcome.candidates:
                    if not candidate.support:
                        continue
                    candidate_id = self._upsert_aspect_candidate(
                        identification_id, outcome.aspect_name, candidate.value
                    )
                    for ref in candidate.support:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO aspect_candidate_evidence "
                            "(candidate_id, evidence_id) VALUES (?, ?)",
                            (candidate_id, ref.evidence_id),
                        )
                    stored += 1
        return stored

    def ask_operator(
        self, sku: str, *, question: str, why_it_matters: str = "",
        blocking: bool = True, aspect_name: str | None = None,
        allowed_values: tuple[str, ...] | None = None,
    ) -> Accepted:
        """The operator-as-tool call. A blocking question moves the item to needs_info.

        `allowed_values` is eBay's list for the aspect, captured now so the answer
        can be checked later without a Taxonomy call. Empty for FREE_TEXT aspects
        and for anything that is not about an aspect at all.

        Asking something already outstanding is a no-op. `map-aspects --apply` opens
        one question per unresolved required aspect, and it is meant to be re-run --
        so without this, three runs left three identical questions about Model, and
        answering one of them left the item in needs_info behind the other two. An
        unanswered question is a request that has not been met yet; asking again
        does not make it more true.
        """
        if not question.strip():
            raise Rejected("AskOperator", ["question is empty"])
        get_item(self.conn, sku)

        existing = self._outstanding_question(sku, aspect_name, question)
        if existing is not None:
            state = current_state(self.conn, sku)
            return Accepted(
                "AskOperator", sku, state, state,
                f"already open as question {existing}",
                {"question_id": existing, "already_open": True},
            )

        self.conn.execute(
            "INSERT INTO open_question (sku, question, why_it_matters, blocking, "
            "asked_at, aspect_name, allowed_values_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sku, question, why_it_matters, 1 if blocking else 0, now_iso(), aspect_name,
             json.dumps(list(allowed_values)) if allowed_values else None),
        )
        state = current_state(self.conn, sku)
        if blocking and state == ItemState.IDENTIFYING:
            return self._transition(
                sku, ItemState.NEEDS_INFO, command="AskOperator", detail=question[:120]
            )
        return Accepted("AskOperator", sku, state, state, question[:120])

    def _outstanding_question(
        self, sku: str, aspect_name: str | None, question: str
    ) -> int | None:
        """An unanswered question already asking this, or None.

        Keyed on the aspect where there is one, because that is what the question
        is *about*: two runs of the mapper word the same gap identically today, but
        a reworded prompt asking again about Model is still the same request, and
        matching on text would let it through.

        Without an aspect there is nothing to key on but the text, so that is what
        is compared -- exactly, not loosely. A free-form question the operator
        deliberately asked twice in different words is not something to collapse.
        """
        if aspect_name:
            row = self.conn.execute(
                "SELECT id FROM open_question WHERE sku = ? AND aspect_name = ? "
                "AND answered_at IS NULL ORDER BY id LIMIT 1",
                (sku, aspect_name),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM open_question WHERE sku = ? AND aspect_name IS NULL "
                "AND question = ? AND answered_at IS NULL ORDER BY id LIMIT 1",
                (sku, question),
            ).fetchone()
        return row["id"] if row else None

    def resume_identification(self, sku: str) -> Accepted:
        """needs_info -> identifying, once nothing blocking is outstanding.

        `answer_question` returns the item on its own when the last blocker is
        cleared, but an item can also reach needs_info and have its questions
        answered out of band. This is the way back that does not depend on which
        command happened to clear the final one.
        """
        outstanding = unresolved_blocking_questions(self.conn, sku)
        if outstanding:
            raise Rejected(
                "ResumeIdentification",
                [f"blocking question unanswered: {q['question'][:80]}"
                 for q in outstanding],
            )
        return self._transition(
            sku, ItemState.IDENTIFYING, command="ResumeIdentification",
            detail="all blocking questions answered",
        )

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

        # The pricing layer owns price. Before this check, `item propose
        # --price-cents` and `price approve` were two independent authorities over
        # the same number and nothing reconciled them -- whichever the publisher
        # read is what reached eBay, and the other was decoration.
        #
        # Imported here rather than at module scope to keep the gateway's import
        # graph shallow; the pricing layer is a peer, not a dependency of the
        # state machine itself.
        from resell.store_pricing import approved_price_cents

        approved = approved_price_cents(self.conn, sku)
        if approved is None:
            reasons.append(
                "no approved price for this item; price it first with "
                "`resell price recommend`, `resell price propose --objective ...` "
                "and `resell price approve`"
            )
        elif proposal.price_cents != approved:
            reasons.append(
                f"price {proposal.price_cents} does not match the approved price "
                f"{approved}; approve a new price rather than typing a different one"
            )

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
        """Back to pricing from proposed / approved / publish_failed. Voids approvals.

        Wrapped in a transaction so a rejected transition rolls the side effects
        back. Previously the approvals were voided first and the legality check ran
        second, which meant a refused command still destroyed state -- a rejected
        command must leave nothing behind.
        """
        with transaction(self.conn):
            self._void_approvals(sku, reason)
            return self._transition(
                sku, ItemState.PRICING, command="Revise", detail=reason
            )

    def abandon(self, sku: str, reason: str) -> Accepted:
        if not reason.strip():
            raise Rejected("Abandon", ["a reason is required"])
        with transaction(self.conn):
            self._void_approvals(sku, "item abandoned")
            self.conn.execute(
                "UPDATE listing SET active = 0, updated_at = ? WHERE sku = ? AND active = 1",
                (now_iso(), sku),
            )
            return self._transition(
                sku, ItemState.ABANDONED, command="Abandon", detail=reason
            )

    def restore(self, sku: str, reason: str = "") -> Accepted:
        """Bring an abandoned item back to where it was, from the event log.

        The target is read out of history rather than supplied, which is what keeps
        this from being an arbitrary jump: `_transition` records every state change
        with its `from`, so the state an item was in when it was abandoned is a
        fact the database already holds. A caller cannot use restore to move an
        item somewhere it never was.

        Nothing is undone. Abandoning voided approvals and deactivated listings,
        and both stay that way -- an item restored to `proposed` needs approving
        again, which is correct, because the approval that existed was voided for a
        stated reason and is part of the record.
        """
        item = get_item(self.conn, sku)
        state = ItemState(item["state"])
        if state is not ItemState.ABANDONED:
            raise Rejected("Restore", [f"item is {state}, not abandoned"])

        previous = state_before_abandonment(self.conn, sku)
        if previous is None:
            raise Rejected(
                "Restore",
                ["no recorded state to return to; the abandonment predates the "
                 "event log or was never recorded"],
            )
        with transaction(self.conn):
            return self._transition(
                sku, previous, command="Restore",
                detail=reason or f"restored to {previous}",
            )

    # --- operator-only commands ---------------------------------------------

    def answer_question(
        self, question_id: int, answer: str, *, operator: bool = False,
        value_not_listed: bool = False,
    ) -> Accepted:
        """Operator-only. Answering own questions would defeat the whole loop.

        When the question carries eBay's allowed values, an answer outside them is
        refused. `value_not_listed` overrides that -- eBay does not guarantee its
        value list is exhaustive -- and the override is written into the evidence
        payload along with the list as it stood, so a disagreement with eBay is an
        operator decision on the record rather than an invisible exception.

        An answer about an aspect also becomes an aspect_candidate citing this
        evidence row, which is what lets resolution treat it as an adjudication
        instead of filing it and carrying on.
        """
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
        value = answer.strip()

        keys = row.keys()
        allowed = (
            json.loads(row["allowed_values_json"])
            if "allowed_values_json" in keys and row["allowed_values_json"]
            else []
        )
        from resell.domain import values_not_in_allowed

        unlisted = values_not_in_allowed(allowed, [value]) if allowed else []
        if unlisted and not value_not_listed:
            shown = ", ".join(allowed[:12])
            more = "" if len(allowed) <= 12 else f", and {len(allowed) - 12} more"
            raise Rejected("AnswerQuestion", [
                f"{value!r} is not one of the values eBay lists for "
                f"{row['aspect_name']}",
                f"eBay accepts: {shown}{more}",
                "if that list is incomplete, pass value_not_listed to record the "
                "answer as an explicit operator override",
            ])

        self.conn.execute(
            "UPDATE open_question SET answer = ?, answered_at = ? WHERE id = ?",
            (answer, now_iso(), question_id),
        )

        # Duplicates opened before the check in `ask_operator` existed, or opened
        # under a different wording, are settled by the same statement. One operator
        # answer about Model answers every outstanding request for Model: leaving
        # the siblings open would hold the item in needs_info on a question that has
        # in fact been answered, which is the trap this pairs with preventing.
        #
        # Note what this is not: the model closing its own question. The answer being
        # propagated is the operator's, and only to requests about the same aspect.
        siblings = self._settle_duplicate_questions(sku, row["aspect_name"], question_id, answer)

        payload = {"question": row["question"], "answer": answer}
        if unlisted:
            # The override is the record, not the permission.
            payload["value_not_listed"] = True
            payload["allowed_values_at_answer"] = allowed
        # basis='operator' is what lets this answer adjudicate a contradiction later.
        cursor = self.conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
            "recorded_at, basis, subject) "
            "VALUES (?, 'operator_answer', 'operator', ?, 1, ?, ?, ?)",
            (sku, json.dumps(payload), now_iso(),
             str(Basis.OPERATOR), str(Subject.THIS_ITEM)),
        )
        evidence_id = cursor.lastrowid

        aspect_name = row["aspect_name"] if "aspect_name" in keys else None
        if aspect_name:
            identification = current_identification(self.conn, sku)
            if identification is not None:
                # Applied first, then cited. The candidate belongs to whichever
                # identification version ends up holding the value -- writing it
                # against the old one and then superseding that version leaves the
                # citation on a version nothing reads, and a second question about
                # the same aspect lands on a different row instead of adding its
                # citation to the first.
                self._apply_operator_aspect(sku, identification, aspect_name, value)
                identification = current_identification(self.conn, sku)
                candidate_id = self._upsert_aspect_candidate(
                    identification["id"], aspect_name, value
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO aspect_candidate_evidence "
                    "(candidate_id, evidence_id) VALUES (?, ?)",
                    (candidate_id, evidence_id),
                )

        also = f" (and {len(siblings)} duplicate: {siblings})" if siblings else ""
        state = current_state(self.conn, sku)
        if state == ItemState.NEEDS_INFO and not unresolved_blocking_questions(self.conn, sku):
            accepted = self._transition(
                sku, ItemState.IDENTIFYING, command="AnswerQuestion",
                detail=f"all blocking questions answered{also}",
            )
        else:
            accepted = Accepted(
                "AnswerQuestion", sku, state, state,
                f"question {question_id} answered{also}",
            )
        # Set on both paths. Settling the last duplicate is exactly the case that
        # releases the item, so the transition path is the one that most needs to
        # report what it closed.
        if siblings:
            accepted.data["duplicates_settled"] = siblings
        return accepted

    def _apply_operator_aspect(
        self, sku: str, identification, aspect_name: str, value: str
    ) -> None:
        """Put the operator's answer onto the identification, if it changes it.

        A candidate was recorded and nothing promoted it, so answering changed
        nothing anyone could see: publishing still refused for want of the aspect
        just supplied, and the next mapping run asked again. MP-000016 answered
        the same two questions three times, and by then the item had been priced
        and approved -- past the point where any mapping run would ever happen, so
        waiting for one was waiting for nothing.

        The operator is the authority. `basis='operator'` on the evidence row
        above is what adjudicates a contradiction, so the answer does not need a
        model to ratify it.

        Only when it changes something. Restating a value already recorded must
        not mint an identification version -- versions are how this item's history
        reads, and one per keystroke would bury it. That also keeps two questions
        about one aspect converging on one candidate, which is what carries the
        citations.
        """
        import json as _json

        try:
            current = _json.loads(identification["aspects"] or "{}")
        except (TypeError, ValueError):
            current = {}
        if [str(v) for v in current.get(aspect_name, [])] == [value]:
            return

        from resell.cli_item import merged_identification

        fields, _ = merged_identification(
            self.conn, sku, aspects={aspect_name: [value]}
        )
        try:
            self.propose_identification(sku, **fields)
        except Rejected:
            # A terminal item cannot take a new identification. The answer stays
            # recorded as evidence, which is still true and still the record.
            return

    def _settle_duplicate_questions(
        self, sku: str, aspect_name: str | None, answered_id: int, answer: str
    ) -> list[int]:
        """Close the other outstanding questions about the same aspect.

        Returns the ids closed, so the caller can say so rather than have questions
        disappear silently. The recorded answer names where it came from: the
        operator answered one request, and this is the record of it settling the
        others, not a second independent statement.
        """
        if not aspect_name:
            return []
        rows = self.conn.execute(
            "SELECT id FROM open_question WHERE sku = ? AND aspect_name = ? "
            "AND answered_at IS NULL AND id != ? ORDER BY id",
            (sku, aspect_name, answered_id),
        ).fetchall()
        if not rows:
            return []
        stamp = now_iso()
        settled = [row["id"] for row in rows]
        self.conn.executemany(
            "UPDATE open_question SET answer = ?, answered_at = ? WHERE id = ?",
            [(f"{answer}  [settled by the answer to question {answered_id}]",
              stamp, question_id) for question_id in settled],
        )
        return settled

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


def state_before_abandonment(conn: sqlite3.Connection, sku: str) -> ItemState | None:
    """The state this item was in when it was last abandoned.

    Read from `item.state_changed` events, which `_transition` has always written
    with both ends of the move. Nothing new is stored to make restore possible --
    the history was already sufficient, it was simply never read.

    The *last* abandonment, not the first: an item can be abandoned, restored and
    abandoned again, and the relevant question is where it came from this time.
    """
    row = conn.execute(
        "SELECT payload FROM events WHERE item_id = ? AND kind = 'item.state_changed' "
        "ORDER BY id DESC", (sku,),
    )
    for record in row:
        try:
            payload = json.loads(record["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("to") != str(ItemState.ABANDONED):
            continue
        origin = payload.get("from")
        try:
            state = ItemState(origin)
        except ValueError:
            return None
        return state if is_legal_transition(ItemState.ABANDONED, state) else None
    return None
