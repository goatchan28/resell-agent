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


# --- execution spike ---------------------------------------------------------


def test_only_required_aspects_are_filled():
    """Optional aspects are omitted; free-text required aspects get eBay's
    conventional placeholder rather than being left empty (which fails publish)."""
    from resell.ebay.client import EbayClient  # noqa: F401  (import shape check)
    from resell.spike import required_aspects

    class FakeClient:
        def get(self, path, **kwargs):
            return {
                "aspects": [
                    {
                        "localizedAspectName": "Format",
                        "aspectConstraint": {"aspectRequired": True},
                        "aspectValues": [{"localizedValue": "Paperback"}, {"localizedValue": "Hardcover"}],
                    },
                    {
                        "localizedAspectName": "Author",
                        "aspectConstraint": {"aspectRequired": True},
                        "aspectValues": [],
                    },
                    {
                        "localizedAspectName": "Genre",
                        "aspectConstraint": {"aspectRequired": False},
                        "aspectValues": [{"localizedValue": "Sci-Fi"}],
                    },
                ]
            }

    filled = required_aspects(FakeClient(), "0", "261186")
    assert filled == {"Format": ["Paperback"], "Author": ["Does not apply"]}
    assert "Genre" not in filled


def test_publish_diagnosis_identifies_each_known_cause():
    from resell.spike import diagnose_publish_failure

    assert "25018 CONFIRMED" in diagnose_publish_failure([25018], "")
    assert "shipping service" in diagnose_publish_failure([25007], "")
    assert "system error" in diagnose_publish_failure([25001], "").lower()
    assert "aspect" in diagnose_publish_failure([], "Missing required item specific").lower()
    assert "Unrecognised" in diagnose_publish_failure([12345], "")


def test_write_calls_carry_content_language():
    """The Inventory API requires Content-Language on writes, and the error it
    returns when the header is absent does not mention the header."""
    from resell.spike import WRITE_HEADERS

    assert WRITE_HEADERS["Content-Language"] == "en-US"


# --- domain: states, sku, pricing --------------------------------------------


def test_sku_is_sequential_zero_padded_and_sortable():
    from resell.domain import format_sku, parse_sku

    assert format_sku(1) == "MP-000001"
    assert format_sku(42) == "MP-000042"
    assert format_sku(999999) == "MP-999999"
    # Lexical order must match numeric order; eBay sorts SKUs as strings.
    assert sorted([format_sku(2), format_sku(10), format_sku(1)]) == [
        "MP-000001", "MP-000002", "MP-000010",
    ]
    assert parse_sku("MP-000042") == 42
    with pytest.raises(ValueError):
        format_sku(0)
    with pytest.raises(ValueError):
        parse_sku("2026-08-19-0001")


def test_terminal_states_have_no_exits():
    from resell.domain import TERMINAL_STATES, TRANSITIONS, ItemState

    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()
    assert ItemState.LISTED in TERMINAL_STATES


def test_every_state_appears_in_the_transition_table():
    """A state missing from the table would fail closed, but silently — better to
    catch it here than to discover an item can never leave a state."""
    from resell.domain import TRANSITIONS, ItemState

    assert set(TRANSITIONS) == set(ItemState)
    for targets in TRANSITIONS.values():
        for target in targets:
            assert target in TRANSITIONS


def test_publication_floor_is_net_proceeds_not_cost_margin():
    """A cost-based floor would block decluttered items, where cost basis is zero
    or unknown and any sale is a good sale."""
    from resell.domain import meets_publication_floor

    ok, _ = meets_publication_floor(1999, seller_shipping_cost_cents=400)
    assert ok is True
    # Seller-borne shipping must reduce proceeds — forgetting it is how a listing
    # loses money while looking fine.
    ok, reason = meets_publication_floor(600, seller_shipping_cost_cents=400)
    assert ok is False
    assert "shipping" in reason


