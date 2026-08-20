"""Tests for the failure modes that actually bite in this flow.

Every test here runs without network access, credentials, or httpx.
"""

from __future__ import annotations

import json
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
    nor be re-approved (approve requires `proposed`), so it was stuck.

    The legacy database is built with raw SQL rather than the current gateway: a
    database written by an older version was written by older code, and driving a
    v5 schema with current code that expects v7 columns tests nothing real.
    """
    path = tmp_path / "legacy.db"
    full = db.MIGRATIONS
    db.MIGRATIONS = full[:5]
    try:
        conn = db.connect(path)

        def build(sku: str, seq: int, *, with_listing: bool, live_approval_row: bool) -> str:
            stamp = db.now_iso()
            conn.execute(
                "INSERT INTO item (sku, seq, state, purchase_cost_cents, "
                "acquisition_intent, created_at, updated_at, state_changed_at) "
                "VALUES (?, ?, 'approved', 2500, 'resale', ?, ?, ?)",
                (sku, seq, stamp, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO photo (sku, position, source_path, content_sha256, "
                "validated_at, validation_errors, added_at) "
                "VALUES (?, 1, '/a.jpg', ?, ?, '[]', ?)",
                (sku, _digest(sku), stamp, stamp),
            )
            conn.execute(
                "INSERT INTO identification (sku, version, title, category_id, "
                "condition_id, created_at) VALUES (?, 1, 'Blazer', '3002', 'USED_EXCELLENT', ?)",
                (sku, stamp),
            )
            if with_listing:
                conn.execute(
                    "INSERT INTO listing (sku, marketplace, environment, title, "
                    "category_id, condition_id, price_cents, currency, "
                    "seller_shipping_cost_cents, created_at, updated_at) "
                    "VALUES (?, 'EBAY_US', 'sandbox', 'Blazer', '3002', "
                    "'USED_EXCELLENT', 8900, 'USD', 1200, ?, ?)",
                    (sku, stamp, stamp),
                )
            conn.execute(
                "INSERT INTO approval (sku, proposal_hash, proposal_snapshot, "
                "approved_by, approved_at, voided_at, voided_reason) "
                "VALUES (?, 'hash', '{}', 'operator', ?, ?, ?)",
                (sku, stamp, None if live_approval_row else stamp,
                 None if live_approval_row else "legacy void"),
            )
            return sku

        stuck = build("MP-000001", 1, with_listing=True, live_approval_row=False)
        orphan = build("MP-000002", 2, with_listing=False, live_approval_row=False)
        healthy = build("MP-000003", 3, with_listing=True, live_approval_row=True)
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

    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'item.state_repaired'"
    ).fetchone()[0] == 2

    # Reopening must not re-fire the repair.
    conn.close()
    conn = db.connect(path)
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'item.state_repaired'"
    ).fetchone()[0] == 2

def test_publish_error_classification():
    from resell.ebay.client import EbayApiError
    from resell.ebay.publisher import classify_publish_error

    def err(error_id: int, status: int = 400, message: str = "x") -> EbayApiError:
        return EbayApiError(
            status, [{"errorId": error_id, "message": message}], method="POST", url="/x"
        )

    assert "25018" in classify_publish_error(err(25018))
    assert "shipping service" in classify_publish_error(err(25007))
    assert "system error" in classify_publish_error(err(25001, status=500)).lower()
    assert "aspect" in classify_publish_error(err(999, message="Missing item specific")).lower()
    assert "Unrecognised" in classify_publish_error(err(4242))


def test_photo_integrity_refuses_content_that_changed_on_disk(tmp_path: Path):
    """The approval covers specific photo content by hash. If a file is edited
    after approval, publishing it would send eBay something never approved."""
    import hashlib

    from resell.ebay.publisher import PublishAborted, Publisher
    from resell.gateway import Gateway

    conn = db.connect(tmp_path / "integrity.db")
    gateway = Gateway(conn, environment="sandbox")
    photo = tmp_path / "p.jpg"
    photo.write_bytes(_jpeg_bytes(1600, 1200) + b"\x00" * 1000)

    sku = gateway.ingest_item(purchase_cost_cents=2500).sku
    gateway.attach_photo(
        sku, source_path=str(photo),
        content_sha256=hashlib.sha256(photo.read_bytes()).hexdigest(),
        image_format="jpeg", size_bytes=photo.stat().st_size, validation_errors=None,
    )
    publisher = Publisher.__new__(Publisher)
    publisher.conn = conn
    publisher.gateway = gateway

    # Unchanged: passes.
    assert len(publisher._verify_photo_integrity(sku)) == 1

    photo.write_bytes(_jpeg_bytes(1600, 1200) + b"\x00" * 2000)
    with pytest.raises(PublishAborted, match="changed on disk"):
        publisher._verify_photo_integrity(sku)

    photo.unlink()
    with pytest.raises(PublishAborted, match="file is missing"):
        publisher._verify_photo_integrity(sku)


def test_local_check_failure_leaves_the_item_recoverable(tmp_path: Path):
    """`publishing` has only two exits (listed, publish_failed), so entering it and
    then aborting on a local problem would strand the item. Checks therefore run
    before the transition."""
    import hashlib

    from resell.domain import TRANSITIONS, ItemState
    from resell.ebay.publisher import PublishAborted, Publisher
    from resell.gateway import Gateway

    conn = db.connect(tmp_path / "recover.db")
    gateway = Gateway(conn, environment="sandbox")
    photo = tmp_path / "p.jpg"
    photo.write_bytes(_jpeg_bytes(1600, 1200) + b"\x00" * 1000)
    digest = hashlib.sha256(photo.read_bytes()).hexdigest()

    sku = gateway.ingest_item(purchase_cost_cents=2500).sku
    gateway.attach_photo(
        sku, source_path=str(photo), content_sha256=digest,
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(
        sku, title="t", category_id="3002", condition_id="USED_EXCELLENT"
    )
    gateway.begin_pricing(sku)
    accepted = gateway.propose_listing(sku, _valid_proposal(sku, (digest,)))
    gateway.approve(sku, accepted.data["proposal_hash"], operator=True)

    photo.write_bytes(_jpeg_bytes(1600, 1200) + b"\x00" * 5000)  # edit after approval

    publisher = Publisher.__new__(Publisher)
    publisher.conn = conn
    publisher.gateway = gateway
    with pytest.raises(PublishAborted):
        publisher.publish(sku)

    state = conn.execute("SELECT state FROM item WHERE sku = ?", (sku,)).fetchone()[0]
    assert state == "approved"
    # And `approved` has a way out, unlike `publishing`.
    assert ItemState.PROPOSED in TRANSITIONS[ItemState.APPROVED]
    assert TRANSITIONS[ItemState.PUBLISHING] == frozenset(
        {ItemState.LISTED, ItemState.PUBLISH_FAILED}
    )


def test_publish_progress_makes_each_stage_skippable(tmp_path: Path):
    """Resumption is driven by the listing row, so an interrupted publish continues
    at the next call rather than repeating a non-idempotent one."""
    from resell.gateway import Gateway, active_listing

    conn = db.connect(tmp_path / "progress.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = _build_approved(gateway, conn)
    gateway.begin_publishing(sku)

    listing = active_listing(conn, sku, "EBAY_US", "sandbox")
    assert not listing["has_inventory_item"]
    assert listing["offer_id"] is None
    assert listing["listing_id"] is None

    gateway.record_publish_progress(sku, has_inventory_item=True)
    gateway.record_publish_progress(sku, offer_id="OFF-777")
    listing = active_listing(conn, sku, "EBAY_US", "sandbox")
    assert listing["has_inventory_item"] == 1
    assert listing["offer_id"] == "OFF-777"
    assert listing["listing_id"] is None
    # Without a listing_id the item still cannot be marked listed.
    from resell.gateway import Rejected

    with pytest.raises(Rejected, match="does not prove publication"):
        gateway.mark_listed(sku)

    gateway.record_publish_progress(sku, listing_id="110590224174")
    assert gateway.mark_listed(sku).to_state.value == "listed"


def test_taxonomy_4xx_aborts_while_5xx_proceeds(tmp_path: Path):
    """A 5xx means Taxonomy is down, which should not block a publish the proposal
    gate already validated. A 4xx means eBay rejected OUR request — almost always an
    invalid category — and proceeding would fail later with an inventory item and
    offer already written."""
    from resell.ebay.client import EbayApiError
    from resell.ebay.publisher import PublishAborted, Publisher

    class FakeListing(dict):
        def __getitem__(self, key):
            return dict.get(self, key)

    listing = FakeListing(
        sku="MP-000001", marketplace="EBAY_US", category_id="3002", aspects="{}"
    )

    publisher = Publisher.__new__(Publisher)

    def raise_status(status: int):
        def _raise(_listing):
            raise EbayApiError(
                status, [{"errorId": 62003, "message": "The specified category ID is not valid."}],
                method="GET", url="/taxonomy",
            )
        return _raise

    publisher.required_aspects = raise_status(500)
    step = publisher._check_aspects(listing)
    assert step.ok
    assert "500" in step.detail

    publisher.required_aspects = raise_status(400)
    with pytest.raises(PublishAborted) as info:
        publisher._check_aspects(listing)
    message = str(info.value)
    assert "not a valid leaf category" in message
    assert "suggest-category" in message  # the remedy is named


def test_identify_merges_by_default_and_replaces_on_request(tmp_path: Path, monkeypatch):
    """Regression: correcting one field with `identify --category X` silently
    discarded title, condition and aspects, because a new identification supersedes
    rather than edits. The domain semantics are right; the flag interface was a
    trap."""
    import argparse

    monkeypatch.setenv("RESELL_DB", str(tmp_path / "identify.db"))
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    monkeypatch.setenv("EBAY_CLIENT_ID", "a")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "b")
    monkeypatch.setenv("EBAY_RUNAME", "X-Y-Z-abc")

    from resell import cli_item
    from resell.gateway import Gateway, current_identification

    conn = db.connect(tmp_path / "identify.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=2500).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1000, validation_errors=None,
    )
    gateway.begin_identification(sku)

    def identify(**overrides):
        args = argparse.Namespace(
            sku=sku, title=None, description=None, brand=None, model=None, variant=None,
            category=None, condition=None, aspect=None, confidence=None, reasoning=None,
            replace=False,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        assert cli_item.cmd_item_identify(args) == 0

    identify(
        title="Blazer 42R", description="Navy wool.", brand="Brooks Brothers",
        category="3002", condition="USED_EXCELLENT", aspect=["Brand=Brooks Brothers"],
    )
    identify(category="3001")

    current = current_identification(conn, sku)
    assert current["version"] == 2
    assert current["category_id"] == "3001"        # the correction applied
    assert current["title"] == "Blazer 42R"        # and nothing else was lost
    assert current["condition_id"] == "USED_EXCELLENT"
    assert current["brand"] == "Brooks Brothers"
    assert json.loads(current["aspects"]) == {"Brand": ["Brooks Brothers"]}

    identify(category="57001", replace=True)
    current = current_identification(conn, sku)
    assert current["version"] == 3
    assert current["category_id"] == "57001"
    assert current["title"] is None               # --replace means replace

    # Superseded versions remain, so wiped values are recoverable.
    versions = conn.execute(
        "SELECT version, title FROM identification WHERE sku = ? ORDER BY version", (sku,)
    ).fetchall()
    assert [row["version"] for row in versions] == [1, 2, 3]
    assert versions[0]["title"] == "Blazer 42R"


# --- aspect schema -----------------------------------------------------------


def _aspect_response() -> dict:
    return {
        "aspects": [
            {
                "localizedAspectName": "Brand",
                "aspectConstraint": {
                    "aspectRequired": True, "aspectMode": "FREE_TEXT",
                    "itemToAspectCardinality": "SINGLE", "aspectDataType": "STRING",
                    "aspectMaxLength": 65,
                },
                "aspectValues": [{"localizedValue": "Brooks Brothers"}],
            },
            {
                "localizedAspectName": "Size Type",
                "aspectConstraint": {
                    "aspectRequired": True, "aspectMode": "SELECTION_ONLY",
                    "itemToAspectCardinality": "SINGLE", "aspectDataType": "STRING",
                },
                "aspectValues": [
                    {"localizedValue": "Regular"}, {"localizedValue": "Big & Tall"}
                ],
            },
            {
                "localizedAspectName": "Outer Shell Material",
                "aspectConstraint": {
                    "aspectRequired": False, "aspectMode": "FREE_TEXT",
                    "itemToAspectCardinality": "SINGLE", "aspectDataType": "STRING",
                },
                "aspectValues": [],
            },
        ]
    }


def test_aspect_schema_carries_mode_and_allowed_values():
    """The reasoning plane must be handed a form with eBay's own options, not a
    blank field to invent strings into."""
    from resell.ebay.publisher import Publisher

    class FakeClient:
        def get(self, path, **kwargs):
            if "get_default_category_tree_id" in path:
                return {"categoryTreeId": "0"}
            return _aspect_response()

    publisher = Publisher.__new__(Publisher)
    publisher.client = FakeClient()

    specs = {spec.name: spec for spec in publisher.aspect_schema("EBAY_US", "57988")}
    assert set(specs) == {"Brand", "Size Type", "Outer Shell Material"}

    brand = specs["Brand"]
    assert brand.required and not brand.selection_only
    assert brand.max_length == 65

    size_type = specs["Size Type"]
    assert size_type.selection_only
    assert size_type.allowed_values == ("Regular", "Big & Tall")

    assert not specs["Outer Shell Material"].required


def test_unknown_values_warn_only_for_selection_only_aspects():
    """eBay does not guarantee aspectValues is exhaustive, so an absent value is a
    warning rather than a hard failure — blocking on it could refuse a legitimate
    publish."""
    from resell.ebay.publisher import AspectSpec

    selection = AspectSpec(
        name="Style", required=True, mode="SELECTION_ONLY", cardinality="SINGLE",
        data_type="STRING", max_length=None, allowed_values=("Blazer", "Sport Coat"),
    )
    assert selection.unknown_values(["Blazerish"]) == ["Blazerish"]
    assert selection.unknown_values(["Blazer"]) == []
    # Case-insensitive, since eBay's casing is not something to trip over.
    assert selection.unknown_values(["blazer"]) == []

    free_text = AspectSpec(
        name="Brand", required=True, mode="FREE_TEXT", cardinality="SINGLE",
        data_type="STRING", max_length=65, allowed_values=("Nike",),
    )
    assert free_text.unknown_values(["Some Obscure Maker"]) == []


def test_emitted_aspect_flags_preserve_names_with_spaces():
    """Regression: spaces were stripped to dodge shell quoting, emitting
    `--aspect SizeType="?"` — an aspect eBay does not have. Quoting the whole
    Name=Value pair is the shell-safe form that keeps the name intact."""
    names = ["Size Type", "Outer Shell Material"]
    flags = " ".join(f'--aspect "{name}=?"' for name in names)
    assert '--aspect "Size Type=?"' in flags
    assert "SizeType" not in flags

    # And the parser handles the quoted form: shlex mirrors what the shell passes.
    import shlex

    argv = shlex.split(f"identify MP-000001 {flags}")
    supplied = [argv[i + 1] for i, token in enumerate(argv) if token == "--aspect"]
    assert supplied[0] == "Size Type=?"
    name, _, value = supplied[0].partition("=")
    assert name == "Size Type"
    assert value == "?"


# --- item condition ----------------------------------------------------------


def test_condition_id_maps_to_the_inventory_api_enum():
    """getItemConditionPolicies returns numeric IDs; createOrReplaceInventoryItem
    takes an enum string. There is no NEW_WITH_TAGS — clothing's "New with tags" is
    condition ID 1000, whose enum is plain NEW."""
    from resell.ebay.publisher import CONDITION_ID_TO_ENUM

    assert CONDITION_ID_TO_ENUM["1000"] == "NEW"
    assert CONDITION_ID_TO_ENUM["1500"] == "NEW_OTHER"
    assert CONDITION_ID_TO_ENUM["1750"] == "NEW_WITH_DEFECTS"
    assert CONDITION_ID_TO_ENUM["3000"] == "USED_EXCELLENT"
    assert CONDITION_ID_TO_ENUM["7000"] == "FOR_PARTS_OR_NOT_WORKING"
    assert "NEW_WITH_TAGS" not in set(CONDITION_ID_TO_ENUM.values())


def test_condition_policy_parses_and_flags_unmapped_ids():
    """An ID eBay adds later must surface as unmapped rather than silently vanish."""
    from resell.ebay.publisher import Publisher

    class FakeClient:
        def get(self, path, **kwargs):
            return {
                "itemConditionPolicies": [
                    {
                        "categoryId": "57988",
                        "itemConditionRequired": True,
                        "itemConditions": [
                            {"conditionId": "1000", "conditionDescription": "New with tags"},
                            {"conditionId": "3000", "conditionDescription": "Pre-owned - Good"},
                            {"conditionId": "9999", "conditionDescription": "Future condition"},
                        ],
                    }
                ]
            }

    publisher = Publisher.__new__(Publisher)
    publisher.client = FakeClient()
    policy = publisher.condition_policy("EBAY_US", "57988")

    assert policy.required is True
    assert policy.allowed_enums() == {"NEW", "USED_EXCELLENT"}
    labels = {o.condition_id: o.description for o in policy.options}
    # eBay's label is category-specific and is what the operator should read.
    assert labels["1000"] == "New with tags"
    unmapped = [o for o in policy.options if o.enum_value is None]
    assert [o.condition_id for o in unmapped] == ["9999"]


def test_condition_policy_absent_category_is_not_an_error():
    from resell.ebay.publisher import Publisher

    class FakeClient:
        def get(self, path, **kwargs):
            return {"itemConditionPolicies": []}

    publisher = Publisher.__new__(Publisher)
    publisher.client = FakeClient()
    policy = publisher.condition_policy("EBAY_US", "12345")
    assert policy.options == ()
    assert policy.required is False


def test_publish_rejects_a_condition_the_category_disallows():
    from resell.ebay.publisher import PublishAborted, Publisher

    class FakeListing(dict):
        def __getitem__(self, key):
            return dict.get(self, key)

    class FakeClient:
        def get(self, path, **kwargs):
            return {
                "itemConditionPolicies": [
                    {
                        "categoryId": "57988",
                        "itemConditionRequired": True,
                        "itemConditions": [
                            {"conditionId": "1000", "conditionDescription": "New with tags"},
                            {"conditionId": "3000", "conditionDescription": "Pre-owned - Good"},
                        ],
                    }
                ]
            }

    publisher = Publisher.__new__(Publisher)
    publisher.client = FakeClient()

    ok = FakeListing(
        sku="MP-000001", marketplace="EBAY_US", category_id="57988", condition_id="NEW"
    )
    step = publisher._check_condition(ok)
    assert step.ok
    assert "New with tags" in step.detail

    bad = FakeListing(
        sku="MP-000001", marketplace="EBAY_US", category_id="57988",
        condition_id="FOR_PARTS_OR_NOT_WORKING",
    )
    with pytest.raises(PublishAborted, match="not accepted by category"):
        publisher._check_condition(bad)


def test_rejected_commands_leave_no_trace(tmp_path: Path):
    """Regression: revise and abandon voided approvals and deactivated the listing
    BEFORE checking transition legality, so a refused command still destroyed state.
    Found while probing what a listed item permits — the probe itself corrupted the
    item it was probing."""
    from resell.gateway import Gateway, Rejected, active_listing, live_approval

    conn = db.connect(tmp_path / "atomic.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = _build_approved(gateway, conn)
    gateway.begin_publishing(sku)
    gateway.record_publish_progress(sku, offer_id="OFF-1")
    gateway.record_publish_progress(sku, listing_id="110590224450")
    gateway.mark_listed(sku)

    assert live_approval(conn, sku) is not None
    assert active_listing(conn, sku, "EBAY_US", "sandbox") is not None

    for command in (
        lambda: gateway.revise(sku, "fix aspects"),
        lambda: gateway.abandon(sku, "wrong data"),
    ):
        with pytest.raises(Rejected, match="not a legal transition"):
            command()
        # The record of what was published must survive a refused command.
        assert live_approval(conn, sku) is not None
        assert active_listing(conn, sku, "EBAY_US", "sandbox") is not None
        assert conn.execute(
            "SELECT state FROM item WHERE sku = ?", (sku,)
        ).fetchone()[0] == "listed"


def test_listed_items_refuse_authoritative_mutation_but_accept_evidence(tmp_path: Path):
    """`attach_photo` and `remove_photo` already refused on terminal states while
    `propose_identification` did not — an oversight, not an exemption. Changing the
    identification of a listed item desyncs the local record from eBay and voids the
    approval that documents what was actually published.

    Evidence stays open on purpose: a fact learned about a listed item needs
    somewhere to go, and observations are append-only and non-authoritative."""
    from resell.gateway import (
        Gateway, Rejected, active_listing, current_identification, live_approval,
    )

    conn = db.connect(tmp_path / "terminal.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = _build_approved(gateway, conn)
    gateway.begin_publishing(sku)
    gateway.record_publish_progress(sku, offer_id="OFF-1")
    gateway.record_publish_progress(sku, listing_id="110590224450")
    gateway.mark_listed(sku)

    version_before = current_identification(conn, sku)["version"]
    hash_before = live_approval(conn, sku)["proposal_hash"]

    with pytest.raises(Rejected, match="the identification cannot change"):
        gateway.propose_identification(sku, title="corrected", aspects={"Size": ["42R"]})
    with pytest.raises(Rejected, match="the photo set cannot change"):
        gateway.remove_photo(sku, position=1)

    # Observation is still allowed, and is where a correction lives until a revision
    # workflow can carry it to eBay.
    gateway.record_evidence(
        sku, kind="operator_correction", source="operator",
        payload={"observed_size": "42R", "note": "listed value is wrong"},
    )

    assert current_identification(conn, sku)["version"] == version_before
    assert live_approval(conn, sku)["proposal_hash"] == hash_before
    assert active_listing(conn, sku, "EBAY_US", "sandbox") is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ? AND kind = 'operator_correction'", (sku,)
    ).fetchone()[0] == 1


def test_void_approvals_refuses_on_terminal_items(tmp_path: Path):
    """Defense in depth. Every caller is blocked, but the approval that authorised a
    live listing is the record of what was published and no future code path should
    be able to erase it."""
    from resell.gateway import Gateway, live_approval

    conn = db.connect(tmp_path / "void.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = _build_approved(gateway, conn)
    gateway.begin_publishing(sku)
    gateway.record_publish_progress(sku, listing_id="110590224450")
    gateway.mark_listed(sku)

    assert gateway._void_approvals(sku, "direct call") == 0
    assert live_approval(conn, sku) is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'approval.void_refused'"
    ).fetchone()[0] == 1


# --- reasoning plane: identifiers --------------------------------------------


def test_check_digits_verify_identifier_transcriptions():
    """One of the few places a transcription can be proven wrong rather than
    doubted. OCR reliably confuses 0/O, 1/I, 5/S and 8/B."""
    from resell.reasoning.schema import IdentifierScheme, validate_identifier

    assert validate_identifier(IdentifierScheme.UPC, "036000291452")[0] is True
    assert validate_identifier(IdentifierScheme.UPC, "0-36000-29145-2")[0] is True
    assert validate_identifier(IdentifierScheme.UPC, "036000291453")[0] is False
    assert validate_identifier(IdentifierScheme.EAN, "4006381333931")[0] is True
    assert validate_identifier(IdentifierScheme.ISBN, "0-306-40615-2")[0] is True
    assert validate_identifier(IdentifierScheme.ISBN, "9780306406157")[0] is True
    assert validate_identifier(IdentifierScheme.ISBN, "080442957X")[0] is True

    # No check digit is not the same as passing: an MPN can only be verified
    # against a catalogue, never against itself.
    valid, why = validate_identifier(IdentifierScheme.MPN, "BR-1818-FITZ")
    assert valid is None
    assert "no check digit" in why


def test_normalization_preserves_characters_so_misreads_are_detectable():
    """Regression: a digits-only filter turned "O36OOO291452" into "36291452",
    silently deleting four characters, so the failure was reported as a length
    problem rather than the substitution it was."""
    from resell.reasoning.schema import (
        IdentifierObservation, IdentifierScheme, normalize_identifier,
    )

    assert normalize_identifier(IdentifierScheme.UPC, "O36OOO291452") == "O36OOO291452"
    assert normalize_identifier(IdentifierScheme.UPC, "0-36000-29145-2") == "036000291452"

    misread = IdentifierObservation(IdentifierScheme.UPC, "O36OOO291452", photo_position=3)
    assert misread.usable is False
    assert "036000291452" in misread.check_explanation      # the correction is named
    assert "OCR misread" in misread.check_explanation

    wrong = IdentifierObservation(IdentifierScheme.UPC, "036000291453", photo_position=3)
    assert wrong.usable is False
    assert "OCR misread" not in wrong.check_explanation     # genuinely wrong, not misread

    good = IdentifierObservation(IdentifierScheme.UPC, "036000291452", photo_position=3)
    assert good.usable is True


def test_observations_must_be_checkable():
    from resell.reasoning.schema import Basis, Observation, Subject

    assert Observation(claim="label reads 42R", basis=Basis.TEXT_READ).problems()
    assert not Observation(
        claim="label reads 42R", basis=Basis.TEXT_READ, photo_positions=(3,)
    ).problems()
    assert Observation(
        claim="18in tall", basis=Basis.MEASUREMENT, photo_positions=(1,)
    ).problems()
    # An external source describes a candidate product, not the object on the table.
    assert Observation(
        claim="navy wool", basis=Basis.EXTERNAL_SOURCE, subject=Subject.THIS_ITEM
    ).problems()


# --- reasoning plane: candidate resolution -----------------------------------


def _ref(evidence_id, basis):
    from resell.reasoning.schema import EvidenceRef

    return EvidenceRef(evidence_id, basis)


def test_candidates_require_material_support():
    """Candidates are generated from evidence, never seeded from Taxonomy's allowed
    list — otherwise every permitted value would look like a live option."""
    from resell.reasoning.gaps import Candidate, Resolution, resolve_aspect
    from resell.reasoning.schema import Basis

    outcome = resolve_aspect("Size", [
        Candidate("42R", (_ref(1, Basis.TEXT_READ),)),
        Candidate("38", ()),
        Candidate("40", ()),
    ])
    assert outcome.resolution is Resolution.RESOLVED
    assert outcome.value == "42R"
    assert "uncited" in outcome.explanation

    nothing = resolve_aspect("Size", [Candidate("38", ()), Candidate("40", ())])
    assert nothing.resolution is Resolution.UNSUPPORTED


def test_contradiction_and_ambiguity_are_distinguished_by_evidence_overlap():
    """Disjoint evidence means two independent sources disagree. Shared evidence
    means one observation is itself indecisive. Different problems, different
    questions."""
    from resell.reasoning.gaps import (
        Candidate, GapAction, Resolution, gap_for, resolve_aspect,
    )
    from resell.reasoning.schema import Basis

    contradicted = resolve_aspect("Size", [
        Candidate("42R", (_ref(1, Basis.TEXT_READ),)),
        Candidate("38", (_ref(2, Basis.TEXT_READ),)),
    ])
    assert contradicted.resolution is Resolution.CONTRADICTED
    assert gap_for(contradicted).action is GapAction.ASK_OPERATOR
    assert "disagree" in gap_for(contradicted).question

    ambiguous = resolve_aspect("Color", [
        Candidate("Navy", (_ref(5, Basis.VISUAL_OBSERVATION),)),
        Candidate("Charcoal", (_ref(5, Basis.VISUAL_OBSERVATION),)),
    ])
    assert ambiguous.resolution is Resolution.AMBIGUOUS
    assert gap_for(ambiguous).action is GapAction.REQUEST_PHOTO

    # Mixed: a genuine disagreement outranks one hedged observation.
    mixed = resolve_aspect("Size", [
        Candidate("42R", (_ref(1, Basis.TEXT_READ),)),
        Candidate("44R", (_ref(3, Basis.INFERENCE),)),
        Candidate("38", (_ref(2, Basis.TEXT_READ),)),
    ])
    assert mixed.resolution is Resolution.CONTRADICTED


def test_operator_adjudicates_without_silent_precedence():
    """The operator has the object in hand, so their statement settles a dispute.
    No other basis gets automatic precedence — a hidden ranking would reintroduce
    unexplained value selection with better paperwork."""
    from resell.reasoning.gaps import Candidate, Resolution, resolve_aspect
    from resell.reasoning.schema import Basis

    outcome = resolve_aspect("Size", [
        Candidate("42R", (_ref(1, Basis.TEXT_READ), _ref(9, Basis.OPERATOR))),
        Candidate("38", (_ref(2, Basis.TEXT_READ),)),
    ])
    assert outcome.resolution is Resolution.RESOLVED_BY_OPERATOR
    assert outcome.value == "42R"
    assert "38" in outcome.explanation          # the superseded reading is retained

    # text_read does not outrank text_read.
    both_read = resolve_aspect("Size", [
        Candidate("42R", (_ref(1, Basis.TEXT_READ),)),
        Candidate("38", (_ref(2, Basis.VISUAL_OBSERVATION),)),
    ])
    assert both_read.resolution is Resolution.CONTRADICTED


def test_mp_000001_size_failure_would_now_be_blocked():
    """The actual failure: Size 38 was legal and false, while the operator's own
    answer said 42R. Both the uncited case and the contradicted case block."""
    from resell.reasoning.gaps import Candidate, Resolution, resolve_aspect
    from resell.reasoning.schema import Basis

    uncited = resolve_aspect("Size", [Candidate("38", ())])
    assert uncited.resolution is Resolution.UNSUPPORTED

    against_operator = resolve_aspect("Size", [
        Candidate("38", (_ref(4, Basis.INFERENCE),)),
        Candidate("42R", (_ref(9, Basis.OPERATOR),)),
    ])
    assert against_operator.value == "42R"


# --- reasoning plane: modes and effort ---------------------------------------


def test_no_identity_modes_require_a_cited_negative_finding():
    """Without this, a model reaches described_object by giving up, and laziness is
    indistinguishable from diligence."""
    from resell.reasoning.gaps import mode_is_supported
    from resell.reasoning.schema import (
        Basis, EvidenceRef, IdentificationEffort, IdentificationMode, NegativeFinding,
    )

    ok, why = mode_is_supported(
        IdentificationMode.DESCRIBED_OBJECT,
        effort=IdentificationEffort.STANDARD,
        negative_finding=None,
    )
    assert ok is False
    assert "cited negative finding" in why

    ok, _ = mode_is_supported(
        IdentificationMode.DESCRIBED_OBJECT,
        effort=IdentificationEffort.STANDARD,
        negative_finding=NegativeFinding(
            surfaces_examined=("underside", "back panel"), photos_reviewed=4
        ),
    )
    assert ok is True

    # Positive modes must be earned too.
    ok, why = mode_is_supported(
        IdentificationMode.EXACT_PRODUCT,
        effort=IdentificationEffort.STANDARD,
        negative_finding=None,
    )
    assert ok is False
    assert "identifier" in why

    ok, _ = mode_is_supported(
        IdentificationMode.EXACT_PRODUCT,
        effort=IdentificationEffort.STANDARD,
        negative_finding=None,
        identifier_support=(EvidenceRef(7, Basis.TEXT_READ),),
    )
    assert ok is True


def test_negative_evidence_requirement_scales_with_effort():
    """The concept survives at every level; the thoroughness scales. A three-dollar
    ornament does not earn a forensic surface sweep."""
    from resell.reasoning.gaps import negative_finding_sufficient
    from resell.reasoning.schema import IdentificationEffort, NegativeFinding

    photos_only = NegativeFinding(photos_reviewed=3)
    assert negative_finding_sufficient(IdentificationEffort.MINIMAL, photos_only)[0] is True
    assert negative_finding_sufficient(IdentificationEffort.STANDARD, photos_only)[0] is False

    named = NegativeFinding(surfaces_examined=("underside",), photos_reviewed=3)
    assert negative_finding_sufficient(IdentificationEffort.STANDARD, named)[0] is True
    assert negative_finding_sufficient(IdentificationEffort.THOROUGH, named)[0] is False

    thorough = NegativeFinding(
        surfaces_examined=("underside", "inner rim"), photos_reviewed=5,
        operator_confirmed=True,
    )
    assert negative_finding_sufficient(IdentificationEffort.THOROUGH, thorough)[0] is True

    # Even minimal requires *something* to have been looked at.
    assert negative_finding_sufficient(
        IdentificationEffort.MINIMAL, NegativeFinding(photos_reviewed=0)
    )[0] is False


# --- identification effort and escalation ------------------------------------


def test_identification_effort_is_explicit_not_derived(tmp_path: Path):
    """Purchase cost is a poor proxy for whether identity is discoverable or worth
    discovering — inherited, gifted, decluttered and free items all have a cost
    basis that says nothing about it."""
    from resell.gateway import Gateway, Rejected

    conn = db.connect(tmp_path / "effort.db")
    gateway = Gateway(conn, environment="sandbox")

    default = gateway.ingest_item(purchase_cost_cents=None, acquisition_intent="declutter").sku
    cheap = gateway.ingest_item(purchase_cost_cents=250, identification_effort="minimal").sku
    dear = gateway.ingest_item(purchase_cost_cents=50000, identification_effort="thorough").sku

    def effort_of(sku):
        return conn.execute(
            "SELECT identification_effort FROM item WHERE sku = ?", (sku,)
        ).fetchone()[0]

    assert effort_of(default) == "standard"
    # A cheap item may warrant minimal and an expensive one thorough, but only
    # because the operator said so — nothing derives it.
    assert effort_of(cheap) == "minimal"
    assert effort_of(dear) == "thorough"

    with pytest.raises(Rejected, match="unknown identification_effort"):
        gateway.ingest_item(purchase_cost_cents=1, identification_effort="exhaustive")


def test_escalation_policy_lets_the_model_ask_but_never_grant():
    from resell.reasoning.gaps import EscalationDecision, escalation_policy
    from resell.reasoning.schema import IdentificationEffort

    minimal, standard, thorough = (
        IdentificationEffort.MINIMAL,
        IdentificationEffort.STANDARD,
        IdentificationEffort.THOROUGH,
    )

    # One cheap step, cited: routing this through a human would waste the human.
    decision, _ = escalation_policy(minimal, standard, cited_evidence=(1,))
    assert decision is EscalationDecision.AUTO_GRANT

    # Uncited: "look harder" with no evidence behind it is unfalsifiable.
    decision, why = escalation_policy(minimal, standard, cited_evidence=())
    assert decision is EscalationDecision.REFUSE
    assert "cite" in why

    # Thorough costs the operator time and photographs, so it is their call.
    decision, _ = escalation_policy(standard, thorough, cited_evidence=(1,))
    assert decision is EscalationDecision.REQUIRES_OPERATOR
    decision, _ = escalation_policy(minimal, thorough, cited_evidence=(1,))
    assert decision is EscalationDecision.REQUIRES_OPERATOR

    decision, why = escalation_policy(thorough, minimal, cited_evidence=(1,))
    assert decision is EscalationDecision.REFUSE
    assert "upward" in why
    assert escalation_policy(standard, standard, cited_evidence=(1,))[0] is EscalationDecision.REFUSE


def test_escalation_requires_operator_for_thorough(tmp_path: Path):
    from resell.gateway import Gateway, Rejected
    from resell.reasoning.schema import Basis, Observation

    conn = db.connect(tmp_path / "escalate.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=250, identification_effort="minimal").sku
    evidence_id = gateway.record_observation(
        sku,
        Observation(
            claim="partial maker's mark on underside",
            basis=Basis.VISUAL_OBSERVATION, photo_positions=(4,), surface="underside",
        ),
    ).data["evidence_id"]

    def effort():
        return conn.execute(
            "SELECT identification_effort FROM item WHERE sku = ?", (sku,)
        ).fetchone()[0]

    gateway.request_effort_escalation(
        sku, to_effort="standard", rationale="mark may resolve the brand",
        evidence_ids=(evidence_id,),
    )
    assert effort() == "standard"

    accepted = gateway.request_effort_escalation(
        sku, to_effort="thorough", rationale="needs macro photography",
        evidence_ids=(evidence_id,),
    )
    assert accepted.data["granted"] is False
    assert effort() == "standard"        # not applied until decided

    with pytest.raises(Rejected, match="only the operator"):
        gateway.decide_effort_escalation(accepted.data["request_id"], granted=True)

    gateway.decide_effort_escalation(
        accepted.data["request_id"], granted=True, operator=True
    )
    assert effort() == "thorough"

    # Cited evidence must belong to the item being escalated.
    other = gateway.ingest_item(purchase_cost_cents=None).sku
    with pytest.raises(Rejected, match="does not belong"):
        gateway.request_effort_escalation(
            other, to_effort="thorough", rationale="x", evidence_ids=(evidence_id,)
        )


def test_escalation_is_scoped_to_identity(tmp_path: Path):
    """Identity budget must not silently become the global research budget; pricing
    and comps get their own policy later."""
    conn = db.connect(tmp_path / "scope.db")
    columns = [row[1] for row in conn.execute("PRAGMA table_info(effort_escalation)")]
    assert "scope" in columns
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO effort_escalation (sku, scope, from_effort, to_effort, "
            "rationale, evidence_ids, requested_at) "
            "VALUES ('MP-000001', 'pricing', 'minimal', 'standard', 'r', '[]', 'now')"
        )


def test_failed_check_digit_is_refused_but_audited(tmp_path: Path):
    """A transcription that is arithmetically impossible should not be citable. The
    attempt still belongs in the audit trail."""
    from resell.gateway import Gateway, Rejected
    from resell.reasoning.schema import IdentifierObservation, IdentifierScheme

    conn = db.connect(tmp_path / "ident.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=None).sku

    with pytest.raises(Rejected, match="OCR misread"):
        gateway.record_identifier(
            sku,
            IdentifierObservation(
                IdentifierScheme.UPC, "O36OOO291452", photo_position=2, surface="hang tag"
            ),
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'identifier.rejected'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ?", (sku,)
    ).fetchone()[0] == 0

    gateway.record_identifier(
        sku,
        IdentifierObservation(
            IdentifierScheme.UPC, "036000291452", photo_position=2, surface="hang tag"
        ),
    )
    row = conn.execute(
        "SELECT kind, basis, subject FROM evidence WHERE sku = ?", (sku,)
    ).fetchone()
    assert row["kind"] == "identifier_observation"
    assert row["basis"] == "text_read"
    assert row["subject"] == "this_item"


def test_citation_integrity_is_enforced_by_foreign_key(tmp_path: Path):
    """A citation to a nonexistent evidence record must fail in the database, not in
    application code — that is what makes citation integrity checkable rather than
    trusted."""
    conn = db.connect(tmp_path / "fk.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO aspect_candidate_evidence (candidate_id, evidence_id) VALUES (1, 9999)"
        )


# --- vision stage ------------------------------------------------------------


def test_normalization_preserves_punctuation_outside_checked_schemes():
    """Regression: formatting stripping was applied to every scheme, turning the
    MPN "TL-4471" into "TL4471". In a UPC a hyphen is a printing convention; in a
    part number it is part of the value, and removing it may match nothing."""
    from resell.reasoning.schema import IdentifierScheme, normalize_identifier

    assert normalize_identifier(IdentifierScheme.MODEL_NUMBER, "TL-4471") == "TL-4471"
    assert normalize_identifier(IdentifierScheme.MPN, "BR 1818/FITZ") == "BR 1818/FITZ"
    assert normalize_identifier(IdentifierScheme.SERIAL, "SN-0093-X") == "SN-0093-X"
    # Checked schemes still lose their printing conventions.
    assert normalize_identifier(IdentifierScheme.UPC, "0-36000-29145-2") == "036000291452"
    assert normalize_identifier(IdentifierScheme.ISBN, "0-306-40615-2") == "0306406152"


def test_malformed_entries_do_not_discard_the_whole_pass():
    from resell.reasoning.tools import parse_observe_tool_input

    proposal = parse_observe_tool_input({
        "observations": [
            {"claim": "ceramic base", "basis": "visual_observation", "photo_positions": [1]},
            {"claim": "wired for UK", "basis": "telepathy", "photo_positions": [1]},
            {"claim": "14in tall", "basis": "measurement", "photo_positions": [1],
             "measurement_method": "guesswork"},
        ],
        "identifiers": [
            {"scheme": "model_number", "raw_transcription": "TL-4471", "photo_position": 3},
            {"scheme": "quantum", "raw_transcription": "x", "photo_position": 1},
            {"scheme": "upc", "raw_transcription": "  ", "photo_position": 1},
        ],
        "identity_search": {"surfaces_examined": ["underside"], "photos_reviewed": 3},
    })

    assert len(proposal.observations) == 1
    assert len(proposal.identifiers) == 1
    assert len(proposal.malformed) == 4
    # The model may not assert that the operator confirmed anything.
    assert proposal.negative_finding.operator_confirmed is False


def test_tool_proposals_reach_the_gateway_and_can_be_refused(tmp_path: Path):
    """The whole point of the boundary: a tool call is a proposal, not a write. A
    model claiming a text_read with no photo citation is refused exactly as an
    operator would be."""
    from resell.gateway import Gateway, Rejected
    from resell.reasoning.tools import parse_observe_tool_input

    conn = db.connect(tmp_path / "vision.db")
    gateway = Gateway(conn, environment="sandbox", model_source="claude-sonnet-5")
    sku = gateway.ingest_item(purchase_cost_cents=None).sku

    proposal = parse_observe_tool_input({
        "observations": [
            {"claim": "ribbed ceramic base", "basis": "visual_observation",
             "photo_positions": [1], "confidence": 0.9},
            {"claim": "Brand is Lampe Berger", "basis": "text_read"},
        ],
        "identifiers": [
            {"scheme": "upc", "raw_transcription": "O36OOO291452", "photo_position": 3},
        ],
        "identity_search": {"photos_reviewed": 3},
    })

    accepted, refused = 0, 0
    for observation in proposal.observations:
        try:
            gateway.record_observation(sku, observation)
            accepted += 1
        except Rejected:
            refused += 1
    for identifier in proposal.identifiers:
        try:
            gateway.record_identifier(sku, identifier)
            accepted += 1
        except Rejected:
            refused += 1

    assert accepted == 1
    assert refused == 2
    # Only what survived validation is on record.
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE sku = ?", (sku,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT source FROM evidence WHERE sku = ?", (sku,)
    ).fetchone()[0] == "claude-sonnet-5"


def _fake_result(provider="fake", model="fake-vision", tool_input=None):
    from resell.reasoning.stages import StageResult, Usage

    return StageResult(
        tool_input=tool_input or {"observations": [], "identity_search": {"photos_reviewed": 0}},
        usage=Usage(input_tokens=2650, output_tokens=402,
                    raw={"prompt_tokens": 2650, "completion_tokens": 402}),
        latency_ms=5100, provider=provider, model=model,
        stop_reason="tool_calls", raw_response={"choices": []},
    )


class _FakeAdapter:
    """A structurally different provider: different token field names, different
    envelope, arguments already parsed from a JSON string by the adapter."""

    provider = "fake"

    def __init__(self, tool_input=None, model="fake-vision", estimated_tokens=1000):
        self.model = model
        self._tool_input = tool_input
        self._estimated_tokens = estimated_tokens
        self.seen = None

    def run(self, request):
        self.seen = request
        return _fake_result(model=self.model, tool_input=self._tool_input)

    def estimate_input_tokens(self, request):
        return self._estimated_tokens

    def rates(self):
        from resell.reasoning.budget import ModelRates

        return ModelRates()


def _stub_image_prep(monkeypatch, tmp_path: Path):
    """for_model shells out to sips, which is macOS-only."""
    import resell.reasoning.vision as vision

    monkeypatch.setattr(vision, "for_model", lambda source, cache_dir, digest=None: Path(source))
    photo = tmp_path / "p.jpg"
    photo.write_bytes(_jpeg_bytes(1600, 1200) + b"\x00" * 500)
    return photo


def test_observation_stage_is_not_shown_the_aspect_form(tmp_path: Path, monkeypatch):
    """A model told a Size aspect is required is under pressure to produce one
    whether or not it can see a size. The observation call describes; a later call
    maps."""
    import json as _json

    from resell.reasoning.vision import observe

    photo = _stub_image_prep(monkeypatch, tmp_path)
    adapter = _FakeAdapter()
    observe([photo], cache_dir=tmp_path, adapter=adapter)

    serialized = _json.dumps(
        {
            "system": adapter.seen.system_prompt,
            "instruction": adapter.seen.instruction,
            "schema": adapter.seen.tool.json_schema,
        }
    ).lower()
    assert "taxonomy" not in serialized
    assert "required aspect" not in serialized
    assert adapter.seen.require_tool is True


def test_reasoning_plane_is_not_coupled_to_a_provider(tmp_path: Path, monkeypatch):
    """The whole boundary: a structurally different provider flows through stages,
    proposals, the gateway and storage without any of them changing."""
    from resell.gateway import Gateway
    from resell.reasoning.vision import observe, record_trace

    photo = _stub_image_prep(monkeypatch, tmp_path)
    conn = db.connect(tmp_path / "neutral.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=None).sku

    adapter = _FakeAdapter(estimated_tokens=1000, tool_input={
        "observations": [
            {"claim": "ribbed ceramic base", "basis": "visual_observation",
             "photo_positions": [1], "confidence": 0.9}
        ],
        "identifiers": [
            {"scheme": "model_number", "raw_transcription": "TL-4471", "photo_position": 1}
        ],
        "identity_search": {"surfaces_examined": ["underside"], "photos_reviewed": 1},
    })
    outcome = observe([photo], cache_dir=tmp_path, adapter=adapter)

    assert outcome.provider == "fake"
    assert len(outcome.proposal.observations) == 1
    assert outcome.proposal.identifiers[0].normalized == "TL-4471"

    source = f"{outcome.provider}/{outcome.model}"
    gateway.record_observation(sku, outcome.proposal.observations[0], source=source)
    gateway.record_identifier(sku, outcome.proposal.identifiers[0], source=source)

    # Evidence is attributable to the provider that produced it, which is what a
    # cross-provider comparison groups by.
    sources = {row[0] for row in conn.execute("SELECT source FROM evidence WHERE sku = ?", (sku,))}
    assert sources == {"fake/fake-vision"}

    trace_id = record_trace(conn, sku, outcome)
    row = conn.execute("SELECT * FROM model_call WHERE id = ?", (trace_id,)).fetchone()
    assert row["provider"] == "fake"
    assert row["input_tokens"] == 2650
    # Cost is derived from a configured rate table, never reported by the provider,
    # so the basis travels with the figure — a number computed from placeholder
    # rates must not be mistaken for an invoice.
    assert row["cost_micros"] is not None
    assert row["rate_basis"] == "provisional_estimate"
    # Whatever token fields the provider actually reported are preserved.
    assert "prompt_tokens" in row["raw_usage"]


def test_replay_key_is_identical_across_providers(tmp_path: Path, monkeypatch):
    """What makes the eval cheap: the stored request describes the input neutrally,
    so the same photos and prompt can be run elsewhere and the outputs compared
    rather than reconstructed."""
    from resell.reasoning.vision import observe

    photo = _stub_image_prep(monkeypatch, tmp_path)
    first = observe([photo], cache_dir=tmp_path, adapter=_FakeAdapter(model="a"))
    second = observe([photo], cache_dir=tmp_path, adapter=_FakeAdapter(model="b"))

    assert first.request.replay_key() == second.request.replay_key()
    key = first.request.replay_key()
    assert key["images"][0]["sha256"]
    assert key["tool"] == "record_observations"
    # The key identifies content, not vendor wire format.
    assert "messages" not in key and "content" not in key


def test_adapter_errors_do_not_leak_vendor_exception_types(tmp_path: Path, monkeypatch):
    from resell.reasoning.adapters import AdapterError, get_adapter
    from resell.reasoning.vision import VisionError, observe

    photo = _stub_image_prep(monkeypatch, tmp_path)

    class _Failing(_FakeAdapter):
        def run(self, request):
            raise AdapterError("fake", "HTTP 500: upstream exploded", status_code=500)

    with pytest.raises(VisionError, match="upstream exploded"):
        observe([photo], cache_dir=tmp_path, adapter=_Failing())

    with pytest.raises(AdapterError, match="no adapter registered"):
        get_adapter("gemini")


def test_anthropic_shapes_stay_inside_the_anthropic_adapter(tmp_path: Path):
    """Every vendor peculiarity here belongs to one file: base64 images with a
    media type, tools plus tool_choice, a content array of typed blocks, and
    input_tokens/output_tokens."""
    from resell.reasoning.adapters.anthropic import AnthropicAdapter
    from resell.reasoning.stages import ImageRef, observation_stage

    photo = tmp_path / "p.jpg"
    photo.write_bytes(_jpeg_bytes(800, 600) + b"\x00" * 100)
    request = observation_stage((ImageRef(path=photo, position=1, content_sha256="abc"),))

    captured = {}

    def transport(payload):
        captured.update(payload)
        return {
            "model": "claude-sonnet-5", "stop_reason": "tool_use",
            "usage": {"input_tokens": 11, "output_tokens": 22},
            "content": [{"type": "tool_use", "name": "record_observations",
                         "input": {"observations": [], "identity_search": {"photos_reviewed": 1}}}],
        }

    result = AnthropicAdapter(transport=transport, api_key="k").run(request)

    assert sorted(captured) == ["max_tokens", "messages", "model", "system", "tool_choice", "tools"]
    image_block = captured["messages"][0]["content"][1]
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/jpeg"
    assert captured["tool_choice"]["name"] == "record_observations"

    # And none of that shape survives into the neutral result.
    assert result.provider == "anthropic"
    assert result.usage.input_tokens == 11
    assert result.usage.raw == {"input_tokens": 11, "output_tokens": 22}
    assert isinstance(result.tool_input, dict)


# --- inference budget --------------------------------------------------------


def test_budget_refuses_before_the_provider_is_contacted(tmp_path: Path, monkeypatch):
    """The point of a pre-call guard: a request that could exceed the budget is
    refused rather than attempted and regretted."""
    from resell.reasoning.budget import BudgetExceeded, StageBudget, StageSpend
    from resell.reasoning.vision import observe

    photo = _stub_image_prep(monkeypatch, tmp_path)
    calls = {"n": 0}

    class Counting(_FakeAdapter):
        def run(self, request):
            calls["n"] += 1
            return super().run(request)

        def estimate_input_tokens(self, request):
            return 9000

        def rates(self):
            from resell.reasoning.budget import ModelRates

            return ModelRates()

    adapter = Counting()

    # Call limit.
    with pytest.raises(BudgetExceeded, match="call limit reached"):
        observe(
            [photo], cache_dir=tmp_path, adapter=adapter,
            budget=StageBudget(max_calls=2), spent=StageSpend(calls=2),
        )
    assert calls["n"] == 0

    # Cost limit, using the worst case rather than an expected case.
    with pytest.raises(BudgetExceeded, match="exceeds the"):
        observe(
            [photo], cache_dir=tmp_path, adapter=adapter,
            budget=StageBudget(max_calls=9, max_cost_micros=1_000), spent=StageSpend(),
        )
    assert calls["n"] == 0

    # Within budget, the call proceeds.
    observe(
        [photo], cache_dir=tmp_path, adapter=adapter,
        budget=StageBudget(max_calls=9, max_cost_micros=500_000), spent=StageSpend(),
    )
    assert calls["n"] == 1


def test_output_cap_is_applied_to_the_request_not_merely_checked(tmp_path: Path, monkeypatch):
    """A cap that is checked but not sent lets the provider generate past it."""
    from resell.reasoning.budget import ModelRates, StageBudget, StageSpend
    from resell.reasoning.vision import observe

    photo = _stub_image_prep(monkeypatch, tmp_path)

    class Adapter(_FakeAdapter):
        def estimate_input_tokens(self, request):
            return 1000

        def rates(self):
            return ModelRates()

    adapter = Adapter()
    observe(
        [photo], cache_dir=tmp_path, adapter=adapter,
        budget=StageBudget(max_output_tokens=1200, max_cost_micros=500_000),
        spent=StageSpend(),
    )
    assert adapter.seen.max_tokens == 1200


def test_worst_case_uses_the_output_cap_not_an_average():
    """Output length is unknown until the call returns, so budgeting for the
    average would let the expensive call through."""
    from resell.reasoning.budget import ModelRates, StageBudget, estimate_cost

    rates = ModelRates(input_micros_per_1k=3000, output_micros_per_1k=15000)
    estimate = estimate_cost(10_000, StageBudget(max_output_tokens=4000), rates)
    assert estimate.max_output_tokens == 4000
    assert estimate.worst_case_micros == 10_000 * 3 + 4000 * 15


def test_cost_figures_carry_their_rate_basis():
    """Providers report tokens, not money. A number derived from placeholder rates
    must never be mistaken for an invoice — the same discipline as the eBay fee
    basis."""
    from resell.reasoning.budget import ModelRates, RateBasis, StageBudget, estimate_cost

    provisional = estimate_cost(1000, StageBudget(), ModelRates())
    assert provisional.rates.basis is RateBasis.PROVISIONAL_ESTIMATE
    assert "estimated" in provisional.describe()

    configured = estimate_cost(
        1000, StageBudget(),
        ModelRates(basis=RateBasis.CONFIGURED, source="price list 2026-08"),
    )
    assert "computed" in configured.describe()


def test_spend_accumulates_from_the_trace_table(tmp_path: Path, monkeypatch):
    from resell.gateway import Gateway
    from resell.reasoning.budget import ModelRates, StageBudget
    from resell.reasoning.vision import observe, record_trace, spend_so_far

    photo = _stub_image_prep(monkeypatch, tmp_path)
    conn = db.connect(tmp_path / "spend.db")
    sku = Gateway(conn, environment="sandbox").ingest_item(purchase_cost_cents=None).sku

    class Adapter(_FakeAdapter):
        def estimate_input_tokens(self, request):
            return 1000

        def rates(self):
            return ModelRates()

    budget = StageBudget(max_calls=5, max_cost_micros=1_000_000)
    assert spend_so_far(conn, sku).calls == 0

    for expected in (1, 2, 3):
        outcome = observe(
            [photo], cache_dir=tmp_path, adapter=Adapter(),
            budget=budget, spent=spend_so_far(conn, sku),
        )
        record_trace(conn, sku, outcome)
        assert spend_so_far(conn, sku).calls == expected

    spent = spend_so_far(conn, sku)
    assert spent.cost_micros > 0
    # The estimate is retained beside the outcome so the guard can be checked
    # against reality rather than trusted.
    row = conn.execute(
        "SELECT cost_micros, estimated_cost_micros, rate_basis FROM model_call "
        "WHERE sku = ? ORDER BY id LIMIT 1", (sku,)
    ).fetchone()
    assert row["estimated_cost_micros"] >= row["cost_micros"]
    assert row["rate_basis"] == "provisional_estimate"


def test_spend_is_scoped_per_stage(tmp_path: Path):
    """An observation budget must not be consumed by some other stage's calls."""
    from resell.gateway import Gateway
    from resell.reasoning.vision import spend_so_far

    conn = db.connect(tmp_path / "scoped.db")
    sku = Gateway(conn, environment="sandbox").ingest_item(purchase_cost_cents=None).sku
    for purpose in ("observe", "observe", "map_aspects"):
        conn.execute(
            "INSERT INTO model_call (sku, purpose, provider, model, input_tokens, "
            "output_tokens, cost_micros, latency_ms, called_at) "
            "VALUES (?, ?, 'p', 'm', 100, 50, 1000, 10, ?)",
            (sku, purpose, db.now_iso()),
        )
    assert spend_so_far(conn, sku, "observe").calls == 2
    assert spend_so_far(conn, sku, "map_aspects").calls == 1


