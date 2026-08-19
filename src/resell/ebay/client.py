"""Authenticated HTTP client for the eBay REST APIs.

Every eBay call in the project goes through here, so this is the one place that
knows about tokens, retries, marketplace headers, and error shapes.
"""

from __future__ import annotations

import random
import sqlite3
import time
from typing import Any, Literal

import httpx

from resell.config import Config
from resell.db import log_event
from resell.ebay.store import TokenStore
from resell.ebay.tokens import AppTokenProvider, UserTokenProvider

AuthKind = Literal["user", "app"]
HostKind = Literal["api", "media"]

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
# Beyond this, sleeping is worse than failing: the caller (or the state machine)
# should decide whether to come back later.
MAX_BACKOFF_SECONDS = 30


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After as delta-seconds. HTTP-date form is ignored, not guessed at."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class EbayApiError(RuntimeError):
    """A non-2xx response from an eBay REST API.

    eBay returns a structured `errors` array. Keeping errorId accessible matters:
    the Inventory API's publish errors in particular are only actionable by ID.
    """

    def __init__(self, status_code: int, errors: list[dict], *, method: str, url: str):
        self.status_code = status_code
        self.errors = errors
        self.method = method
        self.url = url
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [f"{self.method} {self.url} -> HTTP {self.status_code}"]
        for error in self.errors or []:
            error_id = error.get("errorId", "?")
            message = error.get("longMessage") or error.get("message") or ""
            lines.append(f"  [{error_id}] {message}")
            for parameter in error.get("parameters", []) or []:
                lines.append(f"      {parameter.get('name')}={parameter.get('value')}")
        return "\n".join(lines)

    @property
    def error_ids(self) -> set[int]:
        return {int(e["errorId"]) for e in self.errors or [] if "errorId" in e}


