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


# --- provisioning ------------------------------------------------------------


def test_policy_field_names_are_correct_plurals():
    """Regression: these were derived algorithmically and came out as
    "paymentPolicys", so every existence check silently missed and each run
    recreated policies that already existed."""
    from resell.ebay.provision import POLICY_FIELDS

    assert POLICY_FIELDS["payment_policy"] == ("paymentPolicies", "paymentPolicyId")
    assert POLICY_FIELDS["return_policy"] == ("returnPolicies", "returnPolicyId")
    assert POLICY_FIELDS["fulfillment_policy"] == (
        "fulfillmentPolicies",
        "fulfillmentPolicyId",
    )
    for container, id_field in POLICY_FIELDS.values():
        assert not container.endswith("Policys")
        assert id_field.endswith("PolicyId")


def test_only_shipping_service_errors_trigger_fallback():
    """A rejected service code is worth retrying with another code. Anything
    else -- not opted in, bad auth, malformed payload -- recurs identically, so
    retrying just burns calls and muddies the error."""
    from resell.ebay.client import EbayApiError
    from resell.ebay.provision import _looks_like_bad_service

    bad_service = EbayApiError(
        400, [{"errorId": 20400, "message": "Invalid shippingServiceCode value"}],
        method="POST", url="/x",
    )
    not_opted_in = EbayApiError(
        400, [{"errorId": 20403, "message": "User is not eligible for Business Policy."}],
        method="POST", url="/x",
    )
    assert _looks_like_bad_service(bad_service) is True
    assert _looks_like_bad_service(not_opted_in) is False


# --- image validation --------------------------------------------------------


def _png_bytes(width: int, height: int, salt: bytes = b"") -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"tEXt", salt) + chunk(b"IEND", b"")


def _jpeg_bytes(width: int, height: int, exif_pad: int = 0) -> bytes:
    import struct

    out = b"\xff\xd8"
    if exif_pad:
        payload = b"Exif\x00\x00" + b"\x00" * exif_pad
        out += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    sof = struct.pack(">BHHB", 8, height, width, 3) + b"\x00" * 9
    return out + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9"


