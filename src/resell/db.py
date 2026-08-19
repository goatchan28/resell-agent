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