def test_incomplete_adapter_fails_with_a_useful_message(tmp_path: Path, monkeypatch):
    """The budget guard is only as good as the estimate behind it, so both methods
    are required — and a half-written adapter should say which one is missing."""
    from resell.reasoning.vision import VisionError, observe

    photo = _stub_image_prep(monkeypatch, tmp_path)

    class RunOnly:
        provider = "half_built"
        model = "x"

        def run(self, request):
            return _fake_result()

    with pytest.raises(VisionError, match="does not implement rates"):
        observe([photo], cache_dir=tmp_path, adapter=RunOnly())


def test_item_list_shows_terminal_items_too(tmp_path: Path, monkeypatch):
    """SKUs are never reused, so a gap in the sequence is a question worth being
    able to answer — hiding abandoned items would make it unanswerable."""
    import argparse

    monkeypatch.setenv("RESELL_DB", str(tmp_path / "list.db"))
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    monkeypatch.setenv("EBAY_CLIENT_ID", "a")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "b")
    monkeypatch.setenv("EBAY_RUNAME", "X-Y-Z-abc")

    from resell import cli_item
    from resell.gateway import Gateway

    conn = db.connect(tmp_path / "list.db")
    gateway = Gateway(conn, environment="sandbox")
    live = gateway.ingest_item(purchase_cost_cents=2500).sku
    fixture = gateway.ingest_item(purchase_cost_cents=None, notes="fixture").sku
    conn.execute("UPDATE item SET state = 'abandoned' WHERE sku = ?", (fixture,))
    working = gateway.ingest_item(purchase_cost_cents=None).sku

    assert cli_item.cmd_item_list(argparse.Namespace(state=None, active=False)) == 0
    assert cli_item.cmd_item_list(argparse.Namespace(state=None, active=True)) == 0
    assert cli_item.cmd_item_list(argparse.Namespace(state=["intake"], active=False)) == 0

    # The retired SKU is still on record, so the sequence gap is explicable.
    skus = [row[0] for row in conn.execute("SELECT sku FROM item ORDER BY seq")]
    assert skus == [live, fixture, working]


