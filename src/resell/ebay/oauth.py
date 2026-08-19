"""Pure OAuth logic for eBay: URL construction, redirect parsing, expiry rules.

No HTTP dependency and no I/O. Everything here is a function of its arguments,
which is what makes the fiddly parts (and they are fiddly) testable without
credentials or a network.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

from resell.config import Config

# Refresh this far ahead of nominal expiry. Covers clock skew and slow calls.
EXPIRY_SKEW = timedelta(seconds=120)


class OAuthError(RuntimeError):
    """Token exchange or refresh failed."""

    def __init__(self, message: str, *, error: str | None = None, description: str | None = None):
        super().__init__(message)
        self.error = error
        self.description = description


class NeedsConsent(OAuthError):
    """No usable refresh token. The browser consent flow must be re-run.

    Raised when there is no stored token at all, when the refresh token has
    expired, or when eBay has revoked it -- which it does silently if the account
    password or login name changes, or if the user revokes app access.
    """


class ConsentDeclined(OAuthError):
    """The redirect came back without a code, i.e. the grant was declined."""


@dataclass(frozen=True)
class TokenBundle:
    """A stored credential set for one (environment, kind) pair."""

    kind: str  # 'user' | 'application'
    access_token: str | None
    access_token_expires_at: datetime | None
    refresh_token: str | None = None
    refresh_token_expires_at: datetime | None = None
    scopes: str = ""

    def access_token_valid(self, *, at: datetime | None = None, skew: timedelta = EXPIRY_SKEW) -> bool:
        if not self.access_token or not self.access_token_expires_at:
            return False
        return (at or datetime.now(UTC)) + skew < self.access_token_expires_at

    def refresh_token_valid(self, *, at: datetime | None = None) -> bool:
        if not self.refresh_token:
            return False
        if self.refresh_token_expires_at is None:
            return True  # unknown expiry: assume usable, let eBay be the judge
        return (at or datetime.now(UTC)) < self.refresh_token_expires_at

    def with_refreshed_access(self, access_token: str, expires_in: int, *, at: datetime | None = None) -> TokenBundle:
        """Apply a refresh response.

        The refresh response contains only access_token/expires_in/token_type --
        no new refresh_token. Merging rather than replacing is what stops a
        refresh from wiping out the long-lived credential.
        """
        issued = at or datetime.now(UTC)
        return replace(
            self,
            access_token=access_token,
            access_token_expires_at=issued + timedelta(seconds=expires_in),
        )


def bundle_from_token_response(
    payload: dict,
    *,
    kind: str,
    scopes: str,
    at: datetime | None = None,
) -> TokenBundle:
    """Build a bundle from a full token response (authorization_code or client_credentials)."""
    issued = at or datetime.now(UTC)
    access_token = payload.get("access_token")
    if not access_token:
        raise OAuthError(f"Token response contained no access_token: keys={sorted(payload)}")

    expires_in = int(payload.get("expires_in", 0))
    refresh_token = payload.get("refresh_token")
    refresh_expires_in = payload.get("refresh_token_expires_in")

    return TokenBundle(
        kind=kind,
        access_token=access_token,
        access_token_expires_at=issued + timedelta(seconds=expires_in),
        refresh_token=refresh_token,
        refresh_token_expires_at=(
            issued + timedelta(seconds=int(refresh_expires_in)) if refresh_expires_in else None
        ),
        scopes=scopes,
    )


def new_state() -> str:
    """Opaque CSRF value echoed back by eBay in the redirect."""
    return secrets.token_urlsafe(24)


def build_consent_url(config: Config, state: str, *, force_login: bool = False) -> str:
    """Build the Grant Application Access URL the browser must visit.

    redirect_uri takes the RuName, not a URL. urlencode handles the percent
    encoding of the space-separated scope list.
    """
    params = {
        "client_id": config.client_id,
        "redirect_uri": config.runame,
        "response_type": "code",
        "scope": config.scope_string,
        "state": state,
    }
    if force_login:
        # Useful when several sandbox test users exist and the browser already
        # holds a session for the wrong one.
        params["prompt"] = "login"
    return f"{config.env.consent_url}?{urlencode(params)}"


def parse_redirect(redirect: str, *, expected_state: str | None = None) -> str:
    """Extract the authorization code from the URL the browser landed on.

    Accepts a full URL, a bare query string, or one with a leading '?'.

    Returns the code in DECODED form. This matters more than it looks: eBay
    delivers the code percent-encoded in the redirect, and the docs say the value
    must be encoded when posted to the token endpoint. Since any sane HTTP client
    form-encodes the body for you, passing the still-encoded string double-encodes
    it and eBay answers `invalid_grant`. parse_qs decodes once here; httpx encodes
    once on the way out. Do not "helpfully" re-encode in between.
    """
    parsed = urlparse(redirect)
    query = parsed.query or (redirect.lstrip("?") if "=" in redirect else "")
    params = parse_qs(query)

    if expected_state is not None:
        received = (params.get("state") or [None])[0]
        if received != expected_state:
            raise OAuthError(
                "State mismatch between the consent request and the redirect. "
                "Do not continue -- start the login flow again.\n"
                f"  expected: {expected_state}\n  received: {received}"
            )

    code = (params.get("code") or [None])[0]
    if not code:
        error = (params.get("error") or [None])[0]
        description = (params.get("error_description") or [None])[0]
        raise ConsentDeclined(
            "Redirect URL contained no authorization code"
            + (f" (error={error}: {description})" if error else "")
            + ".\nIf you clicked 'Not now' on the consent page, run login again and accept.",
            error=error,
            description=description,
        )
    return code