def test_profitability_reports_but_never_gates():
    from resell.domain import profitability

    unknown = profitability(1999, None, seller_shipping_cost_cents=400)
    assert unknown["profit_cents"] is None
    assert unknown["net_proceeds_cents"] == 1292

    # A zero cost basis must not raise or report infinite ROI.
    free = profitability(1999, 0, seller_shipping_cost_cents=400)
    assert free["profit_cents"] == 1292
    assert free["roi_pct"] is None

    bought = profitability(1999, 1200, seller_shipping_cost_cents=400)
    assert bought["profit_cents"] == 92
    assert bought["roi_pct"] == 7.7


def test_proposal_hash_ignores_key_and_list_order():
    from resell.domain import Proposal, ShippingTerms

    base = dict(
        sku="MP-000001", marketplace="EBAY_US", title="t", description="d",
        category_id="1", condition_id="USED_GOOD", price_cents=1999, currency="USD",
        shipping_terms=ShippingTerms.SELLER_PAID, seller_shipping_cost_cents=0,
        buyer_shipping_charge_cents=0, fulfillment_policy_id="f", payment_policy_id="p",
        return_policy_id="r", merchant_location_key="l",
    )
    a = Proposal(aspects={"B": ["2", "1"], "A": ["x"]}, photo_hashes=("h2", "h1"), **base)
    b = Proposal(aspects={"A": ["x"], "B": ["1", "2"]}, photo_hashes=("h1", "h2"), **base)
    assert a.content_hash() == b.content_hash()

    changed = Proposal(aspects={"A": ["x"], "B": ["1", "2"]}, photo_hashes=("h1",), **base)
    assert changed.content_hash() != a.content_hash()


def test_proposal_validation_collects_all_problems():
    """One round trip should tell the model everything to fix, not just the first
    thing."""
    from resell.domain import Proposal, ShippingTerms

    bad = Proposal(
        sku="MP-000001", marketplace="EBAY_US", title="x" * 90, description="",
        category_id="", condition_id="", aspects={}, price_cents=100, currency="USD",
        shipping_terms=ShippingTerms.SELLER_PAID, seller_shipping_cost_cents=0,
        buyer_shipping_charge_cents=0, photo_hashes=(), fulfillment_policy_id="",
        payment_policy_id="", return_policy_id="", merchant_location_key="",
    )
    problems = bad.validate(required_aspects={"Author"})
    assert len(problems) >= 8
    assert any("80 limit" in p for p in problems)
    assert any("'Author'" in p for p in problems)
    assert any("floor" in p for p in problems)


# --- gateway -----------------------------------------------------------------


def _gateway(tmp_path: Path):
    from resell.gateway import Gateway

    conn = db.connect(tmp_path / "gw.db")
    return Gateway(conn, environment="sandbox"), conn


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def _valid_proposal(sku: str, photo_hashes: tuple[str, ...]):
    from resell.domain import Proposal, ShippingTerms

    return Proposal(
        sku=sku, marketplace="EBAY_US", title="Dune by Frank Herbert paperback",
        description="Good condition.", category_id="261186", condition_id="USED_GOOD",
        aspects={"Author": ["Frank Herbert"], "Format": ["Paperback"]},
        price_cents=1999, currency="USD",
        shipping_terms=ShippingTerms.SELLER_PAID, seller_shipping_cost_cents=400,
        buyer_shipping_charge_cents=0,
        photo_hashes=photo_hashes, fulfillment_policy_id="FUL-1",
        payment_policy_id="PAY-1", return_policy_id="RET-1",
        merchant_location_key="resell-primary",
    )


def test_sku_never_reused_after_delete(tmp_path: Path):
    """A reused SKU would collide with eBay's record of the previous item."""
    gateway, conn = _gateway(tmp_path)
    first = gateway.ingest_item(purchase_cost_cents=100).sku
    conn.execute("DELETE FROM item WHERE sku = ?", (first,))
    assert gateway.ingest_item(purchase_cost_cents=100).sku != first