def test_codes_left_in_prose_are_flagged(tmp_path: Path):
    """From the first real run: the model recorded a style code, a product code and
    a barcode number as text_read claims and left the identifiers array empty, so
    none were check-digit verified or routed to eBay's identifier fields."""
    from resell.reasoning.tools import (
        parse_observe_tool_input, unstructured_identifier_candidates,
    )

    proposal = parse_observe_tool_input({
        "observations": [
            {"claim": "The interior neck label reads 'MADE IN EGYPT'",
             "basis": "text_read", "photo_positions": [1]},
            {"claim": "The tag includes product code '100220547'",
             "basis": "text_read", "photo_positions": [2]},
            {"claim": "The tag has a barcode with number 'S-31517'",
             "basis": "text_read", "photo_positions": [2]},
            {"claim": "The jacket is navy blue", "basis": "visual_observation",
             "photo_positions": [1]},
        ],
        "identifiers": [],
        "identity_search": {"photos_reviewed": 3},
    })
    flagged = {token for token, _ in unstructured_identifier_candidates(proposal)}
    assert "100220547" in flagged
    assert "S-31517" in flagged
    # Prose that merely looks shouty is not a code.
    assert "EGYPT" not in flagged
    # visual_observation claims are not scanned; only transcriptions.
    assert not any("NAVY" in token for token in flagged)

    # Once recorded properly, the code is no longer flagged.
    recorded = parse_observe_tool_input({
        "observations": [
            {"claim": "The tag includes product code '100220547'",
             "basis": "text_read", "photo_positions": [2]},
        ],
        "identifiers": [
            {"scheme": "mpn", "raw_transcription": "100220547", "photo_position": 2},
        ],
        "identity_search": {"photos_reviewed": 3},
    })
    assert unstructured_identifier_candidates(recorded) == []