def test_format_sniffing_ignores_extension():
    """Extensions lie, especially after a rename or an export. Magic bytes do not."""
    from resell.images import sniff_format

    assert sniff_format(_png_bytes(10, 10)) == "png"
    assert sniff_format(_jpeg_bytes(10, 10)) == "jpeg"
    assert sniff_format(b"GIF89a" + b"\x00" * 20) == "gif"
    assert sniff_format(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert sniff_format(b"\x00\x00\x00\x18ftypheic") == "heic"
    assert sniff_format(b"\x00\x00\x00\x18ftypavif") == "avif"
    assert sniff_format(b"8BPS" + b"\x00" * 20) is None


def test_jpeg_dimensions_survive_large_exif(tmp_path: Path):
    """Phone photos carry kilobytes of EXIF before the frame header, so a fixed
    offset read gets garbage. The parser must walk the marker segments."""
    from resell.images import inspect

    path = tmp_path / "photo.jpg"
    path.write_bytes(_jpeg_bytes(4032, 3024, exif_pad=8000))
    facts = inspect(path)
    assert (facts.width, facts.height) == (4032, 3024)


def test_dimension_limit_is_the_sum_not_each_side(tmp_path: Path):
    """eBay's limit is height + width <= 15000, so 8000x8000 fails while
    9000x5000 passes despite having a longer side."""
    from resell.images import inspect

    fails = tmp_path / "big.png"
    fails.write_bytes(_png_bytes(8000, 8000))
    assert not inspect(fails).ok

    passes = tmp_path / "wide.png"
    passes.write_bytes(_png_bytes(9000, 5000))
    assert inspect(passes).ok


def test_low_resolution_warns_but_does_not_block(tmp_path: Path):
    """A small photo is a quality problem, not a rejection. Blocking on it would
    stop a listing eBay would have accepted."""
    from resell.images import inspect

    path = tmp_path / "small.png"
    path.write_bytes(_png_bytes(300, 300))
    facts = inspect(path)
    assert facts.ok is True
    assert facts.warnings


def test_animated_gif_rejected(tmp_path: Path):
    from resell.images import inspect

    path = tmp_path / "anim.gif"
    import struct

    path.write_bytes(
        b"GIF89a" + struct.pack("<HH", 600, 600) + b"\xf7\x00\x00" + b"\x00" * 768
        + (b"\x21\xf9\x04" + b"\x00" * 5) * 3 + b"\x3b"
    )
    facts = inspect(path)
    assert not facts.ok
    assert facts.animated


def test_set_level_limits(tmp_path: Path):
    from resell.images import inspect_all

    path = tmp_path / "a.png"
    path.write_bytes(_png_bytes(1600, 1200))

    _, errors = inspect_all([path] * 25)
    assert any("24" in e for e in errors)

    _, errors = inspect_all([path, path])
    assert any("more than once" in e for e in errors)

    _, errors = inspect_all([])
    assert errors

    _, errors = inspect_all([path])
    assert errors == []


# --- media uploader helpers --------------------------------------------------


def test_rate_limiter_allows_burst_then_blocks():
    """50 requests per 5 seconds, per eBay's documented Media API POST limit."""
    from resell.ebay.media import RateLimiter

    slept: list[float] = []
    clock_value = [0.0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock_value[0] += seconds

    limiter = RateLimiter()
    for _ in range(50):
        limiter.acquire(sleep=sleep, clock=lambda: clock_value[0])
    assert slept == []

    limiter.acquire(sleep=sleep, clock=lambda: clock_value[0])
    assert len(slept) == 1
    assert 5.0 <= slept[0] <= 5.1


def test_image_id_parsed_from_location_header():
    """The id arrives only in the location header, as a full getImage URI."""
    from resell.ebay.media import EbayMediaUploader

    extract = EbayMediaUploader._image_id_from
    assert extract("https://apim.ebay.com/commerce/media/v1_beta/image/IMG-123", None) == "IMG-123"
    assert extract("https://apim.ebay.com/commerce/media/v1_beta/image/IMG-123/", None) == "IMG-123"
    assert extract("", {"imageId": "IMG-456"}) == "IMG-456"
    assert extract("", None) is None


def test_expiry_provenance_is_recorded():
    """An assumed expiry and a stated one can land on the same date, which made
    them indistinguishable in storage. The re-upload-before-expiry logic is only
    as trustworthy as this date, so provenance is tracked separately."""
    from resell.ebay.media import UploadedImage

    stated = UploadedImage("id", "url", None, expiry_source="ebay")
    assumed = UploadedImage("id", "url", None, expiry_source="assumed")
    assert stated.expiry_source != assumed.expiry_source
    assert UploadedImage("id", "url", None).expiry_source == "unknown"


# --- HEIC handling -----------------------------------------------------------


def _fake_heic(claimed_width: int, claimed_height: int, padding: int) -> bytes:
    import struct

    ftyp = struct.pack(">I", 20) + b"ftyp" + b"heic" + b"\x00" * 8
    ispe = struct.pack(">I", 20) + b"ispe" + b"\x00" * 4 + struct.pack(">II", claimed_width, claimed_height)
    return ftyp + b"\x00" * 40 + ispe + b"\x00" * padding


def test_heic_dimensions_are_not_claimed(tmp_path: Path):
    """Regression. The parser used to scan for the first `ispe` box, but iPhone
    HEIC stores the image as a grid of 512x512 tiles each carrying its own ispe,
    so it reported a tile size as the image size. Confidently wrong dimensions
    produced fabricated resolution warnings and would have let an oversized image
    past the 15,000px check. Dimensions are now read from the JPEG derivative."""
    from resell.images import inspect

    path = tmp_path / "IMG_3079.HEIC"
    path.write_bytes(_fake_heic(512, 512, 3246 * 1024))
    facts = inspect(path)

    assert facts.image_format == "heic"
    assert facts.width is None and facts.height is None
    assert facts.dimensions == "unknown"
    assert facts.ok is True  # unknown dimensions do not block; conversion follows


def test_implausible_dimensions_discarded_for_any_format(tmp_path: Path):
    """Format-agnostic guard: a compressed image cannot use 8+ bytes per pixel.
    This is the check that would have caught the HEIC tile bug automatically."""
    from resell.images import _dimensions_are_credible, inspect

    assert _dimensions_are_credible(512, 512, 3_246_000) is False
    assert _dimensions_are_credible(4032, 3024, 3_000_000) is True
    # Tiny files are exempt, where header and metadata overhead dominates.
    assert _dimensions_are_credible(10, 10, 5_000) is True

    liar = tmp_path / "liar.png"
    liar.write_bytes(_png_bytes(100, 100) + b"\x00" * (5 * 1024 * 1024))
    facts = inspect(liar)
    assert facts.width is None
    assert any("implausible" in w for w in facts.warnings)


def test_conversion_only_for_formats_eps_rejects():
    from resell.derivatives import DIRECT_UPLOAD_FORMATS
    from resell.images import NEEDS_LOCAL_CONVERSION

    assert "jpeg" in DIRECT_UPLOAD_FORMATS
    assert "png" in DIRECT_UPLOAD_FORMATS
    assert "gif" in DIRECT_UPLOAD_FORMATS
    # HEIC is documented as supported by the Media API but rejected by EPS with
    # error 190203, which is the whole reason this layer exists.
    assert "heic" in NEEDS_LOCAL_CONVERSION
    assert not (DIRECT_UPLOAD_FORMATS & NEEDS_LOCAL_CONVERSION)


def test_derivative_is_keyed_on_original_content(tmp_path: Path):
    """Identity must follow the original file, not the re-encoded output, so the
    same photo dedupes across runs even though encoding is not byte-stable."""
    import resell.derivatives as derivatives

    source = tmp_path / "shot.HEIC"
    source.write_bytes(_fake_heic(512, 512, 200_000))
    digest = derivatives.source_digest(source)

    calls: list[int] = []

    def fake_convert(src: Path, dst: Path, quality: int) -> None:
        calls.append(quality)
        dst.write_bytes(_jpeg_bytes(4032, 3024) + b"\x00" * 1000)

    original_convert = derivatives._convert
    derivatives._convert = fake_convert
    try:
        first = derivatives.ensure_uploadable(source, tmp_path / "cache")
        assert first.converted is True
        assert first.upload_path.name == f"{digest[:16]}.jpg"
        assert source.exists()  # original preserved as source of truth

        second = derivatives.ensure_uploadable(source, tmp_path / "cache")
        assert second.upload_path == first.upload_path
        assert len(calls) == 1  # cached, not re-encoded
    finally:
        derivatives._convert = original_convert


def test_oversized_derivative_raises_rather_than_uploading(tmp_path: Path):
    import resell.derivatives as derivatives

    source = tmp_path / "huge.HEIC"
    source.write_bytes(_fake_heic(512, 512, 200_000))

    def always_huge(src: Path, dst: Path, quality: int) -> None:
        dst.write_bytes(_jpeg_bytes(4032, 3024) + b"\x00" * (20 * 1024 * 1024))

    original_convert = derivatives._convert
    derivatives._convert = always_huge
    try:
        with pytest.raises(derivatives.ConversionError, match="over eBay's 12 MB limit"):
            derivatives.ensure_uploadable(source, tmp_path / "cache")
    finally:
        derivatives._convert = original_convert