def test_confidence_is_stored_but_never_gates(tmp_path: Path):
    from resell.domain import ItemState

    gateway, conn = _gateway(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=None, acquisition_intent="declutter").sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(
        sku, title="t", category_id="1", condition_id="USED_GOOD", confidence=0.05
    )
    # Very low confidence still passes: the gate is required-information plus no
    # unresolved blocking unknowns, not a number the model picked.
    assert gateway.begin_pricing(sku).to_state == ItemState.PRICING
    stored = conn.execute("SELECT confidence FROM identification WHERE sku = ?", (sku,)).fetchone()
    assert stored[0] == 0.05


def test_operator_only_commands_reject_the_model(tmp_path: Path):
    from resell.gateway import Rejected

    gateway, conn = _gateway(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=None).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.ask_operator(sku, question="Is it cracked?")
    question_id = conn.execute("SELECT id FROM open_question WHERE sku = ?", (sku,)).fetchone()["id"]

    with pytest.raises(Rejected, match="only the operator"):
        gateway.answer_question(question_id, "no")
    with pytest.raises(Rejected, match="only the operator"):
        gateway.approve(sku, "anyhash")


def test_approval_is_voided_by_content_change(tmp_path: Path):
    """The invariant that stops "you approved it, then it changed, then we
    published something you never saw"."""
    from resell.gateway import Rejected, live_approval

    gateway, conn = _gateway(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=1200, acquisition_intent="resale").sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, title="t", category_id="261186", condition_id="USED_GOOD")
    gateway.begin_pricing(sku)
    accepted = gateway.propose_listing(
        sku, _valid_proposal(sku, (_digest("a"),)), required_aspects={"Author", "Format"}
    )
    gateway.approve(sku, accepted.data["proposal_hash"], operator=True)
    assert live_approval(conn, sku) is not None

    gateway.attach_photo(
        sku, source_path="/b.jpg", content_sha256=_digest("b"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    assert live_approval(conn, sku) is None
    # Voiding reverts the state too, so `approved` never lies about reality.
    assert conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0] == "proposed"
    with pytest.raises(Rejected, match="not a legal transition"):
        gateway.begin_publishing(sku)


def test_approve_requires_matching_hash(tmp_path: Path):
    from resell.gateway import Rejected

    gateway, conn = _gateway(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=None).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, title="t", category_id="261186", condition_id="USED_GOOD")
    gateway.begin_pricing(sku)
    gateway.propose_listing(
        sku, _valid_proposal(sku, (_digest("a"),)), required_aspects=set()
    )
    with pytest.raises(Rejected, match="does not match"):
        gateway.approve(sku, "0" * 64, operator=True)


def test_listed_requires_a_listing_id(tmp_path: Path):
    """An HTTP 2xx is not proof of publication — a stubbed 2xx with no listingId
    looked exactly like success during the spike."""
    from resell.domain import ItemState
    from resell.gateway import Rejected

    gateway, conn = _gateway(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=None).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, title="t", category_id="261186", condition_id="USED_GOOD")
    gateway.begin_pricing(sku)
    accepted = gateway.propose_listing(
        sku, _valid_proposal(sku, (_digest("a"),)), required_aspects=set()
    )
    gateway.approve(sku, accepted.data["proposal_hash"], operator=True)
    gateway.begin_publishing(sku)

    with pytest.raises(Rejected, match="does not prove publication"):
        gateway.mark_listed(sku)

    gateway.record_publish_progress(sku, offer_id="OFF-1")
    gateway.record_publish_progress(sku, listing_id="110590224174")
    assert gateway.mark_listed(sku).to_state == ItemState.LISTED

    with pytest.raises(Rejected, match="not a legal transition"):
        gateway.revise(sku)


def test_evidence_immutability_is_database_enforced(tmp_path: Path):
    gateway, conn = _gateway(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=None).sku
    gateway.record_evidence(sku, kind="web_search", source="https://x", payload={"a": 1})

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE evidence SET kind = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM evidence")


def test_send_to_model_filters_prompt_context(tmp_path: Path):
    """A record can be retained for audit without being eligible for a prompt."""
    gateway, conn = _gateway(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=None).sku
    gateway.record_evidence(sku, kind="visible", source="s", payload={"a": 1})
    gateway.record_evidence(sku, kind="withheld", source="s", payload={"b": 2}, send_to_model=False)

    kinds = [item["kind"] for item in gateway.model_context(sku)["evidence"]]
    assert "visible" in kinds
    assert "withheld" not in kinds
    assert conn.execute("SELECT COUNT(*) FROM evidence WHERE sku = ?", (sku,)).fetchone()[0] == 2