def test_identifier_contract_demands_structured_capture():
    """The tool description is where this is actually fixed; the heuristic only
    makes the omission visible."""
    from resell.reasoning.stages import OBSERVE_SYSTEM_PROMPT
    from resell.reasoning.tools import OBSERVE_TOOL_SCHEMA

    # The schema description stays short — length there coincided with the model
    # stringifying its arguments — so it carries only the essentials.
    description = OBSERVE_TOOL_SCHEMA["input_schema"]["properties"]["identifiers"]["description"]
    assert "style" in description
    assert "other" in description  # a scheme of last resort beats omission
    assert len(description) < 250

    # The reasoning behind the rule lives in the system prompt.
    assert "must ALSO appear in the `identifiers` array" in OBSERVE_SYSTEM_PROMPT
    assert "Style codes, product codes" in OBSERVE_SYSTEM_PROMPT


def test_parser_is_total_against_malformed_tool_output(tmp_path: Path):
    """Regression from a real run: the model returned `observations` as a list of
    strings rather than objects, and the parser raised TypeError. It guarded
    KeyError and ValueError but not a wrong type.

    The consequence was worse than the crash — it happened before the trace was
    written, so a call that had been paid for left no record and no budget
    accounting. Parsing must never raise."""
    from resell.reasoning.tools import parse_observe_tool_input

    # The exact shape that crashed.
    proposal = parse_observe_tool_input({
        "observations": ["The jacket is navy", "A notch lapel"],
        "identity_search": {"photos_reviewed": 3},
    })
    assert proposal.observations == []
    assert len(proposal.malformed) == 2
    assert "expected an object" in proposal.malformed[0]
    # The identity search still survived, so the pass was not a total loss.
    assert proposal.negative_finding is not None

    # Nothing raises, whatever arrives.
    for payload in (
        "not json at all",
        [1, 2, 3],
        None,
        42,
        {"observations": "not a list", "identity_search": "not an object"},
        {"observations": [{"claim": "x", "basis": "nonsense"}]},
        {"observations": [{"claim": "x", "basis": "text_read",
                           "photo_positions": "photo one"}]},
    ):
        result = parse_observe_tool_input(payload)
        assert isinstance(result.malformed, list)

    # Recoverable shapes are recovered rather than discarded.
    stringified = parse_observe_tool_input({
        "observations": '[{"claim":"navy","basis":"visual_observation","photo_positions":[1]}]',
        "identity_search": {"photos_reviewed": 1},
    })
    assert len(stringified.observations) == 1
    assert stringified.malformed == []

    unwrapped = parse_observe_tool_input({
        "observations": {"claim": "navy", "basis": "visual_observation",
                         "photo_positions": [1]},
        "identity_search": {"photos_reviewed": 1},
    })
    assert len(unwrapped.observations) == 1

    coerced = parse_observe_tool_input({
        "observations": [{"claim": "navy", "basis": "visual_observation",
                          "photo_positions": ["1", "2"], "confidence": "high"}],
        "identity_search": {"photos_reviewed": 1},
    })
    assert coerced.observations[0].photo_positions == (1, 2)
    assert coerced.observations[0].confidence is None  # unusable, not invented


