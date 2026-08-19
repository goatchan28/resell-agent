"""Token persistence and the network calls that mint tokens.

Two providers, because eBay needs both kinds of credential for V1:

  UserTokenProvider  -- authorization_code + refresh_token grants. Required for
                        anything acting on the seller's behalf: Inventory API,
                        Media API image upload, offers, orders.
  AppTokenProvider   -- client_credentials grant. Required for the research half
                        of the pipeline: Taxonomy (category suggestions, item
                        aspects) and Browse (active-listing comps).

Both expose get_access_token(). Callers never touch a grant type.
"""

from __future__ import annotations

import base64
import sqlite3
from datetime import UTC, datetime

import httpx

from resell.config import Config
from resell.db import log_event
from resell.ebay.oauth import (
    NeedsConsent,
    OAuthError,
    TokenBundle,
    bundle_from_token_response,
)
from resell.ebay.store import TokenStore

TOKEN_TIMEOUT = httpx.Timeout(30.0)


def _basic_auth_header(config: Config) -> str:
    raw = f"{config.client_id}:{config.client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _post_token(config: Config, form: dict[str, str]) -> dict:
    """POST to the identity token endpoint and return the parsed JSON body.

    `form` values must be plain (unencoded); httpx form-encodes them.
    """
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _basic_auth_header(config),
    }
    with httpx.Client(timeout=TOKEN_TIMEOUT) as client:
        response = client.post(config.env.token_url, headers=headers, data=form)

    try:
        payload = response.json()
    except ValueError:
        raise OAuthError(
            f"Token endpoint returned non-JSON (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        ) from None

    if response.status_code >= 400 or "error" in payload:
        error = payload.get("error")
        description = payload.get("error_description")
        message = (
            f"Token request failed (HTTP {response.status_code}): {error} - {description}"
        )
        # invalid_grant on an exchange is nearly always one of a short list of
        # causes; say so rather than making the reader go find the docs.
        if error == "invalid_grant":
            message += (
                "\n\nUsual causes, in order of likelihood:"
                "\n  1. The authorization code was already used (single use only)."
                "\n  2. The code expired (~5 minutes) -- run login again and be quicker."
                "\n  3. redirect_uri does not exactly match the RuName used for consent."
                "\n  4. Code was double-encoded, or credentials are from the other environment."
            )
        raise OAuthError(message, error=error, description=description)

    return payload


class UserTokenProvider:
    """Supplies a valid user access token, refreshing as needed."""

    kind = "user"

    def __init__(self, config: Config, store: TokenStore, conn: sqlite3.Connection):
        self.config = config
        self.store = store
        self.conn = conn

    def exchange_code(self, code: str) -> TokenBundle:
        """Trade a fresh authorization code for a user token + refresh token.

        `code` must be the decoded value from oauth.parse_redirect.
        """
        payload = _post_token(
            self.config,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.runame,
            },
        )
        bundle = bundle_from_token_response(
            payload, kind=self.kind, scopes=self.config.scope_string
        )
        if not bundle.refresh_token:
            raise OAuthError(
                "Authorization code grant returned no refresh_token. Without it the "
                "browser flow would be needed every two hours."
            )
        self.store.save(bundle)
        log_event(
            self.conn,
            "oauth.user_token_minted",
            {
                "environment": self.config.env.name,
                "access_token": bundle.access_token,
                "refresh_token": bundle.refresh_token,
                "access_expires_at": bundle.access_token_expires_at,
                "refresh_expires_at": bundle.refresh_token_expires_at,
                "scopes": bundle.scopes,
            },
        )
        return bundle

    def refresh(self, *, bundle: TokenBundle | None = None) -> TokenBundle:
        bundle = bundle or self.store.load(self.kind)
        if bundle is None:
            raise NeedsConsent(
                "No stored user token. Run: resell auth login"
            )
        if not bundle.refresh_token_valid():
            raise NeedsConsent(
                "The refresh token is missing or expired. Run: resell auth login"
            )

        form = {
            "grant_type": "refresh_token",
            "refresh_token": bundle.refresh_token,
            # Optional per the docs (defaults to the consent scopes), sent
            # explicitly so a scope drift shows up as an error here rather than as
            # a confusing 403 on some unrelated call later.
            "scope": bundle.scopes or self.config.scope_string,
        }
        try:
            payload = _post_token(self.config, form)
        except OAuthError as exc:
            # eBay revokes refresh tokens on password or login-name change, and
            # users can revoke app access by hand. Both surface here as
            # invalid_grant, and both mean the same thing: consent again.
            if exc.error == "invalid_grant":
                log_event(
                    self.conn,
                    "oauth.refresh_rejected",
                    {"environment": self.config.env.name, "error": exc.error},
                )
                raise NeedsConsent(
                    "eBay rejected the refresh token. It was probably revoked -- this "
                    "happens if the account password or login name changed, or app "
                    "access was revoked.\nRun: resell auth login"
                ) from exc
            raise

        updated = bundle.with_refreshed_access(
            payload["access_token"], int(payload["expires_in"])
        )
        self.store.save(updated)
        log_event(
            self.conn,
            "oauth.access_token_refreshed",
            {
                "environment": self.config.env.name,
                "access_token": updated.access_token,
                "expires_at": updated.access_token_expires_at,
            },
        )
        return updated

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        bundle = self.store.load(self.kind)
        if bundle is None:
            raise NeedsConsent("No stored user token. Run: resell auth login")
        if force_refresh or not bundle.access_token_valid():
            bundle = self.refresh(bundle=bundle)
        assert bundle.access_token is not None
        return bundle.access_token


class AppTokenProvider:
    """Supplies an application access token via client_credentials.

    No user consent, no refresh token: when it expires, mint another.
    """

    kind = "application"

    def __init__(self, config: Config, store: TokenStore, conn: sqlite3.Connection):
        self.config = config
        self.store = store
        self.conn = conn

    def mint(self) -> TokenBundle:
        # Only the base scope is valid for client credentials; the sell.* scopes
        # are user-consent scopes and will be rejected here.
        scope = "https://api.ebay.com/oauth/api_scope"
        payload = _post_token(
            self.config, {"grant_type": "client_credentials", "scope": scope}
        )
        bundle = bundle_from_token_response(payload, kind=self.kind, scopes=scope)
        self.store.save(bundle)
        log_event(
            self.conn,
            "oauth.app_token_minted",
            {
                "environment": self.config.env.name,
                "access_token": bundle.access_token,
                "expires_at": bundle.access_token_expires_at,
            },
        )
        return bundle

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        bundle = self.store.load(self.kind)
        if force_refresh or bundle is None or not bundle.access_token_valid():
            bundle = self.mint()
        assert bundle.access_token is not None
        return bundle.access_token