def test_proposal_photos_must_belong_to_the_item(tmp_path: Path):
    from resell.gateway import Rejected

    gateway, conn = _gateway(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=None).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, title="t", category_id="261186", condition_id="USED_GOOD")
    gateway.begin_pricing(sku)
    with pytest.raises(Rejected, match="not an attached validated photo"):
        gateway.propose_listing(sku, _valid_proposal(sku, (_digest("elsewhere"),)))


def test_proposal_does_not_require_uploaded_images(tmp_path: Path):
    """Uploads happen at publish, not at proposal: uploading earlier would burn
    EPS uploads on items that are never approved and start the 30-day expiry
    clock during an open-ended human review."""
    from resell.domain import ItemState

    gateway, conn = _gateway(tmp_path)
    sku = gateway.ingest_item(purchase_cost_cents=None).sku
    gateway.attach_photo(
        sku, source_path="/a.HEIC", content_sha256=_digest("a"),
        image_format="heic", size_bytes=3_000_000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, title="t", category_id="261186", condition_id="USED_GOOD")
    gateway.begin_pricing(sku)

    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
    accepted = gateway.propose_listing(sku, _valid_proposal(sku, (_digest("a"),)))
    assert accepted.to_state == ItemState.PROPOSED


# --- fee provenance and shipping decoupling ----------------------------------


def test_fee_basis_marks_estimates_as_estimates():
    """A floor result must never present a generic estimate as a guarantee."""
    from resell.domain import FeeBasis, FeeModel, compute_proceeds

    provisional = compute_proceeds(1999, seller_shipping_cost_cents=400)
    assert provisional.fee_basis == FeeBasis.PROVISIONAL_ESTIMATE
    assert provisional.is_estimate is True
    assert "estimated" in provisional.describe()

    verified = compute_proceeds(
        1999,
        seller_shipping_cost_cents=400,
        fees=FeeModel(rate=0.13, fixed_cents=30, basis=FeeBasis.CATEGORY_VERIFIED),
    )
    assert verified.is_estimate is False
    assert "computed" in verified.describe()


def test_production_publishing_requires_an_authoritative_fee_basis(tmp_path: Path):
    from resell.domain import FeeBasis, FeeModel, ItemState
    from resell.gateway import Gateway, Rejected

    conn = db.connect(tmp_path / "prod.db")
    production = Gateway(conn, environment="production", fees=FeeModel())
    sku = production.ingest_item(purchase_cost_cents=2500).sku

    # Legality is checked before preconditions, so a fresh item is refused for
    # being in `intake`. The fee objection is asserted against the entry guard,
    # which is where it lives.
    with pytest.raises(Rejected, match="not a legal transition"):
        production.begin_publishing(sku)
    reasons = production._entry_preconditions(sku, ItemState.PUBLISHING)
    assert any("cannot back a" in reason for reason in reasons)

    # Sandbox is unaffected: a provisional estimate is fine for development.
    sandbox = Gateway(conn, environment="sandbox", fees=FeeModel())
    reasons = sandbox._entry_preconditions(sku, ItemState.PUBLISHING)
    assert not any("fee basis" in reason for reason in reasons)

    # With a verified basis, the fee objection disappears in production too; what
    # remains is the missing approval.
    verified = Gateway(
        conn,
        environment="production",
        fees=FeeModel(basis=FeeBasis.CATEGORY_VERIFIED, source="checked 2026-08"),
    )
    reasons = verified._entry_preconditions(sku, ItemState.PUBLISHING)
    assert reasons
    assert not any("fee basis" in reason for reason in reasons)


