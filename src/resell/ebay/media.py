"""Upload local photos to eBay Picture Services and get back hosted URLs.

Deliberately narrow surface: `upload(path) -> UploadedImage`. Nothing downstream
knows what EPS is, which API version this uses, or which host it lives on. That
matters because the replacement is `v1_beta` -- a beta path is the sole successor
to `UploadSiteHostedPictures`, which eBay decommissions on 2026-09-30 -- so a
version bump should be a one-file change.

Two behaviours worth knowing about:

Uploads are keyed by file content hash. Re-uploading the same photo is a database
lookup, not an API call, which keeps re-runs cheap and makes the upload step
safely repeatable -- the same property the state machine needs from every step.

EPS URLs expire. eBay no longer extends the life of unused images beyond 30 days,
and this pipeline has a human approval gate in the middle that can take days. So
expiry is stored and checked, and an expired image is re-uploaded rather than
handed to `createOrReplaceInventoryItem` as a dead URL.
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from resell.db import kv_get, kv_set, log_event, now_iso
from resell.derivatives import ConversionError, Derivative, ensure_uploadable, source_digest
from resell.ebay.client import EbayApiError, EbayClient
from resell.images import ImageFacts, inspect

MEDIA_BASE = "/commerce/media/v1_beta/image"

# Documented: all POST methods in the Media API allow 50 requests per 5 seconds
# at the user level.
RATE_LIMIT_REQUESTS = 50
RATE_LIMIT_WINDOW_SECONDS = 5.0

# Re-upload rather than reuse a URL that expires sooner than this. An image is
# useless the moment eBay drops it, and a publish that fails on a dead URL costs
# far more than a redundant upload.
EXPIRY_MARGIN = timedelta(days=2)

HOST_CHOICE_KEY = "ebay.media_host_kind"


class ImageUploadError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadedImage:
    image_id: str
    eps_url: str | None
    expires_at: datetime | None
    reused: bool = False
    # Set when a JPEG derivative was uploaded instead of the original file.
    converted_from: str | None = None
    # Where expires_at came from. "ebay" means eBay stated it; "assumed" means we
    # fell back to the documented 30-day default because the response carried no
    # date. Worth distinguishing: the re-upload-before-expiry logic is only as
    # trustworthy as this date, and an assumption that looks like a fact is the
    # kind of thing that fails quietly months later.
    expiry_source: str = "unknown"

    @property
    def usable(self) -> bool:
        return bool(self.eps_url)


class ImageUploader(Protocol):
    """The seam. Swap the implementation without touching callers."""

    def upload(self, path: str | Path) -> UploadedImage: ...


class RateLimiter:
    """Sliding-window limiter. In-process only, which is all a single-user tool needs."""

    def __init__(self, requests: int = RATE_LIMIT_REQUESTS, window: float = RATE_LIMIT_WINDOW_SECONDS):
        self.requests = requests
        self.window = window
        self._timestamps: deque[float] = deque()

    def acquire(self, *, sleep=time.sleep, clock=time.monotonic) -> float:
        """Block until a slot is free. Returns how long it waited."""
        waited = 0.0
        while True:
            now = clock()
            while self._timestamps and now - self._timestamps[0] >= self.window:
                self._timestamps.popleft()
            if len(self._timestamps) < self.requests:
                self._timestamps.append(now)
                return waited
            delay = self.window - (now - self._timestamps[0]) + 0.01
            sleep(delay)
            waited += delay


class EbayMediaUploader:
    """createImageFromFile against the Media API."""

    def __init__(
        self, client: EbayClient, conn: sqlite3.Connection, *, convert: bool = True
    ):
        self.client = client
        self.conn = conn
        self.limiter = RateLimiter()
        # convert=False forces the original bytes at eBay. Only useful for probing
        # what EPS actually accepts; the default is the reliable path.
        self.convert = convert
        self.cache_dir = Path(client.config.db_path).parent / "derivatives"

    # --- persistence ---------------------------------------------------------

    def _lookup(self, digest: str) -> UploadedImage | None:
        row = self.conn.execute(
            "SELECT * FROM images WHERE environment = ? AND content_sha256 = ?",
            (self.client.config.env.name, digest),
        ).fetchone()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
        if expires_at and expires_at - EXPIRY_MARGIN <= datetime.now(UTC):
            log_event(
                self.conn,
                "media.cached_url_expired",
                {"image_id": row["image_id"], "expires_at": row["expires_at"]},
            )
            return None
        return UploadedImage(
            row["image_id"], row["eps_url"], expires_at, reused=True, expiry_source="stored"
        )

    def _record(
        self, digest: str, source: Path, facts: ImageFacts, image: UploadedImage
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO images (
                environment, content_sha256, image_id, eps_url, expires_at,
                local_path, size_bytes, width, height, image_format, uploaded_at,
                uploaded_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(environment, content_sha256) DO UPDATE SET
                image_id      = excluded.image_id,
                eps_url       = excluded.eps_url,
                expires_at    = excluded.expires_at,
                local_path    = excluded.local_path,
                uploaded_at   = excluded.uploaded_at,
                uploaded_path = excluded.uploaded_path
            """,
            (
                self.client.config.env.name,
                digest,
                image.image_id,
                image.eps_url,
                image.expires_at.isoformat() if image.expires_at else None,
                str(source.resolve()),
                facts.size_bytes,
                facts.width,
                facts.height,
                facts.image_format,
                now_iso(),
                str(facts.path.resolve()),
            ),
        )

    # --- host resolution -----------------------------------------------------

    def _host_kinds(self) -> tuple[str, ...]:
        """Which host to try, most likely first.

        eBay documents the image methods under apim.ebay.com while the rest of the
        platform, including other Media API resources, sits on api.ebay.com. That
        inconsistency was flagged as unverified during design, so rather than bet
        on it, try the documented host and fall back once. The winner is cached, so
        the fallback costs one wasted call ever.
        """
        remembered = kv_get(self.conn, HOST_CHOICE_KEY)
        if remembered in ("media", "api"):
            return (remembered,)
        return ("media", "api")

    # --- the upload ----------------------------------------------------------

    def upload(self, path: str | Path) -> UploadedImage:
        source = Path(path)
        if not source.exists():
            raise ImageUploadError(f"{source} does not exist")

        # Identity is the ORIGINAL file's content, not the derivative's. Two runs
        # of the same photo must dedupe even though re-encoding is not guaranteed
        # to be byte-identical.
        digest = source_digest(source)
        cached = self._lookup(digest)
        if cached:
            return cached

        if self.convert:
            try:
                derivative = ensure_uploadable(source, self.cache_dir, digest=digest)
            except ConversionError as exc:
                raise ImageUploadError(str(exc)) from exc
        else:
            derivative = Derivative(source, source, converted=False)

        # Validate what will actually be sent. For a HEIC this is the only point
        # at which dimensions are knowable, since the original is not parseable.
        facts = inspect(derivative.upload_path)
        if not facts.ok:
            raise ImageUploadError(
                f"{source.name} failed local validation: " + "; ".join(facts.errors)
            )

        data = derivative.upload_path.read_bytes()

        waited = self.limiter.acquire()
        if waited:
            log_event(self.conn, "media.rate_limited", {"waited_s": round(waited, 2)})

        image_id, location = self._create_image(facts, data)
        eps_url, expires_at, expiry_source = self._resolve_url(image_id)

        image = UploadedImage(
            image_id,
            eps_url,
            expires_at,
            expiry_source=expiry_source,
            converted_from=derivative.note if derivative.converted else None,
        )
        self._record(digest, source, facts, image)
        log_event(
            self.conn,
            "media.image_uploaded",
            {
                "image_id": image_id,
                "location": location,
                "sha256": digest[:12],
                "bytes": facts.size_bytes,
                "dimensions": facts.dimensions,
                "format": facts.image_format,
                "source": source.name,
                "converted": derivative.note or None,
                "expires_at": expires_at,
                "expiry_source": expiry_source,
                "host_kind": kv_get(self.conn, HOST_CHOICE_KEY),
            },
        )
        return image

    def _create_image(self, facts: ImageFacts, data: bytes) -> tuple[str, str]:
        """POST the multipart body. Returns (image_id, raw location header)."""
        last_error: EbayApiError | None = None
        for host_kind in self._host_kinds():
            try:
                status, body, headers = self.client.request(
                    "POST",
                    f"{MEDIA_BASE}/create_image_from_file",
                    host="media" if host_kind == "media" else "api",
                    # Single form key named "image". httpx sets the multipart
                    # Content-Type and boundary; setting it by hand breaks the
                    # boundary and produces an opaque 400.
                    files={"image": (facts.path.name, data)},
                    retry_safe=False,  # a retry creates a second EPS image
                    expect_json=False,
                    marketplace=False,
                )
            except EbayApiError as exc:
                last_error = exc
                # 404 means wrong host/route, which the other host may fix.
                # Anything else is about the image or the credential and will
                # recur identically, so stop.
                if exc.status_code != 404:
                    raise
                log_event(
                    self.conn, "media.host_rejected", {"host_kind": host_kind, "status": 404}
                )
                continue

            kv_set(self.conn, HOST_CHOICE_KEY, host_kind)
            location = headers.get("location", "") or ""
            image_id = self._image_id_from(location, body)
            if not image_id:
                raise ImageUploadError(
                    f"upload returned HTTP {status} but no image id could be found "
                    f"in the location header ({location!r}) or body ({body!r})"
                )
            return image_id, location

        raise ImageUploadError(
            "createImageFromFile returned 404 on both documented hosts; the API path "
            f"may have moved off v1_beta. Last error:\n{last_error}"
        )

    @staticmethod
    def _image_id_from(location: str, body: object) -> str | None:
        """The id arrives in the location header, as a full getImage URI."""
        if location:
            return location.rstrip("/").rsplit("/", 1)[-1] or None
        if isinstance(body, dict):
            return body.get("imageId") or body.get("image_id")
        return None

    def _resolve_url(self, image_id: str) -> tuple[str | None, datetime | None, str]:
        """Get the EPS URL and expiry for an image.

        createImageFromFile signals success with 201 and a location header; eBay's
        guidance is to call getImage for the details, including expiration. Rather
        than assume the create response is empty, this reads it if present and only
        falls back to getImage when it is not.
        """
        try:
            body = self.client.get(
                f"{MEDIA_BASE}/{image_id}",
                host="media" if kv_get(self.conn, HOST_CHOICE_KEY) != "api" else "api",
                marketplace=False,
            )
        except EbayApiError as exc:
            log_event(
                self.conn,
                "media.get_image_failed",
                {"image_id": image_id, "status": exc.status_code},
            )
            return None, None, "unknown"

        url = (body or {}).get("imageUrl")
        raw_expiry = (body or {}).get("expirationDate") or (body or {}).get("expiryDate")
        expires_at = _parse_timestamp(raw_expiry)
        if expires_at is not None:
            return url, expires_at, "ebay"
        # Unused EPS images are dropped after 30 days and eBay no longer extends
        # that, so assuming the documented default is safer than treating the URL
        # as permanent. Flagged as assumed so it is never mistaken for eBay's word.
        return url, datetime.now(UTC) + timedelta(days=30), "assumed"


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def upload_all(
    uploader: ImageUploader, paths: list[str | Path]
) -> list[tuple[Path, UploadedImage | Exception]]:
    """Upload a photo set, collecting failures rather than aborting on the first.

    Partial results are useful: knowing that photo 5 of 8 is the problem beats
    knowing only that the set failed.
    """
    results: list[tuple[Path, UploadedImage | Exception]] = []
    for path in paths:
        try:
            results.append((Path(path), uploader.upload(path)))
        except (ImageUploadError, EbayApiError) as exc:
            results.append((Path(path), exc))
    return results
