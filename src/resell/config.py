"""Configuration and environment definitions.

Deliberately dependency-light so it can be imported anywhere, including tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Request every scope V1 will need, up front.
#
# eBay's guidance is explicit about why: adding a scope to an existing user token
# requires a fresh consent grant. For a single-user personal tool that means
# re-running the browser dance; there is no reason to do it twice. sell.account is
# needed because publishing an offer requires business policy IDs, and
# sell.fulfillment is needed the moment something actually sells.
#
# Note the host: scope identifiers are always api.ebay.com, even in sandbox.
DEFAULT_SCOPES: tuple[str, ...] = (
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
)


@dataclass(frozen=True)
class EbayEnvironment:
    name: str
    auth_host: str   # consent page (browser)
    api_host: str    # REST APIs + token endpoint
    media_host: str  # Media API image methods live on a different host

    @property
    def consent_url(self) -> str:
        return f"{self.auth_host}/oauth2/authorize"

    @property
    def token_url(self) -> str:
        return f"{self.api_host}/identity/v1/oauth2/token"


SANDBOX = EbayEnvironment(
    name="sandbox",
    auth_host="https://auth.sandbox.ebay.com",
    api_host="https://api.sandbox.ebay.com",
    # The Media API image methods are documented under apim.* rather than api.*.
    # Kept separate rather than assumed identical; verify on first upload.
    media_host="https://apim.sandbox.ebay.com",
)

PRODUCTION = EbayEnvironment(
    name="production",
    auth_host="https://auth.ebay.com",
    api_host="https://api.ebay.com",
    media_host="https://apim.ebay.com",
)

ENVIRONMENTS = {e.name: e for e in (SANDBOX, PRODUCTION)}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    env: EbayEnvironment
    client_id: str
    client_secret: str
    runame: str
    scopes: tuple[str, ...]
    marketplace_id: str
    db_path: Path

    @property
    def scope_string(self) -> str:
        """Space-separated scopes. Callers must not pre-encode; the HTTP layer does."""
        return " ".join(self.scopes)

    @property
    def has_credentials(self) -> bool:
        """Whether this config could authenticate. Same rule `load_config` applies.

        A caller that was handed a config rather than loading one still needs to
        ask this before reaching for eBay, and duplicating the three-field check
        at each such site is how they drift apart.
        """
        return bool(self.client_id and self.client_secret and self.runame)


def _load_dotenv() -> None:
    """Load .env if python-dotenv is installed. Optional so the package imports bare."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def load_config(*, require_credentials: bool = True) -> Config:
    _load_dotenv()

    env_name = os.environ.get("EBAY_ENV", "sandbox").strip().lower()
    if env_name not in ENVIRONMENTS:
        raise ConfigError(
            f"EBAY_ENV must be one of {sorted(ENVIRONMENTS)}, got {env_name!r}"
        )

    client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    runame = os.environ.get("EBAY_RUNAME", "").strip()

    if require_credentials:
        missing = [
            name
            for name, value in (
                ("EBAY_CLIENT_ID", client_id),
                ("EBAY_CLIENT_SECRET", client_secret),
                ("EBAY_RUNAME", runame),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill it in."
            )

    # A URL here is the single most common setup mistake: eBay's redirect_uri
    # parameter takes the RuName token, not the accept URL it points at.
    if runame.startswith(("http://", "https://")):
        raise ConfigError(
            "EBAY_RUNAME looks like a URL. eBay's redirect_uri parameter wants the "
            "RuName value (e.g. 'Jane_Doe-JaneDoe-MyApp-abcdefgh'), not the accept "
            "URL configured behind it."
        )

    raw_scopes = os.environ.get("EBAY_SCOPES", "").split()
    scopes = tuple(raw_scopes) if raw_scopes else DEFAULT_SCOPES

    db_path = Path(os.environ.get("RESELL_DB", "./data/resell.db")).expanduser()

    return Config(
        env=ENVIRONMENTS[env_name],
        client_id=client_id,
        client_secret=client_secret,
        runame=runame,
        scopes=scopes,
        marketplace_id=os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_US").strip(),
        db_path=db_path,
    )
