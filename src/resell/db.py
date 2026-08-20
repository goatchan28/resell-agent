"""SQLite storage: schema migrations, plus the append-only event log.

The event log exists from day one on purpose. Every token mint, every HTTP call,
every state transition lands here. It costs almost nothing now and becomes the
audit trail, the debugger, and eventually the eval dataset.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Each migration is applied once, in order, tracked via PRAGMA user_version.
#
# Individual statements rather than one script per migration: Connection.
# executescript() issues an implicit COMMIT before it runs, which would silently
# end the surrounding transaction and leave a half-applied migration if a later
# statement failed.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # 1 -- credentials and the audit log
    (
        """
        CREATE TABLE oauth_tokens (
            environment              TEXT NOT NULL,
            kind                     TEXT NOT NULL CHECK (kind IN ('user', 'application')),
            access_token             TEXT,
            access_token_expires_at  TEXT,
            refresh_token            TEXT,
            refresh_token_expires_at TEXT,
            scopes                   TEXT NOT NULL,
            updated_at               TEXT NOT NULL,
            PRIMARY KEY (environment, kind)
        )
        """,
        """
        CREATE TABLE events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            kind    TEXT NOT NULL,
            item_id TEXT,
            payload TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_events_kind_ts ON events (kind, ts)",
        "CREATE INDEX idx_events_item ON events (item_id) WHERE item_id IS NOT NULL",
        """
        CREATE TABLE kv (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ),
    # 2 -- uploaded images, keyed by content hash so re-uploading the same photo
    # is a lookup rather than an API call
    (
        """
        CREATE TABLE images (
            environment    TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            image_id       TEXT NOT NULL,
            eps_url        TEXT,
            expires_at     TEXT,
            local_path     TEXT NOT NULL,
            size_bytes     INTEGER,
            width          INTEGER,
            height         INTEGER,
            image_format   TEXT,
            uploaded_at    TEXT NOT NULL,
            PRIMARY KEY (environment, content_sha256)
        )
        """,
        "CREATE INDEX idx_images_expires ON images (expires_at)",
    ),
    # 3 -- local_path is the original (the identity); uploaded_path is what eBay
    # actually received, which differs whenever a JPEG derivative was needed.
    ("ALTER TABLE images ADD COLUMN uploaded_path TEXT",),
    # 4 -- the permanent item model. Money is stored in integer cents throughout;
    # floats have no place in a system that computes proceeds.
    (
        # SKU allocation. A dedicated AUTOINCREMENT table rather than item.rowid,
        # because AUTOINCREMENT guarantees a value is never reused even after a
        # delete -- and a reused SKU would collide with eBay's record of the old one.
        """
        CREATE TABLE sku_sequence (
            seq          INTEGER PRIMARY KEY AUTOINCREMENT,
            allocated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE item (
            sku                 TEXT PRIMARY KEY,
            seq                 INTEGER NOT NULL UNIQUE,
            state               TEXT NOT NULL,
            purchase_cost_cents INTEGER,
            acquisition_intent  TEXT NOT NULL DEFAULT 'unknown'
                CHECK (acquisition_intent IN ('resale', 'declutter', 'unknown')),
            acquired_on         TEXT,
            notes               TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            state_changed_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_item_state ON item (state)",
        # Photos belong to the item. The upload cache (images) is keyed by content
        # hash, so a photo joins to its EPS URL only once uploaded -- which now
        # happens at publish time, not at proposal time.
        """
        CREATE TABLE photo (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            sku               TEXT NOT NULL REFERENCES item(sku),
            position          INTEGER NOT NULL,
            source_path       TEXT NOT NULL,
            content_sha256    TEXT NOT NULL,
            image_format      TEXT,
            size_bytes        INTEGER,
            validated_at      TEXT,
            validation_errors TEXT,
            added_at          TEXT NOT NULL,
            UNIQUE (sku, position),
            UNIQUE (sku, content_sha256)
        )
        """,
        # Append-only. Provenance is mandatory; send_to_model gates what may be
        # placed in a prompt.
        """
        CREATE TABLE evidence (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sku           TEXT NOT NULL REFERENCES item(sku),
            kind          TEXT NOT NULL,
            source        TEXT NOT NULL,
            payload       TEXT NOT NULL,
            confidence    REAL,
            send_to_model INTEGER NOT NULL DEFAULT 1,
            recorded_at   TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_evidence_sku ON evidence (sku, recorded_at)",
        """
        CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence
        BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END
        """,
        """
        CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence
        BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END
        """,
        # Beliefs supersede rather than overwrite, so the history of what the model
        # thought and when stays inspectable. confidence is recorded for evaluation
        # and is deliberately NOT a transition gate.
        """
        CREATE TABLE identification (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sku           TEXT NOT NULL REFERENCES item(sku),
            version       INTEGER NOT NULL,
            brand         TEXT,
            model         TEXT,
            variant       TEXT,
            title         TEXT,
            description   TEXT,
            condition_id  TEXT,
            category_id   TEXT,
            aspect_schema TEXT,
            aspects       TEXT,
            confidence    REAL,
            reasoning     TEXT,
            superseded_at TEXT,
            created_at    TEXT NOT NULL,
            UNIQUE (sku, version)
        )
        """,
        # Blocking questions are what make needs_info a real state and the operator
        # a callable tool.
        """
        CREATE TABLE open_question (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            sku            TEXT NOT NULL REFERENCES item(sku),
            question       TEXT NOT NULL,
            why_it_matters TEXT,
            blocking       INTEGER NOT NULL DEFAULT 1,
            asked_at       TEXT NOT NULL,
            answer         TEXT,
            answered_at    TEXT
        )
        """,
        "CREATE INDEX idx_question_open ON open_question (sku) WHERE answered_at IS NULL",
        # One row per (item, marketplace, environment). This is the seam a future
        # Facebook or Mercari adapter plugs into -- item itself never learns about
        # marketplaces. The three eBay identifiers all live here because the spike
        # proved none is derivable from another.
        """
        CREATE TABLE listing (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            sku                   TEXT NOT NULL REFERENCES item(sku),
            marketplace           TEXT NOT NULL,
            environment           TEXT NOT NULL,
            title                 TEXT,
            description           TEXT,
            category_id           TEXT,
            condition_id          TEXT,
            aspects               TEXT,
            price_cents           INTEGER,
            currency              TEXT NOT NULL DEFAULT 'USD',
            shipping_cost_cents   INTEGER NOT NULL DEFAULT 0,
            estimated_fees_cents  INTEGER,
            fulfillment_policy_id TEXT,
            payment_policy_id     TEXT,
            return_policy_id      TEXT,
            merchant_location_key TEXT,
            has_inventory_item    INTEGER NOT NULL DEFAULT 0,
            offer_id              TEXT,
            listing_id            TEXT,
            published_at          TEXT,
            active                INTEGER NOT NULL DEFAULT 1,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX idx_listing_active ON listing (sku, marketplace, environment) WHERE active = 1",
        # Approval binds to CONTENT, not to an item. proposal_hash covers exactly
        # what was shown; if anything changes the approval no longer matches and
        # cannot authorise a publish.
        """
        CREATE TABLE approval (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            sku               TEXT NOT NULL REFERENCES item(sku),
            proposal_hash     TEXT NOT NULL,
            proposal_snapshot TEXT NOT NULL,
            approved_by       TEXT NOT NULL DEFAULT 'operator',
            approved_at       TEXT NOT NULL,
            voided_at         TEXT,
            voided_reason     TEXT
        )
        """,
        "CREATE INDEX idx_approval_sku ON approval (sku, approved_at)",
        """
        CREATE TRIGGER approval_no_delete BEFORE DELETE ON approval
        BEGIN SELECT RAISE(ABORT, 'approvals are append-only; void instead'); END
        """,
        # An approval may only ever be voided. Nothing else about it can change.
        """
        CREATE TRIGGER approval_only_void BEFORE UPDATE ON approval
        WHEN OLD.sku <> NEW.sku
          OR OLD.proposal_hash <> NEW.proposal_hash
          OR OLD.proposal_snapshot <> NEW.proposal_snapshot
          OR OLD.approved_at <> NEW.approved_at
        BEGIN SELECT RAISE(ABORT, 'an approval may only be voided, not edited'); END
        """,
        # Per-item AI cost, retained in full rather than aggregated.
        """
        CREATE TABLE model_call (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sku           TEXT REFERENCES item(sku),
            purpose       TEXT NOT NULL,
            model         TEXT NOT NULL,
            input_tokens  INTEGER,
            output_tokens INTEGER,
            cost_micros   INTEGER,
            latency_ms    INTEGER,
            called_at     TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_model_call_sku ON model_call (sku, called_at)",
    ),
    # 5 -- decouple two assumptions before they harden into architecture.
    #
    # Shipping: the original column was documented as seller-borne, which encoded
    # free shipping into the schema. Splitting it into what the seller bears and
    # what the buyer is charged makes all four arrangements representable, so
    # supporting buyer-paid or calculated shipping later is a caller change rather
    # than a migration.
    #
    # Fees: a floor check backed by a generic estimate is not a guarantee. Recording
    # the basis and the exact rates used means a stored figure can always say where
    # it came from, and production publishing can require a verified basis.
    (
        "ALTER TABLE listing RENAME COLUMN shipping_cost_cents TO seller_shipping_cost_cents",
        "ALTER TABLE listing ADD COLUMN buyer_shipping_charge_cents INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE listing ADD COLUMN shipping_terms TEXT NOT NULL DEFAULT 'seller_paid'",
        "ALTER TABLE listing ADD COLUMN fee_basis TEXT NOT NULL DEFAULT 'provisional_estimate'",
        "ALTER TABLE listing ADD COLUMN fee_rate_used REAL",
        "ALTER TABLE listing ADD COLUMN fee_fixed_cents_used INTEGER",
    ),
    # 6 -- heal items left in `approved` with no live approval.
    #
    # Voiding an approval now reverts the state along with it, so this cannot recur.
    # Databases written before that change can still hold the incoherent state,
    # where the label claims approval that no longer exists. Publishing was already
    # blocked by the entry precondition, so nothing unsafe happened -- but the item
    # could neither publish nor be re-approved, because approve() requires
    # `proposed`. Repairing here beats telling the operator to run `revise`, which
    # would discard a perfectly good proposal and rebuild it.
    (
        """
        UPDATE item
           SET state = 'proposed',
               state_changed_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
               updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
         WHERE state = 'approved'
           AND NOT EXISTS (
                 SELECT 1 FROM approval
                  WHERE approval.sku = item.sku AND approval.voided_at IS NULL)
           AND EXISTS (
                 SELECT 1 FROM listing
                  WHERE listing.sku = item.sku AND listing.active = 1)
        """,
        # With no active listing there is no proposal to re-approve, so `pricing`
        # is the honest resting place.
        """
        UPDATE item
           SET state = 'pricing',
               state_changed_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
               updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
         WHERE state = 'approved'
           AND NOT EXISTS (
                 SELECT 1 FROM approval
                  WHERE approval.sku = item.sku AND approval.voided_at IS NULL)
        """,
        # Every state change writes an event; a migration-driven one is no exception.
        """
        INSERT INTO events (ts, kind, item_id, payload)
        SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
               'item.state_repaired',
               sku,
               json_object('to', state, 'reason',
                           'migration 6: approved with no live approval')
          FROM item
         WHERE state IN ('proposed', 'pricing')
           AND state_changed_at >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-5 seconds')
        """,
    ),
    # 7 -- the reasoning plane's storage.
    (
        # Explicit, not derived from purchase cost. Cost is a poor proxy for how much
        # identification matters: inherited, gifted, decluttered and free items all
        # have a cost basis that says nothing about whether identity is discoverable
        # or worth discovering. Scoped to identity resolution by name, so it does not
        # quietly become the global research budget -- pricing and comps get their
        # own policy later.
        "ALTER TABLE item ADD COLUMN identification_effort TEXT NOT NULL DEFAULT 'standard'",

        # Basis and subject are columns rather than payload keys so they can be
        # queried and constrained. subject is what lets external research join the
        # loop without contaminating it: a catalogue page describes a candidate
        # product, not the object on the table.
        "ALTER TABLE evidence ADD COLUMN basis TEXT",
        "ALTER TABLE evidence ADD COLUMN subject TEXT NOT NULL DEFAULT 'this_item'",
        "CREATE INDEX idx_evidence_basis ON evidence (sku, basis)",

        # Mode is a conclusion, versioned with the identification rather than fixed
        # on the item: an object can begin described_object and become exact_product
        # when a model number turns up on its base.
        "ALTER TABLE identification ADD COLUMN mode TEXT NOT NULL DEFAULT 'unresolved'",
        "ALTER TABLE identification ADD COLUMN mode_rationale TEXT",
        "ALTER TABLE identification ADD COLUMN negative_finding TEXT",
        # For described objects, characterisation replaces naming: dimensions,
        # materials, style, distinguishing features, condition detail, search terms.
        "ALTER TABLE identification ADD COLUMN descriptors TEXT",

        # Candidate sets, not single values. A candidate exists only because evidence
        # supports it, and the foreign key to evidence means a citation to a
        # nonexistent record fails in the database rather than in application code.
        """
        CREATE TABLE aspect_candidate (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            identification_id INTEGER NOT NULL REFERENCES identification(id),
            aspect_name       TEXT NOT NULL,
            value             TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            UNIQUE (identification_id, aspect_name, value)
        )
        """,
        """
        CREATE TABLE aspect_candidate_evidence (
            candidate_id INTEGER NOT NULL REFERENCES aspect_candidate(id),
            evidence_id  INTEGER NOT NULL REFERENCES evidence(id),
            PRIMARY KEY (candidate_id, evidence_id)
        )
        """,
        "CREATE INDEX idx_candidate_aspect ON aspect_candidate (identification_id, aspect_name)",

        # The model may request more identification budget; it may not grant itself
        # any. scope exists so a later pricing budget is additive rather than a
        # reinterpretation of this one.
        """
        CREATE TABLE effort_escalation (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sku           TEXT NOT NULL REFERENCES item(sku),
            scope         TEXT NOT NULL DEFAULT 'identity'
                              CHECK (scope IN ('identity')),
            from_effort   TEXT NOT NULL,
            to_effort     TEXT NOT NULL,
            rationale     TEXT NOT NULL,
            evidence_ids  TEXT NOT NULL,
            requested_at  TEXT NOT NULL,
            decision      TEXT CHECK (decision IN ('granted', 'denied')),
            decided_by    TEXT,
            decided_at    TEXT
        )
        """,
        "CREATE INDEX idx_escalation_pending ON effort_escalation (sku) WHERE decision IS NULL",

        # Full traces retained per item rather than aggregated away.
        "ALTER TABLE model_call ADD COLUMN request TEXT",
        "ALTER TABLE model_call ADD COLUMN response TEXT",
    ),
    # 8 -- provider is recorded alongside model so the same eval set can be run
    # across providers and compared. raw_usage keeps whatever token fields a
    # provider reports beyond the normalised input/output pair.
    (
        "ALTER TABLE model_call ADD COLUMN provider TEXT",
        "ALTER TABLE model_call ADD COLUMN raw_usage TEXT",
        "CREATE INDEX idx_model_call_provider ON model_call (provider, purpose)",
    ),
    # 9 -- inference budget accounting. cost_micros is now populated, but derived
    # from a configured rate table rather than reported by the provider, so
    # rate_basis records whether the figure rests on verified prices or on
    # placeholders. estimated_cost_micros is the pre-call worst case that was
    # checked against the budget, kept so estimates can be compared to outcomes.
    (
        "ALTER TABLE model_call ADD COLUMN estimated_cost_micros INTEGER",
        "ALTER TABLE model_call ADD COLUMN rate_basis TEXT",
    ),
    # 10 -- a paid provider call must survive our own crashes.
    #
    # The row is written before the provider is contacted and finalised afterwards,
    # so a call that fails during parsing -- or takes the process down with it --
    # stays auditable and still counts against the budget. Existing rows predate
    # this and were all successful, hence the default.
    (
        "ALTER TABLE model_call ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'",
        "ALTER TABLE model_call ADD COLUMN error TEXT",
        "CREATE INDEX idx_model_call_status ON model_call (sku, purpose, status)",
    ),
    # 11 -- link evidence to the call that produced it.
    #
    # Mapping is scoped to the latest observation run, because two runs produce
    # near-duplicate observations and citing one of two near-identical rows is
    # arbitrary. Everything is retained: earlier runs stay as append-only evidence
    # for audit and for cross-provider evaluation. Operator evidence has no call and
    # is always in scope.
    (
        "ALTER TABLE evidence ADD COLUMN model_call_id INTEGER REFERENCES model_call(id)",
        "CREATE INDEX idx_evidence_call ON evidence (sku, model_call_id)",
    ),
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database, apply migrations, return a connection."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not db_path.exists()

    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit; we manage txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    if is_new:
        # Refresh tokens live here in plaintext. Owner-only is the minimum bar;
        # on a Mac with FileVault that is a reasonable posture for a personal tool.
        # If that stops being enough, move token storage to the macOS Keychain --
        # TokenStore is the only thing that would need to change.
        db_path.chmod(0o600)

    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, statements in enumerate(MIGRATIONS[version:], start=version + 1):
        with transaction(conn):
            for statement in statements:
                conn.execute(statement)
            # PRAGMA cannot be parameterised; index comes from enumerate, not input.
            conn.execute(f"PRAGMA user_version = {index}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --- event log ---------------------------------------------------------------

_SECRET_KEY_PATTERN = re.compile(
    r"(token|secret|authorization|password|credential|client_id)", re.IGNORECASE
)


def redact(value: Any) -> Any:
    """Recursively replace secret-looking values with a stable fingerprint.

    The log needs to be safe to read, paste into a bug report, and keep forever.
    Fingerprints preserve the one thing that matters for debugging -- whether two
    log lines refer to the same credential -- without storing the credential.
    """
    if isinstance(value, dict):
        return {
            key: (fingerprint(item) if _SECRET_KEY_PATTERN.search(key) and isinstance(item, str) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def fingerprint(secret: str | None) -> str:
    """Short, stable, non-reversible identifier for a credential."""
    import hashlib

    if not secret:
        return "<none>"
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def log_event(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    item_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (ts, kind, item_id, payload) VALUES (?, ?, ?, ?)",
        (now_iso(), kind, item_id, json.dumps(redact(payload or {}), default=str)),
    )


def recent_events(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    )


# --- tiny key/value store (used to carry OAuth state across CLI invocations) ---


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now_iso()),
    )


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key = ?", (key,))