class EbayClient:
    def __init__(self, config: Config, conn: sqlite3.Connection):
        self.config = config
        self.conn = conn
        store = TokenStore(conn, config.env.name)
        self.user_tokens = UserTokenProvider(config, store, conn)
        self.app_tokens = AppTokenProvider(config, store, conn)
        self._http = httpx.Client(timeout=httpx.Timeout(60.0))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EbayClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- request plumbing ----------------------------------------------------

    def _token(self, auth: AuthKind, *, force_refresh: bool = False) -> str:
        provider = self.user_tokens if auth == "user" else self.app_tokens
        return provider.get_access_token(force_refresh=force_refresh)

    def _base(self, host: HostKind) -> str:
        return self.config.env.api_host if host == "api" else self.config.env.media_host

    def request(
        self,
        method: str,
        path: str,
        *,
        auth: AuthKind = "user",
        host: HostKind = "api",
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        marketplace: bool = True,
        expect_json: bool = True,
        retry_safe: bool | None = None,
    ) -> tuple[int, Any, httpx.Headers]:
        """Make an authenticated call. Returns (status_code, body, response_headers).

        Response headers are returned because some eBay endpoints put the thing you
        actually need there rather than in the body -- Media API image creation
        returns the image id in `location`.

        retry_safe declares whether repeating this exact request is harmless.
        Safety is a property of the operation, not of the status code that came
        back, so the caller decides. Default: True for GET/PUT/DELETE, which are
        idempotent by HTTP semantics and by eBay's design (createOrReplace* is a
        PUT keyed by SKU). False for POST, because createOffer, publishOffer and
        createImageFromFile all create something new each time they succeed.
        Pass retry_safe=True on a POST only when a duplicate is genuinely
        harmless or the endpoint dedupes.
        """
        if retry_safe is None:
            retry_safe = method.upper() in {"GET", "PUT", "DELETE", "HEAD"}
        url = self._base(host) + path
        token = self._token(auth)
        attempt = 0
        refreshed = False

        while True:
            attempt += 1
            request_headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if marketplace:
                request_headers["X-EBAY-C-MARKETPLACE-ID"] = self.config.marketplace_id
            # Let httpx set Content-Type for files (multipart boundary) and json.
            if json is not None:
                request_headers["Content-Type"] = "application/json"
            if headers:
                request_headers.update(headers)

            started = time.monotonic()
            response = self._http.request(
                method,
                url,
                params=params,
                json=json,
                content=content,
                files=files,
                headers=request_headers,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)

            log_event(
                self.conn,
                "ebay.request",
                {
                    "method": method,
                    "url": url,
                    "auth": auth,
                    "status": response.status_code,
                    "elapsed_ms": elapsed_ms,
                    "attempt": attempt,
                    # eBay returns this on REST calls; quote it in support tickets.
                    "rlogid": response.headers.get("rlogid"),
                },
            )

            # Reactive refresh. eBay's own guidance is to refresh on failure rather
            # than track lifetimes precisely, and a token can be revoked long before
            # its nominal expiry. Proactive skew handles the common case; this
            # handles the rest. Once only, so a genuine 401 cannot loop.
            if response.status_code == 401 and not refreshed:
                refreshed = True
                attempt -= 1
                token = self._token(auth, force_refresh=True)
                continue

            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS and retry_safe:
                # eBay enforces both short burst limits (the Media API allows 50
                # POSTs per 5 seconds) and daily quotas. Backing off a few seconds
                # clears a burst limit and does nothing at all for an exhausted
                # daily quota, so honour Retry-After when eBay sends it and give up
                # rather than grind when it does not.
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                if response.status_code == 429 and retry_after is None and attempt > 1:
                    log_event(
                        self.conn,
                        "ebay.rate_limited_giving_up",
                        {"url": url, "attempt": attempt},
                    )
                    break
                backoff = retry_after if retry_after is not None else min(2**attempt, 8)
                if backoff > MAX_BACKOFF_SECONDS:
                    log_event(
                        self.conn,
                        "ebay.retry_after_too_long",
                        {"url": url, "retry_after_s": backoff},
                    )
                    break
                time.sleep(backoff + random.uniform(0, 0.5))
                continue

            break

        if response.status_code >= 400:
            errors: list[dict] = []
            try:
                body = response.json()
                errors = body.get("errors") or [{"message": response.text[:500]}]
            except ValueError:
                errors = [{"message": response.text[:500] or "<empty response body>"}]
            raise EbayApiError(
                response.status_code, errors, method=method, url=url
            )

        if not expect_json or not response.content:
            return response.status_code, None, response.headers
        try:
            return response.status_code, response.json(), response.headers
        except ValueError:
            return response.status_code, response.text, response.headers

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)[1]

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)[1]

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)[1]

    # --- smoke tests ---------------------------------------------------------

    def get_inventory_locations(self, limit: int = 1) -> Any:
        """Cheap read that exercises a user token and the sell.inventory scope.

        A fresh sandbox seller has no locations, so an empty result is success --
        it proves consent, scope, and marketplace routing all work. (An inventory
        location is separately required before an offer can be published; that
        belongs to the publish step, not to auth.)
        """
        return self.get("/sell/inventory/v1/location", params={"limit": limit})

    def get_privileges(self) -> Any:
        """Read-only seller privileges. The most reliable sell.account probe.

        Preferred over the business-policy read as a scope check: it does not
        depend on the account being opted into any program, so a failure here
        really does mean the scope or token is wrong.
        """
        return self.get("/sell/account/v1/privilege")

    def get_opted_in_programs(self) -> Any:
        return self.get("/sell/account/v1/program/get_opted_in_programs")

    def opt_in_to_program(self, program_type: str) -> tuple[int, Any, Any]:
        """Opt the seller into an eBay program.

        Not retry_safe: this mutates account state. It happens to be idempotent in
        practice, but that is eBay's behaviour to guarantee, not ours to assume.
        """
        return self.request(
            "POST",
            "/sell/account/v1/program/opt_in",
            json={"programType": program_type},
            retry_safe=False,
            expect_json=False,
        )

    def get_fulfillment_policies(self) -> Any:
        """Cheap read that exercises the sell.account scope.

        Publishing an offer needs fulfillment, payment and return policy IDs, and
        those are only reachable with this scope. Read-only, so it is safe to run
        before anything is configured -- an empty list still proves the scope was
        granted, which is the thing worth knowing now rather than at publish time.
        """
        return self.get(
            "/sell/account/v1/fulfillment_policy",
            params={"marketplace_id": self.config.marketplace_id},
        )

    def get_default_category_tree_id(self) -> Any:
        """Cheap read that exercises an application token via the Taxonomy API."""
        return self.get(
            "/commerce/taxonomy/v1/get_default_category_tree_id",
            auth="app",
            params={"marketplace_id": self.config.marketplace_id},
        )