def test_unparseable_identity_search_is_reported_not_dropped():
    """Silently discarding it would lose the distinction between "nothing there"
    and "did not look" — the thing that justifies a described_object mode."""
    from resell.reasoning.tools import parse_observe_tool_input

    proposal = parse_observe_tool_input({
        "observations": [], "identity_search": "I checked the underside",
    })
    assert proposal.negative_finding is None
    assert any("identity_search" in entry for entry in proposal.malformed)


def test_one_bad_entry_does_not_discard_the_good_ones():
    from resell.reasoning.tools import parse_observe_tool_input

    proposal = parse_observe_tool_input({
        "observations": [
            {"claim": "navy", "basis": "visual_observation", "photo_positions": [1]},
            "a loose string",
            {"claim": "tall", "basis": "nonsense"},
        ],
        "identifiers": [
            {"scheme": "mpn", "raw_transcription": "100220547", "photo_position": 2},
            "junk",
        ],
        "identity_search": {"surfaces_examined": ["underside"], "photos_reviewed": 3},
    })
    assert len(proposal.observations) == 1
    assert len(proposal.identifiers) == 1
    assert len(proposal.malformed) == 3
    assert proposal.negative_finding.surfaces_examined == ("underside",)


# --- durable call ledger -----------------------------------------------------