def test_shipping_terms_are_represented_not_assumed():
    """The schema must not encode seller-paid shipping. All four arrangements are
    expressible; only seller-paid is implemented, and the rest fail loudly."""
    from resell.domain import (
        IMPLEMENTED_SHIPPING_TERMS,
        Proposal,
        ShippingTerms,
        compute_proceeds,
    )

    assert IMPLEMENTED_SHIPPING_TERMS == frozenset({ShippingTerms.SELLER_PAID})
    assert len(list(ShippingTerms)) == 4

    seller_paid = compute_proceeds(1999, seller_shipping_cost_cents=400)
    buyer_paid = compute_proceeds(1999, buyer_shipping_charge_cents=400)
    # Same postage, opposite payer: buyer-paid nets more, and eBay's fee applies to
    # the gross including the shipping the buyer was charged.
    assert buyer_paid.net_cents > seller_paid.net_cents
    assert buyer_paid.gross_cents == 2399
    assert seller_paid.gross_cents == 1999

    base = dict(
        sku="MP-000001", marketplace="EBAY_US", title="t", description="d",
        category_id="1", condition_id="USED_GOOD", aspects={}, price_cents=5000,
        currency="USD", photo_hashes=("h",), fulfillment_policy_id="f",
        payment_policy_id="p", return_policy_id="r", merchant_location_key="l",
    )
    unimplemented = Proposal(
        shipping_terms=ShippingTerms.BUYER_PAID, seller_shipping_cost_cents=0,
        buyer_shipping_charge_cents=400, **base,
    )
    assert any("not implemented" in p for p in unimplemented.validate())

    contradictory = Proposal(
        shipping_terms=ShippingTerms.SELLER_PAID, seller_shipping_cost_cents=400,
        buyer_shipping_charge_cents=400, **base,
    )
    assert any("cannot also charge the buyer" in p for p in contradictory.validate())

    pickup = Proposal(
        shipping_terms=ShippingTerms.LOCAL_PICKUP, seller_shipping_cost_cents=400,
        buyer_shipping_charge_cents=0, **base,
    )
    assert any("no shipping cost" in p for p in pickup.validate())


