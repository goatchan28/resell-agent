"""Token persistence. Pure storage, no network.

Separate from tokens.py so that storage and expiry behaviour can be tested
without httpx, credentials, or a network. It is also the single seam to replace
if refresh tokens should live in the macOS Keychain instead of SQLite.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from resell.db import fingerprint
from resell.ebay.oauth import TokenBundle


class TokenStore:
    """Persists TokenBundles in SQLite, keyed by (environment, kind).

    Environment is part of the key so a sandbox token can never be handed to a
    production call, or the reverse.
    """

    def __init__(self, conn: sqlite3.Connection, environment: str):
        self.conn = conn
        self.environment = environment

    def load(self, kind: str) -> TokenBundle | None:
        row = self.conn.execute(
            "SELECT * FROM oauth_tokens WHERE environment = ? AND kind = ?",
            (self.environment, kind),
        ).fetchone()
        if row is None:
            return None

        def parse(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return TokenBundle(
            kind=row["kind"],
            access_token=row["access_token"],
            access_token_expires_at=parse(row["access_token_expires_at"]),
            refresh_token=row["refresh_token"],
            refresh_token_expires_at=parse(row["refresh_token_expires_at"]),
            scopes=row["scopes"],
        )

    def save(self, bundle: TokenBundle) -> None:
        def fmt(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        self.conn.execute(
            """
            INSERT INTO oauth_tokens (
                environment, kind, access_token, access_token_expires_at,
                refresh_token, refresh_token_expires_at, scopes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(environment, kind) DO UPDATE SET
                access_token             = excluded.access_token,
                access_token_expires_at  = excluded.access_token_expires_at,
                refresh_token            = excluded.refresh_token,
                refresh_token_expires_at = excluded.refresh_token_expires_at,
                scopes                   = excluded.scopes,
                updated_at               = excluded.updated_at
            """,
            (
                self.environment,
                bundle.kind,
                bundle.access_token,
                fmt(bundle.access_token_expires_at),
                bundle.refresh_token,
                fmt(bundle.refresh_token_expires_at),
                bundle.scopes,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )

    def delete(self, kind: str) -> None:
        self.conn.execute(
            "DELETE FROM oauth_tokens WHERE environment = ? AND kind = ?",
            (self.environment, kind),
        )


def describe(bundle: TokenBundle | None) -> dict:
    """Safe-to-print summary of a bundle. Never includes token material."""
    if bundle is None:
        return {"present": False}
    now = datetime.now(UTC)

    def remaining(when: datetime | None) -> str:
        if when is None:
            return "unknown"
        delta = when - now
        if delta.total_seconds() < 0:
            return f"EXPIRED {abs(delta)} ago"
        return f"{delta} remaining"

    return {
        "present": True,
        "kind": bundle.kind,
        "access_token": fingerprint(bundle.access_token),
        "access_expires": remaining(bundle.access_token_expires_at),
        "access_valid_now": bundle.access_token_valid(),
        "refresh_token": fingerprint(bundle.refresh_token),
        "refresh_expires": remaining(bundle.refresh_token_expires_at),
        "scopes": bundle.scopes.split(),
    }