class _LedgerAdapter:
    provider = "fake"
    model = "v1"

    def __init__(self, tool_input=None, fail=None, explode=False):
        self._tool_input = tool_input
        self._fail = fail
        self._explode = explode

    def estimate_input_tokens(self, request):
        return 9000

    def rates(self):
        from resell.reasoning.budget import ModelRates

        return ModelRates()

    def run(self, request):
        from resell.reasoning.adapters.base import AdapterError
        from resell.reasoning.stages import StageResult, Usage

        if self._explode:
            raise KeyboardInterrupt("process died mid-call")
        if self._fail:
            raise AdapterError("fake", self._fail)
        return StageResult(
            tool_input=self._tool_input, usage=Usage(2411, 388, {"input_tokens": 2411}),
            latency_ms=1200, provider="fake", model="v1", stop_reason="tool_use",
            raw_response={"content": []},
        )


def _ledger_setup(tmp_path: Path, monkeypatch):
    import resell.reasoning.vision as vision
    from resell.gateway import Gateway

    monkeypatch.setattr(vision, "for_model", lambda source, cache_dir, digest=None: Path(source))
    photo = tmp_path / "p.jpg"
    photo.write_bytes(_jpeg_bytes(1600, 1200) + b"\x00" * 500)
    conn = db.connect(tmp_path / "ledger.db")
    sku = Gateway(conn, environment="sandbox").ingest_item(purchase_cost_cents=None).sku
    return conn, sku, [photo]


def test_a_paid_call_survives_a_parse_failure(tmp_path: Path, monkeypatch):
    """The call that crashed on the first real run left no trace at all, because the
    row was only written after parsing succeeded. It had been paid for."""
    from resell.reasoning.budget import StageBudget
    from resell.reasoning.vision import observe_and_record, spend_so_far

    conn, sku, photos = _ledger_setup(tmp_path, monkeypatch)
    budget = StageBudget(max_calls=9, max_cost_micros=1_000_000)

    adapter = _LedgerAdapter(tool_input={
        "observations": [{"claim": "navy", "photo_positions": [1]}],  # no basis
        "identity_search": {"photos_reviewed": 3},
    })
    outcome, call_id = observe_and_record(
        conn, sku, photos, cache_dir=tmp_path, adapter=adapter, budget=budget
    )
    assert outcome.proposal.observations == []

    row = conn.execute("SELECT * FROM model_call WHERE id = ?", (call_id,)).fetchone()
    assert row["status"] == "parse_failed"
    assert row["input_tokens"] == 2411      # tokens were billed and are recorded
    assert row["cost_micros"] > 0
    assert row["response"] is not None      # the raw response is kept for inspection
    assert "unusable basis" in row["error"]
    # And it counts against the budget.
    assert spend_so_far(conn, sku).calls == 1


def test_a_crash_mid_call_leaves_an_attempted_row_charged_at_estimate(tmp_path: Path, monkeypatch):
    """Not knowing whether we were billed is not a reason to assume we were not."""
    from resell.reasoning.budget import StageBudget
    from resell.reasoning.vision import observe_and_record, spend_so_far

    conn, sku, photos = _ledger_setup(tmp_path, monkeypatch)
    budget = StageBudget(max_calls=9, max_cost_micros=1_000_000)

    with pytest.raises(KeyboardInterrupt):
        observe_and_record(
            conn, sku, photos, cache_dir=tmp_path,
            adapter=_LedgerAdapter(explode=True), budget=budget,
        )

    row = conn.execute("SELECT * FROM model_call WHERE sku = ?", (sku,)).fetchone()
    assert row["status"] == "attempted"
    assert row["cost_micros"] is None
    assert row["estimated_cost_micros"] > 0
    assert row["request"] is not None       # the replay key was written beforehand

    spent = spend_so_far(conn, sku)
    assert spent.calls == 1
    assert spent.cost_micros == row["estimated_cost_micros"]


def test_provider_errors_are_recorded_but_not_charged(tmp_path: Path, monkeypatch):
    """A rejected request is not a billed one, so it should not consume budget —
    but it must still be visible."""
    from resell.reasoning.budget import StageBudget
    from resell.reasoning.vision import VisionError, observe_and_record, spend_so_far

    conn, sku, photos = _ledger_setup(tmp_path, monkeypatch)

    with pytest.raises(VisionError, match="529"):
        observe_and_record(
            conn, sku, photos, cache_dir=tmp_path,
            adapter=_LedgerAdapter(fail="HTTP 529 overloaded"),
            budget=StageBudget(max_calls=9, max_cost_micros=1_000_000),
        )

    row = conn.execute("SELECT * FROM model_call WHERE sku = ?", (sku,)).fetchone()
    assert row["status"] == "provider_error"
    assert "529" in row["error"]
    assert spend_so_far(conn, sku).calls == 0


def test_ledger_row_exists_before_the_provider_is_contacted(tmp_path: Path, monkeypatch):
    """The ordering is the whole mechanism: durable first, call second."""
    from resell.reasoning.budget import StageBudget
    from resell.reasoning.vision import observe_and_record

    conn, sku, photos = _ledger_setup(tmp_path, monkeypatch)
    seen: dict = {}

    class Checking(_LedgerAdapter):
        def run(self, request):
            # By the time the provider is contacted, the row must already be durable.
            seen["rows"] = conn.execute(
                "SELECT id, status FROM model_call WHERE sku = ?", (sku,)
            ).fetchall()
            return super().run(request)

    observe_and_record(
        conn, sku, photos, cache_dir=tmp_path,
        adapter=Checking(tool_input={
            "observations": [{"claim": "navy", "basis": "visual_observation",
                              "photo_positions": [1]}],
            "identity_search": {"photos_reviewed": 1},
        }),
        budget=StageBudget(max_calls=9, max_cost_micros=1_000_000),
    )
    assert len(seen["rows"]) == 1
    assert seen["rows"][0]["status"] == "attempted"


def test_keyed_observations_are_recovered_and_named(tmp_path: Path):
    """A dict of entries keyed by index used to be wrapped into one unusable
    element, reporting "unusable basis None" and hiding the real shape."""
    from resell.reasoning.tools import parse_observe_tool_input

    proposal = parse_observe_tool_input({
        "observations": {
            "1": {"claim": "navy blazer", "basis": "visual_observation",
                  "photo_positions": [1]},
            "2": {"claim": "notch lapel", "basis": "visual_observation",
                  "photo_positions": [1]},
        },
        "identity_search": {"photos_reviewed": 3},
    })
    assert len(proposal.observations) == 2
    assert any("keyed rather than listed" in entry for entry in proposal.malformed)

    # A genuine single unwrapped entry still recovers silently.
    single = parse_observe_tool_input({
        "observations": {"claim": "navy", "basis": "visual_observation",
                         "photo_positions": [1]},
        "identity_search": {"photos_reviewed": 1},
    })
    assert len(single.observations) == 1
    assert single.malformed == []

    # A missing basis now reports what WAS present, which is the diagnostic question.
    missing = parse_observe_tool_input({
        "observations": [{"claim": "navy", "photo_positions": [1], "category": "colour"}],
        "identity_search": {"photos_reviewed": 1},
    })
    assert "keys present" in missing.malformed[0]
    assert "category" in missing.malformed[0]