def test_preconditions_are_enforced_on_state_entry(tmp_path: Path):
    """Regression: preconditions used to live in the public commands, so calling
    the private _transition directly moved an item to `publishing` with a voided
    approval. Enforcing on entry means no code path can skip them."""
    from resell.domain import ItemState
    from resell.gateway import Gateway, Rejected, live_approval

    conn = db.connect(tmp_path / "entry.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=2500, acquisition_intent="resale").sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, title="t", category_id="3002", condition_id="USED_EXCELLENT")
    gateway.begin_pricing(sku)
    accepted = gateway.propose_listing(sku, _valid_proposal(sku, (_digest("a"),)))
    gateway.approve(sku, accepted.data["proposal_hash"], operator=True)

    # Void the approval by changing the photo set.
    gateway.attach_photo(
        sku, source_path="/b.jpg", content_sha256=_digest("b"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    assert live_approval(conn, sku) is None

    # Force the incoherent state the gateway no longer produces, simulating a
    # hand-edited or corrupted database. The entry guard must still refuse, because
    # this is defense in depth rather than a consequence of the void behaviour.
    conn.execute("UPDATE item SET state = 'approved' WHERE sku = ?", (sku,))
    with pytest.raises(Rejected, match="no live approval"):
        gateway._transition(sku, ItemState.PUBLISHING, command="Forced")
    assert conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0] == "approved"


def test_entry_preconditions_cover_every_gated_state(tmp_path: Path):
    """A gated state with no entry check would be enterable unconditionally."""
    from resell.domain import ItemState
    from resell.gateway import Gateway

    conn = db.connect(tmp_path / "cover.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=None).sku
    for state in (
        ItemState.IDENTIFYING,
        ItemState.PRICING,
        ItemState.APPROVED,
        ItemState.PUBLISHING,
        ItemState.LISTED,
    ):
        assert gateway._entry_preconditions(sku, state), f"{state} has no entry guard"


def test_truncated_hash_is_distinguished_from_a_changed_proposal(tmp_path: Path):
    """Regression: both sides of the comparison were abbreviated to 16 chars for
    display, so pasting the truncated value produced an error showing two
    identical strings that "do not match" — which reads as a broken program."""
    from resell.gateway import Gateway, Rejected

    conn = db.connect(tmp_path / "hash.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=2500).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(sku, title="t", category_id="3002", condition_id="USED_EXCELLENT")
    gateway.begin_pricing(sku)
    accepted = gateway.propose_listing(sku, _valid_proposal(sku, (_digest("a"),)))
    full = accepted.data["proposal_hash"]
    assert len(full) == 64

    with pytest.raises(Rejected) as info:
        gateway.approve(sku, full[:16], operator=True)
    message = "\n".join(info.value.reasons)
    assert "truncated" in message
    assert "copy/paste" in message
    assert full in message  # the usable value is handed back

    with pytest.raises(Rejected) as info:
        gateway.approve(sku, "0" * 64, operator=True)
    message = "\n".join(info.value.reasons)
    assert "truncated" not in message
    # Never abbreviate the two values being compared.
    assert full in message
    assert "0" * 64 in message

    assert gateway.approve(sku, full, operator=True).to_state.value == "approved"


# --- approval/state coherence and photo removal ------------------------------


def _build_approved(gateway, conn, *, photo_count: int = 2):
    from resell.domain import Proposal, ShippingTerms
    from resell.gateway import validated_photos

    sku = gateway.ingest_item(purchase_cost_cents=2500, acquisition_intent="resale").sku
    for index in range(photo_count):
        gateway.attach_photo(
            sku, source_path=f"/p{index}.jpg", content_sha256=_digest(f"{sku}{index}"),
            image_format="jpeg", size_bytes=1000, validation_errors=None,
        )
    gateway.begin_identification(sku)
    gateway.propose_identification(
        sku, title="Blazer 42R", category_id="3002", condition_id="USED_EXCELLENT"
    )
    gateway.begin_pricing(sku)
    proposal = Proposal(
        sku=sku, marketplace="EBAY_US", title="Blazer 42R", description="Navy wool.",
        category_id="3002", condition_id="USED_EXCELLENT", aspects={"Brand": ["BB"]},
        price_cents=8900, currency="USD", shipping_terms=ShippingTerms.SELLER_PAID,
        seller_shipping_cost_cents=1200, buyer_shipping_charge_cents=0,
        photo_hashes=tuple(p["content_sha256"] for p in validated_photos(conn, sku)),
        fulfillment_policy_id="FUL", payment_policy_id="PAY", return_policy_id="RET",
        merchant_location_key="resell-primary",
    )
    accepted = gateway.propose_listing(sku, proposal, required_aspects={"Brand"})
    gateway.approve(sku, accepted.data["proposal_hash"], operator=True)
    return sku


def test_voiding_an_approval_reverts_the_state(tmp_path: Path):
    """`approved` must imply a live approval. Otherwise the state label lies, and
    recovery needlessly costs a trip back through pricing."""
    from resell.domain import ItemState
    from resell.gateway import Gateway, live_approval

    conn = db.connect(tmp_path / "revert.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = _build_approved(gateway, conn)
    assert gateway._entry_preconditions(sku, ItemState.PUBLISHING) == []

    gateway.attach_photo(
        sku, source_path="/extra.jpg", content_sha256=_digest("extra"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    assert live_approval(conn, sku) is None
    state = conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0]
    assert state == str(ItemState.PROPOSED)


def test_remove_photo_voids_approval_and_compacts_positions(tmp_path: Path):
    """eBay treats the first image as the gallery photo, so contiguous ordering is
    part of the listing's meaning."""
    from resell.domain import ItemState
    from resell.gateway import Gateway, live_approval, validated_photos

    conn = db.connect(tmp_path / "remove.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = _build_approved(gateway, conn, photo_count=4)
    assert [p["position"] for p in validated_photos(conn, sku)] == [1, 2, 3, 4]

    gateway.remove_photo(sku, position=2)
    remaining = validated_photos(conn, sku)
    assert [p["position"] for p in remaining] == [1, 2, 3]
    assert [p["source_path"] for p in remaining] == ["/p0.jpg", "/p2.jpg", "/p3.jpg"]
    assert live_approval(conn, sku) is None
    state = conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0]
    assert state == str(ItemState.PROPOSED)

    # Removal by content hash works too.
    target = validated_photos(conn, sku)[-1]["content_sha256"]
    gateway.remove_photo(sku, content_sha256=target)
    assert len(validated_photos(conn, sku)) == 2


def test_remove_photo_selector_and_existence_guards(tmp_path: Path):
    from resell.gateway import Gateway, Rejected

    conn = db.connect(tmp_path / "guards.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = _build_approved(gateway, conn)

    with pytest.raises(Rejected, match="exactly one of"):
        gateway.remove_photo(sku)
    with pytest.raises(Rejected, match="exactly one of"):
        gateway.remove_photo(sku, position=1, content_sha256="abc")
    with pytest.raises(Rejected, match="no photo at"):
        gateway.remove_photo(sku, position=999)


def test_photo_set_is_frozen_once_published(tmp_path: Path):
    """A published listing lives on eBay. Changing the local photo set would
    silently desync the two, and listing revision is not implemented."""
    from resell.gateway import Gateway, Rejected

    conn = db.connect(tmp_path / "frozen.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = _build_approved(gateway, conn)
    gateway.begin_publishing(sku)
    gateway.record_publish_progress(sku, offer_id="OFF-1")
    gateway.record_publish_progress(sku, listing_id="110590224174")
    gateway.mark_listed(sku)

    with pytest.raises(Rejected, match="photo set cannot change"):
        gateway.remove_photo(sku, position=1)
    with pytest.raises(Rejected, match="photo set cannot change"):
        gateway.attach_photo(
            sku, source_path="/x.jpg", content_sha256=_digest("x"),
            image_format="jpeg", size_bytes=1, validation_errors=None,
        )


def test_approval_revalidates_content_not_just_the_hash(tmp_path: Path):
    """A hash match proves the proposal has not changed since approval. It does not
    prove the proposal is still valid — removing every photo changes the hash and
    voids the old approval, but nothing stopped a fresh approval of a photoless
    listing until entry preconditions re-ran validation."""
    from resell.gateway import Gateway, Rejected, active_listing

    conn = db.connect(tmp_path / "revalidate.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = _build_approved(gateway, conn, photo_count=1)
    gateway.remove_photo(sku, position=1)

    listing = active_listing(conn, sku, "EBAY_US", "sandbox")
    current = gateway._proposal_from_listing(sku, listing).content_hash()
    with pytest.raises(Rejected, match="no longer valid: at least one photo"):
        gateway.approve(sku, current, operator=True)

    # Restoring a photo makes it approvable again.
    gateway.attach_photo(
        sku, source_path="/new.jpg", content_sha256=_digest("new"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    listing = active_listing(conn, sku, "EBAY_US", "sandbox")
    restored = gateway._proposal_from_listing(sku, listing).content_hash()
    assert gateway.approve(sku, restored, operator=True).to_state.value == "approved"


def test_verify_safeguards_builds_its_own_fixture(tmp_path: Path, monkeypatch):
    """Regression: it used to probe whatever item you named, and three checks
    reported false results because their premises were not established —
    tampering with an empty evidence table raises nothing, and a "forced
    transition past a voided approval" succeeds when the approval is live. One
    check also mutated real state on success."""
    import argparse

    monkeypatch.setenv("RESELL_DB", str(tmp_path / "verify.db"))
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    monkeypatch.setenv("EBAY_CLIENT_ID", "a")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "b")
    monkeypatch.setenv("EBAY_RUNAME", "X-Y-Z-abc")

    from resell import cli_item

    # A pre-existing item must be left alone entirely.
    from resell.gateway import Gateway

    conn = db.connect(tmp_path / "verify.db")
    existing = Gateway(conn, environment="sandbox").ingest_item(purchase_cost_cents=100).sku
    before = conn.execute("SELECT state FROM item WHERE sku = ?", (existing,)).fetchone()[0]

    assert cli_item.cmd_item_verify_safeguards(argparse.Namespace()) == 0

    after = conn.execute("SELECT state FROM item WHERE sku = ?", (existing,)).fetchone()[0]
    assert after == before

    # The fixture exists, is abandoned, and its SKU is distinct and retired.
    fixture = conn.execute(
        "SELECT sku, state FROM item WHERE sku != ? ORDER BY seq DESC LIMIT 1", (existing,)
    ).fetchone()
    assert fixture["state"] == "abandoned"
    assert fixture["sku"] != existing
    # Evidence must be present, or the append-only checks would pass vacuously.
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ?", (fixture["sku"],)
    ).fetchone()[0] >= 1


def test_migration_6_heals_approved_without_a_live_approval(tmp_path: Path):
    """Databases written before void-reverts-state can hold `approved` with no live
    approval. Publishing was already blocked, but the item could neither publish
    nor be re-approved (approve requires `proposed`), so it was stuck."""
    import hashlib

    from resell.domain import Proposal, ShippingTerms
    from resell.gateway import Gateway, active_listing, live_approval

    path = tmp_path / "legacy.db"
    full = db.MIGRATIONS
    db.MIGRATIONS = full[:5]
    try:
        conn = db.connect(path)
        gateway = Gateway(conn, environment="sandbox")

        def build(with_listing: bool) -> str:
            sku = gateway.ingest_item(purchase_cost_cents=2500).sku
            digest = hashlib.sha256(f"{sku}a".encode()).hexdigest()
            gateway.attach_photo(
                sku, source_path="/a.jpg", content_sha256=digest,
                image_format="jpeg", size_bytes=1000, validation_errors=None,
            )
            gateway.begin_identification(sku)
            gateway.propose_identification(
                sku, title="Blazer", category_id="3002", condition_id="USED_EXCELLENT"
            )
            gateway.begin_pricing(sku)
            if with_listing:
                proposal = Proposal(
                    sku=sku, marketplace="EBAY_US", title="Blazer", description="Navy.",
                    category_id="3002", condition_id="USED_EXCELLENT", aspects={},
                    price_cents=8900, currency="USD",
                    shipping_terms=ShippingTerms.SELLER_PAID,
                    seller_shipping_cost_cents=1200, buyer_shipping_charge_cents=0,
                    photo_hashes=(digest,), fulfillment_policy_id="F",
                    payment_policy_id="P", return_policy_id="R", merchant_location_key="L",
                )
                accepted = gateway.propose_listing(sku, proposal)
                gateway.approve(sku, accepted.data["proposal_hash"], operator=True)
            return sku

        stuck = build(True)
        # Simulate the pre-fix void: mark voided without reverting the state.
        conn.execute(
            "UPDATE approval SET voided_at = ?, voided_reason = 'legacy' WHERE sku = ?",
            (db.now_iso(), stuck),
        )
        orphan = build(False)
        conn.execute("UPDATE item SET state = 'approved' WHERE sku = ?", (orphan,))
        healthy = build(True)
        conn.close()
    finally:
        db.MIGRATIONS = full

    conn = db.connect(path)

    def state_of(sku: str) -> str:
        return conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0]

    # With a proposal to return to, `proposed`; without one, `pricing`.
    assert state_of(stuck) == "proposed"
    assert state_of(orphan) == "pricing"
    # An item with a genuinely live approval must not be disturbed.
    assert state_of(healthy) == "approved"
    assert live_approval(conn, healthy) is not None

    repairs = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'item.state_repaired'"
    ).fetchone()[0]
    assert repairs == 2

    # The healed item is one re-approval from publishing.
    gateway = Gateway(conn, environment="sandbox")
    listing = active_listing(conn, stuck, "EBAY_US", "sandbox")
    current = gateway._proposal_from_listing(stuck, listing).content_hash()
    assert gateway.approve(stuck, current, operator=True).to_state.value == "approved"
    assert gateway._entry_preconditions(stuck, "publishing") == []

    # Reopening must not re-fire the repair.
    conn.close()
    conn = db.connect(path)
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'item.state_repaired'"
    ).fetchone()[0] == 2
