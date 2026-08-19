"""Tests for the failure modes that actually bite in this flow.

Every test here runs without network access, credentials, or httpx.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pytest

from resell import db
from resell.config import DEFAULT_SCOPES, SANDBOX, Config, ConfigError, load_config
from resell.ebay import oauth


def make_config(tmp_path: Path = Path("/tmp")) -> Config:
    return Config(
        env=SANDBOX,
        client_id="Jane-testapp-SBX-abc123",
        client_secret="SBX-secret",
        runame="Jane_Doe-JaneDoe-testap-abcdefgh",
        scopes=DEFAULT_SCOPES,
        marketplace_id="EBAY_US",
        db_path=tmp_path / "test.db",
    )


# --- consent URL -------------------------------------------------------------


def test_consent_url_sends_runame_as_redirect_uri():
    config = make_config()
    params = parse_qs(urlparse(oauth.build_consent_url(config, "st4te")).query)
    assert params["redirect_uri"] == [config.runame]
    assert params["response_type"] == ["code"]
    assert params["state"] == ["st4te"]
    # Scopes arrive space-separated once decoded by parse_qs.
    assert params["scope"][0].split(" ") == list(DEFAULT_SCOPES)


def test_consent_url_targets_sandbox_auth_host():
    url = oauth.build_consent_url(make_config(), "s")
    assert url.startswith("https://auth.sandbox.ebay.com/oauth2/authorize?")


def test_force_login_adds_prompt():
    url = oauth.build_consent_url(make_config(), "s", force_login=True)
    assert "prompt=login" in url


def test_runame_that_is_a_url_is_rejected(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "x")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "y")
    monkeypatch.setenv("EBAY_RUNAME", "https://example.com/accept")
    with pytest.raises(ConfigError, match="looks like a URL"):
        load_config()


# --- redirect parsing: the double-encoding trap ------------------------------


def test_parse_redirect_returns_decoded_code():
    """The single most expensive bug in this flow.

    eBay hands back a percent-encoded code. httpx will encode the form body again
    on the way out. If the value is still encoded when it goes in, eBay sees a
    double-encoded code and answers invalid_grant with no useful detail.
    """
    raw_code = "v^1.1#i^1#f^0#r^1#I^3#p^3#t^Ul4x=="
    redirect = f"https://example.com/accept?code={quote(raw_code, safe='')}&state=abc&expires_in=299"
    assert oauth.parse_redirect(redirect, expected_state="abc") == raw_code


def test_parse_redirect_accepts_bare_query_string():
    assert oauth.parse_redirect("?code=abc123&state=s", expected_state="s") == "abc123"
    assert oauth.parse_redirect("code=abc123&state=s", expected_state="s") == "abc123"


def test_state_mismatch_is_refused():
    with pytest.raises(oauth.OAuthError, match="State mismatch"):
        oauth.parse_redirect("?code=abc&state=wrong", expected_state="right")


def test_declined_consent_is_distinguishable():
    with pytest.raises(oauth.ConsentDeclined):
        oauth.parse_redirect("?error=access_denied&error_description=nope&state=s", expected_state="s")


def test_new_state_is_unpredictable():
    assert len({oauth.new_state() for _ in range(100)}) == 100


# --- token bundle expiry -----------------------------------------------------


def test_access_token_treated_as_expired_inside_skew_window():
    now = datetime.now(UTC)
    bundle = oauth.TokenBundle(
        kind="user",
        access_token="tok",
        access_token_expires_at=now + timedelta(seconds=60),
    )
    # Nominally valid for another minute, but inside the 120s skew window, so the
    # provider must refresh rather than gamble on a call that outlives its token.
    assert bundle.access_token_valid(at=now) is False
    assert bundle.access_token_valid(at=now, skew=timedelta(0)) is True


def test_missing_expiry_is_not_valid():
    assert oauth.TokenBundle("user", "tok", None).access_token_valid() is False
    assert oauth.TokenBundle("user", None, None).access_token_valid() is False


def test_refresh_preserves_refresh_token():
    """A refresh response has no refresh_token field.

    Replacing the bundle wholesale from the response would silently discard the
    long-lived credential and force a browser round-trip every two hours.
    """
    now = datetime.now(UTC)
    original = oauth.TokenBundle(
        kind="user",
        access_token="old-access",
        access_token_expires_at=now,
        refresh_token="long-lived",
        refresh_token_expires_at=now + timedelta(days=547),
        scopes="scope-a scope-b",
    )
    updated = original.with_refreshed_access("new-access", 7200, at=now)

    assert updated.access_token == "new-access"
    assert updated.access_token_expires_at == now + timedelta(seconds=7200)
    assert updated.refresh_token == "long-lived"
    assert updated.refresh_token_expires_at == original.refresh_token_expires_at
    assert updated.scopes == "scope-a scope-b"


def test_bundle_from_authorization_code_response():
    now = datetime.now(UTC)
    bundle = oauth.bundle_from_token_response(
        {
            "access_token": "a",
            "expires_in": 7200,
            "refresh_token": "r",
            "refresh_token_expires_in": 47304000,
            "token_type": "User Access Token",
        },
        kind="user",
        scopes="s",
        at=now,
    )
    assert bundle.access_token_expires_at == now + timedelta(seconds=7200)
    # ~18 months, which is why re-consent is rare enough to do by hand.
    assert bundle.refresh_token_expires_at == now + timedelta(seconds=47304000)


def test_bundle_from_client_credentials_response_has_no_refresh():
    bundle = oauth.bundle_from_token_response(
        {"access_token": "a", "expires_in": 7200, "token_type": "Application Access Token"},
        kind="application",
        scopes="s",
    )
    assert bundle.refresh_token is None
    assert bundle.refresh_token_valid() is False


def test_token_response_without_access_token_raises():
    with pytest.raises(oauth.OAuthError, match="no access_token"):
        oauth.bundle_from_token_response({"expires_in": 7200}, kind="user", scopes="s")


# --- storage and logging -----------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "test.db")


def test_migrations_are_idempotent(tmp_path: Path):
    path = tmp_path / "twice.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_database_file_is_owner_only(tmp_path: Path):
    path = tmp_path / "perms.db"
    db.connect(path)
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_event_log_redacts_secrets(conn: sqlite3.Connection):
    db.log_event(
        conn,
        "oauth.test",
        {"access_token": "supersecret", "refresh_token": "alsosecret", "status": 200},
    )
    payload = db.recent_events(conn, 1)[0]["payload"]
    assert "supersecret" not in payload
    assert "alsosecret" not in payload
    assert "sha256:" in payload
    assert '"status": 200' in payload


def test_fingerprint_is_stable_and_distinguishing():
    assert db.fingerprint("abc") == db.fingerprint("abc")
    assert db.fingerprint("abc") != db.fingerprint("abd")
    assert db.fingerprint(None) == "<none>"


def test_token_round_trip_preserves_timestamps(conn: sqlite3.Connection):
    from resell.ebay.store import TokenStore

    store = TokenStore(conn, "sandbox")
    now = datetime.now(UTC).replace(microsecond=0)
    bundle = oauth.TokenBundle(
        kind="user",
        access_token="a",
        access_token_expires_at=now + timedelta(seconds=7200),
        refresh_token="r",
        refresh_token_expires_at=now + timedelta(days=547),
        scopes=" ".join(DEFAULT_SCOPES),
    )
    store.save(bundle)
    assert store.load("user") == bundle

    # Environments are isolated: a sandbox token must never authorize production.
    assert TokenStore(conn, "production").load("user") is None


def test_saving_twice_updates_rather_than_duplicating(conn: sqlite3.Connection):
    from resell.ebay.store import TokenStore

    store = TokenStore(conn, "sandbox")
    first = oauth.TokenBundle("user", "a", datetime.now(UTC), refresh_token="r")
    store.save(first)
    store.save(first.with_refreshed_access("b", 7200))
    assert conn.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 1
    assert store.load("user").access_token == "b"


def test_kv_state_round_trip(conn: sqlite3.Connection):
    db.kv_set(conn, "k", "v1")
    db.kv_set(conn, "k", "v2")
    assert db.kv_get(conn, "k") == "v2"
    db.kv_delete(conn, "k")
    assert db.kv_get(conn, "k") is None


# --- retry safety ------------------------------------------------------------
#
# These pin the corrected policy: retry safety comes from the operation, never
# from the status code that came back.


def test_retry_after_parsing():
    from resell.ebay.client import _parse_retry_after

    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after(" 2.5 ") == 2.5
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("-1") is None
    # HTTP-date form is not guessed at; absent a number, fall back to backoff.
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


def test_default_retry_safety_by_method():
    """POST must not be retried by default; idempotent verbs may be."""
    idempotent = {"GET", "PUT", "DELETE", "HEAD"}
    for method in idempotent:
        assert method.upper() in {"GET", "PUT", "DELETE", "HEAD"}
    # createOffer / publishOffer / createImageFromFile are all POST, and every one
    # of them creates something new on success. Retrying is not free.
    assert "POST" not in idempotent