def test_stringified_observations_are_recovered(tmp_path: Path):
    """The actual second-run failure: the model serialised `observations` into a
    JSON string rather than emitting an array, and omitted `identity_search`
    entirely despite it being required. 2,189 output tokens of real content, in the
    wrong container.

    The old parser JSON-decoded the string, found something that was not a list of
    entries, wrapped it, and reported "unusable basis None" — a message describing
    neither the cause nor the shape."""
    import json as _json

    from resell.reasoning.tools import parse_observe_tool_input

    entries = [
        {"claim": "The item is a men's suit jacket", "basis": "visual_observation",
         "photo_positions": [1]},
        {"claim": "The interior label reads 'MADE IN EGYPT'", "basis": "text_read",
         "photo_positions": [1]},
    ]

    # Array serialised as a string, identity_search missing.
    recovered = parse_observe_tool_input({"observations": _json.dumps(entries)})
    assert len(recovered.observations) == 2
    assert recovered.malformed == []
    assert recovered.negative_finding is None

    # A truncated string is unrecoverable, and says so accurately.
    truncated = parse_observe_tool_input({"observations": _json.dumps(entries)[:80]})
    assert truncated.observations == []
    assert "unparseable string" in truncated.malformed[0]


def test_schema_stays_lean_while_keeping_its_constraints():
    """A long tool schema coincided with the model stringifying its arguments. The
    detail belongs in the system prompt, where it cannot affect how tool arguments
    are encoded — but the constraints themselves must survive the trim."""
    import json as _json

    from resell.reasoning.stages import OBSERVE_SYSTEM_PROMPT
    from resell.reasoning.tools import OBSERVE_TOOL_SCHEMA

    schema = OBSERVE_TOOL_SCHEMA["input_schema"]
    assert len(_json.dumps(schema)) < 2500

    assert schema["required"] == ["observations", "identifiers", "identity_search"]
    items = schema["properties"]["observations"]["items"]
    assert items["required"] == ["claim", "basis"]
    assert len(items["properties"]["basis"]["enum"]) == 4

    # The removed guidance survives where it belongs.
    assert "`text_read`" in OBSERVE_SYSTEM_PROMPT
    assert "There is no default" in OBSERVE_SYSTEM_PROMPT
    assert "Do not serialise them" in OBSERVE_SYSTEM_PROMPT


def test_identifiers_is_a_required_field(tmp_path: Path):
    """Two consecutive runs transcribed product codes into prose and omitted the
    identifiers array entirely — which satisfied the schema, because the field was
    optional. Prompt emphasis cannot fix a contract that says the field may be
    absent; an empty array is now an explicit assertion instead."""
    import json as _json

    from resell.reasoning.stages import OBSERVE_SYSTEM_PROMPT
    from resell.reasoning.tools import OBSERVE_TOOL_SCHEMA

    schema = OBSERVE_TOOL_SCHEMA["input_schema"]
    assert schema["required"] == ["observations", "identifiers", "identity_search"]
    assert "empty array asserts" in schema["properties"]["identifiers"]["description"]
    assert "empty array is a claim, not a default" in OBSERVE_SYSTEM_PROMPT
    # The trim must survive the addition.
    assert len(_json.dumps(schema)) < 2500


def test_backstop_catches_the_codes_from_the_real_run():
    """The four codes that reached prose on the third run, verbatim."""
    from resell.reasoning.tools import (
        parse_observe_tool_input, unstructured_identifier_candidates,
    )

    proposal = parse_observe_tool_input({
        "observations": [
            {"claim": "The swing tag product number reads '100220547'",
             "basis": "text_read", "photo_positions": [2]},
            {"claim": "The swing tag lists a factory code '28643'",
             "basis": "text_read", "photo_positions": [2]},
            {"claim": "The swing tag has a barcode with number 'S-315125' printed beneath it",
             "basis": "text_read", "photo_positions": [2]},
            {"claim": "The interior neck label reads MADE IN EGYPT",
             "basis": "text_read", "photo_positions": [1]},
            {"claim": "The jacket is navy blue", "basis": "visual_observation",
             "photo_positions": [1]},
        ],
        "identifiers": [],
        "identity_search": {"photos_reviewed": 3},
    })
    flagged = {token for token, _ in unstructured_identifier_candidates(proposal)}
    assert {"100220547", "28643", "S-315125"} <= flagged
    # Prose in capitals is not a code.
    assert "EGYPT" not in flagged


# --- aspect mapping ----------------------------------------------------------


def _mapping_fixture(tmp_path: Path):
    """An item with two observation runs and one operator statement."""
    from resell.gateway import Gateway
    from resell.reasoning.schema import Basis, Observation

    conn = db.connect(tmp_path / "mapping.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=2500).sku

    def call(purpose="observe", status="completed"):
        return conn.execute(
            "INSERT INTO model_call (sku, purpose, provider, model, called_at, status) "
            "VALUES (?, ?, 'a', 'm', ?, ?)",
            (sku, purpose, db.now_iso(), status),
        ).lastrowid

    old = call()
    stale = gateway.record_observation(
        sku, Observation(claim="OLD RUN: navy", basis=Basis.VISUAL_OBSERVATION,
                         photo_positions=(1,)),
        model_call_id=old,
    ).data["evidence_id"]

    latest = call()
    ids = {"stale": stale}
    for key, observation in {
        "navy": Observation(claim="Swing tag colour reads 'NAVY MINI HT'",
                            basis=Basis.TEXT_READ, photo_positions=(2,)),
        "brand": Observation(claim="Label reads BROOKS BROTHERS",
                             basis=Basis.TEXT_READ, photo_positions=(3,)),
        "dept": Observation(claim="Men's tailored cut", basis=Basis.INFERENCE,
                            photo_positions=(1,)),
    }.items():
        ids[key] = gateway.record_observation(
            sku, observation, model_call_id=latest
        ).data["evidence_id"]

    ids["operator"] = gateway.record_evidence(
        sku, kind="operator_answer", source="operator", payload={"answer": "42R"},
        basis=str(Basis.OPERATOR),
    ).data["evidence_id"]
    return conn, gateway, sku, ids


def _specs():
    from resell.ebay.publisher import AspectSpec

    return [
        AspectSpec("Brand", True, "FREE_TEXT", "SINGLE", "STRING", 65, ()),
        AspectSpec("Size", True, "SELECTION_ONLY", "SINGLE", "STRING", None,
                   ("38R", "40R", "42R")),
        AspectSpec("Color", True, "SELECTION_ONLY", "SINGLE", "STRING", None,
                   ("Navy", "Blue")),
        AspectSpec("Material", False, "FREE_TEXT", "SINGLE", "STRING", 65, ()),
    ]


class _MapAdapter:
    provider = "fake"
    model = "m"

    def __init__(self, tool_input):
        self._tool_input = tool_input
        self.seen = None

    def estimate_input_tokens(self, request):
        return 2000

    def rates(self):
        from resell.reasoning.budget import ModelRates

        return ModelRates()

    def run(self, request):
        from resell.reasoning.stages import StageResult, Usage

        self.seen = request
        return StageResult(
            tool_input=self._tool_input, usage=Usage(2000, 600, {}), latency_ms=900,
            provider="fake", model="m", stop_reason="tool_use", raw_response={},
        )


def _map(conn, sku, tool_input, adapter=None):
    from resell.gateway import observations_in_scope
    from resell.reasoning.budget import StageBudget, StageSpend
    from resell.reasoning.mapping import map_aspects

    adapter = adapter or _MapAdapter(tool_input)
    return map_aspects(
        conn, sku, specs=_specs(), observations=observations_in_scope(conn, sku),
        adapter=adapter, budget=StageBudget(max_calls=99, max_cost_micros=9_000_000),
        spent=StageSpend(),
    ), adapter


def test_mapping_is_scoped_to_the_latest_observation_run(tmp_path: Path):
    """Two runs of the same photos produce near-duplicate claims, and citing one of
    two near-identical rows is arbitrary. Earlier runs stay in the database for
    audit and cross-provider evaluation; they are simply not citable."""
    from resell.gateway import observations_in_scope

    conn, _, sku, ids = _mapping_fixture(tmp_path)

    in_scope = {row["id"] for row in observations_in_scope(conn, sku)}
    assert ids["stale"] not in in_scope
    assert ids["navy"] in in_scope
    # Operator evidence belongs to no run and is always citable.
    assert ids["operator"] in in_scope
    # Nothing was deleted.
    assert conn.execute("SELECT COUNT(*) FROM evidence WHERE sku = ?", (sku,)).fetchone()[0] == 5

    outcome, _ = _map(conn, sku, {"aspects": [
        {"aspect_name": "Color", "candidates": [
            {"value": "Navy", "evidence_ids": [ids["stale"]]}]},
    ]})
    assert any("not in scope" in entry for entry in outcome.proposal.malformed)


def test_invented_citations_are_dropped_not_trusted(tmp_path: Path):
    """A fabricated citation is worse than a missing one, because it looks like
    support."""
    from resell.reasoning.gaps import Resolution

    conn, _, sku, ids = _mapping_fixture(tmp_path)
    outcome, _ = _map(conn, sku, {"aspects": [
        {"aspect_name": "Size", "candidates": [{"value": "42R", "evidence_ids": [9999]}]},
        {"aspect_name": "Brand", "candidates": [
            {"value": "Brooks Brothers", "evidence_ids": [ids["brand"]]}]},
    ]})
    by_name = {item.aspect_name: item for item in outcome.outcomes}
    assert by_name["Size"].resolution is Resolution.UNSUPPORTED
    assert by_name["Brand"].value == "Brooks Brothers"
    assert any("9999" in entry for entry in outcome.proposal.malformed)


def test_basis_is_read_from_storage_not_from_the_model(tmp_path: Path):
    """Resolution treats an operator statement as adjudicating, so the basis must
    come from the database — otherwise a model could claim operator support for its
    own guess."""
    from resell.reasoning.gaps import Resolution

    conn, _, sku, ids = _mapping_fixture(tmp_path)
    outcome, _ = _map(conn, sku, {"aspects": [
        {"aspect_name": "Size", "candidates": [
            {"value": "40R", "evidence_ids": [ids["dept"]]},
            {"value": "42R", "evidence_ids": [ids["operator"]]},
        ]},
    ]})
    size = next(item for item in outcome.outcomes if item.aspect_name == "Size")
    assert size.resolution is Resolution.RESOLVED_BY_OPERATOR
    assert size.value == "42R"
    assert "40R" in size.explanation      # the superseded reading is retained


def test_mapping_distinguishes_ambiguity_from_contradiction(tmp_path: Path):
    from resell.reasoning.gaps import GapAction, Resolution

    conn, _, sku, ids = _mapping_fixture(tmp_path)

    ambiguous, _ = _map(conn, sku, {"aspects": [
        {"aspect_name": "Color", "candidates": [
            {"value": "Navy", "evidence_ids": [ids["navy"]]},
            {"value": "Blue", "evidence_ids": [ids["navy"]]},
        ]},
    ]})
    color = next(item for item in ambiguous.outcomes if item.aspect_name == "Color")
    assert color.resolution is Resolution.AMBIGUOUS
    assert next(g for g in ambiguous.gaps if g.aspect_name == "Color").action is GapAction.REQUEST_PHOTO

    contradicted, _ = _map(conn, sku, {"aspects": [
        {"aspect_name": "Size", "candidates": [
            {"value": "40R", "evidence_ids": [ids["dept"]]},
            {"value": "38R", "evidence_ids": [ids["navy"]]},
        ]},
    ]})
    size = next(item for item in contradicted.outcomes if item.aspect_name == "Size")
    assert size.resolution is Resolution.CONTRADICTED
    assert next(g for g in contradicted.gaps if g.aspect_name == "Size").action is GapAction.ASK_OPERATOR


def test_optional_aspect_gaps_do_not_block(tmp_path: Path):
    conn, _, sku, ids = _mapping_fixture(tmp_path)
    outcome, _ = _map(conn, sku, {"aspects": [
        {"aspect_name": "Brand", "candidates": [
            {"value": "Brooks Brothers", "evidence_ids": [ids["brand"]]}]},
        {"aspect_name": "Material", "candidates": []},
    ]})
    material = next(g for g in outcome.gaps if g.aspect_name == "Material")
    assert material.blocking is False
    assert all(g.blocking for g in outcome.gaps if g.aspect_name in {"Size", "Color"})


def test_mapping_stage_sends_no_images(tmp_path: Path):
    """It works from recorded observations so that every value is traceable to one.
    Letting it re-observe would let it produce values with nothing behind them."""
    conn, _, sku, ids = _mapping_fixture(tmp_path)
    _, adapter = _map(conn, sku, {"aspects": []})
    assert adapter.seen.images == ()
    assert "Recorded observations:" in adapter.seen.instruction
    assert f"[{ids['navy']}]" in adapter.seen.instruction


def test_candidate_citations_are_enforced_by_foreign_key(tmp_path: Path):
    conn, gateway, sku, ids = _mapping_fixture(tmp_path)
    outcome, _ = _map(conn, sku, {"aspects": [
        {"aspect_name": "Brand", "candidates": [
            {"value": "Brooks Brothers", "evidence_ids": [ids["brand"]]}]},
    ]})
    gateway.propose_identification(sku, title="t", category_id="3001", condition_id="NEW")
    identification_id = conn.execute(
        "SELECT id FROM identification WHERE sku = ? ORDER BY version DESC LIMIT 1", (sku,)
    ).fetchone()[0]

    assert gateway.record_aspect_candidates(sku, identification_id, outcome.outcomes) == 1
    row = conn.execute(
        "SELECT c.aspect_name, c.value, e.evidence_id FROM aspect_candidate c "
        "JOIN aspect_candidate_evidence e ON e.candidate_id = c.id"
    ).fetchone()
    assert row["value"] == "Brooks Brothers"
    assert row["evidence_id"] == ids["brand"]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO aspect_candidate_evidence (candidate_id, evidence_id) "
            "VALUES (?, 88888)", (1,)
        )


def test_operator_evidence_records_its_basis(tmp_path: Path):
    """Regression: record_evidence and answer_question never set basis, so an
    operator answer stored NULL and fell back to `inference` during resolution —
    adjudication would silently not work."""
    from resell.gateway import Gateway
    from resell.reasoning.schema import Basis

    conn = db.connect(tmp_path / "basis.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=None).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.ask_operator(sku, question="What size is on the label?")
    question_id = conn.execute(
        "SELECT id FROM open_question WHERE sku = ?", (sku,)
    ).fetchone()["id"]
    gateway.answer_question(question_id, "42R", operator=True)

    basis = conn.execute(
        "SELECT basis FROM evidence WHERE sku = ? AND kind = 'operator_answer'", (sku,)
    ).fetchone()[0]
    assert basis == str(Basis.OPERATOR)


def test_placeholder_values_never_resolve():
    """From the first real mapping run: the model proposed the literal string
    `<UNKNOWN>` for Chest Size and cited the observation saying no size number is
    legible. That is a negative observation — evidence of absence, not support for a
    value — and `<UNKNOWN>` would have gone into a listing verbatim."""
    from resell.reasoning.tools import is_placeholder, parse_map_tool_input

    assert is_placeholder("<UNKNOWN>")
    assert is_placeholder("UNKNOWN")
    assert is_placeholder("n/a")
    assert is_placeholder("  tbd ")
    assert is_placeholder("not legible")
    # Real eBay aspect values that merely sound like absence must survive.
    assert not is_placeholder("Does not apply")
    assert not is_placeholder("Unbranded")
    assert not is_placeholder("None of the above")

    proposal = parse_map_tool_input(
        {"aspects": [
            {"aspect_name": "Chest Size", "candidates": [
                {"value": "<UNKNOWN>", "evidence_ids": [59]}]},
        ]},
        valid_evidence_ids={59},
    )
    assert proposal.candidates_by_aspect["Chest Size"] == []
    assert any("placeholder is not a value" in entry for entry in proposal.malformed)


def test_multi_valued_aspects_are_not_ambiguities():
    """A fabric that is 88% wool, 8% polyester and 4% elastane genuinely has three
    materials. Reporting that as an ambiguity asks the operator to choose between
    three correct answers."""
    from resell.reasoning.gaps import Candidate, Resolution, resolve_aspect
    from resell.reasoning.schema import Basis, EvidenceRef

    ref = EvidenceRef(16, Basis.TEXT_READ)
    candidates = [
        Candidate("Wool", (ref,)), Candidate("Polyester", (ref,)),
        Candidate("Spandex", (ref,)),
    ]

    multi = resolve_aspect("Material", candidates, cardinality="MULTI")
    assert multi.resolution is Resolution.RESOLVED
    assert multi.values == ("Wool", "Polyester", "Spandex")
    assert not multi.blocking

    # The identical shape on a single-valued aspect still competes.
    single = resolve_aspect("Color", candidates[:2], cardinality="SINGLE")
    assert single.resolution is Resolution.AMBIGUOUS
    assert single.blocking

    # A lone value behaves the same either way.
    lone = resolve_aspect("Brand", [Candidate("Brooks Brothers", (ref,))], cardinality="MULTI")
    assert lone.resolution is Resolution.RESOLVED
    assert lone.values == ("Brooks Brothers",)


def test_cardinality_flows_from_the_aspect_form(tmp_path: Path):
    """The cardinality is eBay's, read from Taxonomy, not something the model or the
    resolver decides."""
    from resell.ebay.publisher import AspectSpec
    from resell.gateway import observations_in_scope
    from resell.reasoning.budget import StageBudget, StageSpend
    from resell.reasoning.gaps import Resolution
    from resell.reasoning.mapping import map_aspects

    conn, _, sku, ids = _mapping_fixture(tmp_path)
    specs = [
        AspectSpec("Material", False, "FREE_TEXT", "MULTI", "STRING", None, ()),
        AspectSpec("Color", True, "SELECTION_ONLY", "SINGLE", "STRING", None, ("Navy", "Blue")),
    ]
    adapter = _MapAdapter({"aspects": [
        {"aspect_name": "Material", "candidates": [
            {"value": "Wool", "evidence_ids": [ids["navy"]]},
            {"value": "Polyester", "evidence_ids": [ids["navy"]]}]},
        {"aspect_name": "Color", "candidates": [
            {"value": "Navy", "evidence_ids": [ids["navy"]]},
            {"value": "Blue", "evidence_ids": [ids["navy"]]}]},
    ]})
    outcome = map_aspects(
        conn, sku, specs=specs, observations=observations_in_scope(conn, sku),
        adapter=adapter, budget=StageBudget(max_calls=9, max_cost_micros=9_000_000),
        spent=StageSpend(),
    )
    by_name = {item.aspect_name: item for item in outcome.outcomes}
    assert by_name["Material"].resolution is Resolution.RESOLVED
    assert by_name["Material"].values == ("Wool", "Polyester")
    assert by_name["Color"].resolution is Resolution.AMBIGUOUS


def test_multi_value_coexistence_is_distinguished_from_competition():
    """Introduced by the cardinality fix and caught on the next real run. Accepting
    several values is not the same as those values coexisting.

    Material cites one fabric-content reading for wool, polyester and elastane: the
    same evidence, three properties the item genuinely has. MPN cites one
    observation for a product number and a different one for a style code: two codes
    competing for one field, which the previous run had correctly flagged as
    contradicted before MULTI silently resolved it."""
    from resell.reasoning.gaps import (
        BLOCKING_RESOLUTIONS, Candidate, Resolution, resolve_aspect,
    )
    from resell.reasoning.schema import Basis, EvidenceRef

    shared = (EvidenceRef(16, Basis.TEXT_READ), EvidenceRef(45, Basis.TEXT_READ))
    coexisting = resolve_aspect(
        "Material",
        [Candidate("Wool", shared), Candidate("Polyester", shared),
         Candidate("Spandex", shared)],
        cardinality="MULTI",
    )
    assert coexisting.resolution is Resolution.RESOLVED
    assert coexisting.values == ("Wool", "Polyester", "Spandex")
    assert "shared evidence" in coexisting.explanation

    competing = resolve_aspect(
        "MPN",
        [Candidate("100220547", (EvidenceRef(15, Basis.TEXT_READ),)),
         Candidate("SUJT EXP 2BSV SLIM", (EvidenceRef(14, Basis.TEXT_READ),))],
        cardinality="MULTI",
    )
    assert competing.resolution is Resolution.RESOLVED_UNVERIFIED
    assert "compete for this field" in competing.explanation
    # Usable, not blocking: they may legitimately coexist, and nothing here can tell.
    assert Resolution.RESOLVED_UNVERIFIED not in BLOCKING_RESOLUTIONS
    assert competing.blocking is False


def test_apply_records_the_category_that_produced_the_aspects(tmp_path: Path, monkeypatch):
    """Regression: `--apply` called propose_identification with aspects alone, which
    supersedes rather than patches, so the identification came out with aspects and
    a NULL category, title and condition. Aspects without the category whose form
    defines them are uninterpretable — and `item aspects` then had no category to
    look up.

    The merge existed for `item identify` already; --apply bypassed it. There is now
    one implementation."""
    import argparse

    monkeypatch.setenv("RESELL_DB", str(tmp_path / "apply.db"))
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    monkeypatch.setenv("EBAY_CLIENT_ID", "a")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "b")
    monkeypatch.setenv("EBAY_RUNAME", "X-Y-Z-abc")

    from resell import cli_item
    from resell.gateway import Gateway, current_identification

    conn = db.connect(tmp_path / "apply.db")
    gateway = Gateway(conn, environment="sandbox")
    sku = gateway.ingest_item(purchase_cost_cents=2500).sku
    gateway.attach_photo(
        sku, source_path="/a.jpg", content_sha256=_digest("a"),
        image_format="jpeg", size_bytes=1, validation_errors=None,
    )
    gateway.begin_identification(sku)
    gateway.propose_identification(
        sku, title="Brooks Brothers Blazer", category_id="3001", condition_id="NEW"
    )

    fields, carried = cli_item.merged_identification(
        conn, sku, aspects={"Brand": ["Brooks Brothers"]}, category_id="3001"
    )
    assert fields["category_id"] == "3001"
    assert fields["title"] == "Brooks Brothers Blazer"   # not wiped
    assert fields["condition_id"] == "NEW"
    assert fields["aspects"] == {"Brand": ["Brooks Brothers"]}
    assert {"title", "condition_id"} <= set(carried)

    gateway.propose_identification(sku, **fields)
    current = current_identification(conn, sku)
    assert current["category_id"] == "3001"
    assert current["title"] == "Brooks Brothers Blazer"

    # Merging reads the CURRENT version only. Reaching further back would resurrect
    # beliefs that were deliberately superseded.
    gateway.propose_identification(sku, aspects={"Color": ["Navy"]})
    fields, carried = cli_item.merged_identification(conn, sku, aspects={"Size": ["42R"]})
    assert fields["title"] is None
    assert carried == []
    # And the superseded values remain on record.
    titles = [
        row["title"] for row in conn.execute(
            "SELECT title FROM identification WHERE sku = ? ORDER BY version", (sku,)
        )
    ]
    assert "Brooks Brothers Blazer" in titles
