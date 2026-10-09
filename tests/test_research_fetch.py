"""Retrieval primitives: where a document came from, and what it says.

The adapters that fetched pages and had a model read them are gone with the
identity research loop. What is left is what outlived it and is still used --
authority resolved from a URL, HTML reduced to text, and the fetcher itself, which
`marketplace.py` still drives. Authority is the one worth insisting on: it is a
fact about the URL and never a judgement the reasoning plane gets to make.
"""

from __future__ import annotations

import pytest

from resell import db
from resell.domain import FeeModel
from resell.gateway import Gateway, candidate_evidence
from resell.reasoning.adapters.fetch import FetchError, PageFetcher, html_to_text
from resell.reasoning.adapters.research import ResearchQuery, RetrievalMethod
from resell.reasoning.authority import authority_for_url
from resell.reasoning.research import SourceAuthority


# --- authority is a fact about the URL -----------------------------------------


def test_a_listed_domain_carries_its_authority():
    authority, _ = authority_for_url("https://beatsbydre.com/pill")
    assert authority is SourceAuthority.MANUFACTURER


def test_a_subdomain_inherits_from_its_parent():
    authority, why = authority_for_url("https://www.shop.beatsbydre.com/pill")
    assert authority is SourceAuthority.MANUFACTURER
    assert "via beatsbydre.com" in why


def test_an_unlisted_domain_donates_nothing():
    """Fails closed. The alternative is a heuristic that guesses upward."""
    authority, why = authority_for_url("https://some-blog.example/beats-review")
    assert authority is SourceAuthority.UNKNOWN
    assert "not in the authority table" in why


def test_a_lookalike_domain_does_not_inherit():
    """Matching walks up the labels, never down: apple.com.evil.example is a
    different registrable domain and is exactly the shape a spoof takes."""
    authority, _ = authority_for_url("https://apple.com.evil.example/p")
    assert authority is SourceAuthority.UNKNOWN


def test_a_reseller_is_recorded_as_one():
    authority, _ = authority_for_url("https://www.amazon.com/dp/B0000")
    assert authority is SourceAuthority.RESELLER


def test_ebay_carries_no_authority():
    """No adapter fetches it and none asserts authority for it, so it resolves to
    UNKNOWN like any unlisted host."""
    authority, _ = authority_for_url("https://www.ebay.com/itm/110590229841")
    assert authority is SourceAuthority.UNKNOWN


def test_a_url_with_no_host_is_unknown():
    authority, why = authority_for_url("not-a-url")
    assert authority is SourceAuthority.UNKNOWN
    assert "no host" in why


# --- html to text ---------------------------------------------------------------


def test_script_and_style_contents_are_dropped():
    """A product page embeds its whole catalogue as JSON in a script tag; feeding
    that to the extractor buries the visible specification in machine noise."""
    text, _ = html_to_text(
        "<html><head><style>.a{color:red}</style>"
        '<script>var catalog={"sku":"zzz"}</script></head>'
        "<body><p>Colourway: Navy</p></body></html>"
    )
    assert "Colourway: Navy" in text
    assert "catalog" not in text
    assert "color:red" not in text


def test_tag_boundaries_keep_fields_apart():
    """Two table cells must not become one word, or an excerpt check on either
    would fail against text the page really does contain."""
    text, _ = html_to_text("<table><tr><td>Navy</td><td>Wool</td></tr></table>")
    assert "NavyWool" not in text
    assert "Navy" in text and "Wool" in text


def test_the_title_is_returned_separately():
    _, title = html_to_text("<html><head><title>Beats Pill</title></head><body>x</body></html>")
    assert title == "Beats Pill"


def test_broken_markup_yields_what_it_can():
    text, _ = html_to_text("<p>Navy <b>wool</p></b><div>unclosed")
    assert "Navy" in text and "unclosed" in text


# --- the fetcher ----------------------------------------------------------------


class FakeResponse:
    def __init__(self, body=b"<p>hi</p>", status=200, content_type="text/html", url=None):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.content = body
        self.encoding = "utf-8"
        self.url = url or "https://beatsbydre.com/p"
        self._chunks = None
        self._clock = None

    def set_chunks(self, chunks, clock=None):
        self._chunks = chunks
        self._clock = clock

    def iter_bytes(self):
        if self._chunks is None:
            yield self.content
            return
        for item in self._chunks:
            if isinstance(item, (int, float)):
                # A gap in the transfer. The connection is fine; the page is not.
                if self._clock is not None:
                    self._clock.advance(item)
                continue
            yield item

    def close(self):
        pass


class FakeStream:
    """A streamed response, because that is what the fetcher now asks for.

    `chunks` is a list of byte strings, optionally interleaved with floats: a
    float is how long that gap in the transfer lasts. That is what a stalled
    server looks like from here, and it is the case the byte cap could never
    catch -- the bytes arrive, just never enough of them and never an end.
    """

    def __init__(self, response, chunks=None, clock=None):
        self._response = response
        self._chunks = chunks
        self._clock = clock

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, response=None, error=None, chunks=None, clock=None):
        self._response = response or FakeResponse()
        self._error = error
        self._chunks = chunks
        self._clock = clock
        self.requested = []

    def stream(self, method, url):
        self.requested.append(url)
        if self._error:
            raise self._error
        if self._chunks is not None:
            self._response.set_chunks(self._chunks, self._clock)
        return FakeStream(self._response)

    def close(self):
        pass


def fetcher(response=None, error=None, chunks=None, clock=None, **kw):
    return PageFetcher(
        client=FakeClient(response, error, chunks, clock), respect_robots=False, **kw
    )


def test_a_page_is_fetched_and_reduced_to_text():
    page = fetcher(FakeResponse(b"<html><body><p>Colourway: Navy</p></body></html>")).fetch(
        "https://beatsbydre.com/p"
    )
    assert page.status_code == 200
    assert "Colourway: Navy" in page.text


def test_a_non_html_content_type_is_refused_before_reading():
    with pytest.raises(FetchError, match="not a page"):
        fetcher(FakeResponse(b"%PDF-1.4", content_type="application/pdf")).fetch(
            "https://beatsbydre.com/manual.pdf"
        )


def test_an_error_status_is_a_fetch_error():
    with pytest.raises(FetchError, match="HTTP 404"):
        fetcher(FakeResponse(status=404)).fetch("https://beatsbydre.com/gone")


def test_a_transport_failure_does_not_leak_the_vendor_exception():
    with pytest.raises(FetchError, match="RuntimeError"):
        fetcher(error=RuntimeError("connection reset")).fetch("https://beatsbydre.com/p")


def test_a_non_http_scheme_is_refused():
    with pytest.raises(FetchError, match="not a fetchable scheme"):
        fetcher().fetch("file:///etc/passwd")


def test_an_oversized_body_is_truncated_rather_than_read_whole():
    body = b"<p>" + b"x" * 5000 + b"</p>"
    page = fetcher(FakeResponse(body), max_bytes=500).fetch("https://beatsbydre.com/p")
    assert page.truncated
    assert page.bytes_read == 500


def test_a_redirect_is_reported_so_authority_follows_the_destination():
    page = fetcher(
        FakeResponse(url="https://elsewhere.example/p")
    ).fetch("https://beatsbydre.com/p")
    assert page.redirected
    assert page.final_url == "https://elsewhere.example/p"
    # And the authority of where we landed, not of what was typed.
    assert authority_for_url(page.final_url)[0] is SourceAuthority.UNKNOWN


def test_an_unreadable_robots_file_leaves_the_page_alone():
    """Unreadable is not permission. Proceeding anyway would make the setting
    decorative."""
    guard = PageFetcher(client=FakeClient(), respect_robots=True)
    guard._robots["https://beatsbydre.com"] = None
    allowed, why = guard.allowed("https://beatsbydre.com/p")
    assert allowed is False
    assert "could not be read" in why


def test_robots_disallow_is_honoured():
    from urllib.robotparser import RobotFileParser

    parser = RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /private"])
    guard = PageFetcher(client=FakeClient(), respect_robots=True)
    guard._robots["https://beatsbydre.com"] = parser
    assert guard.allowed("https://beatsbydre.com/private/x")[0] is False
    assert guard.allowed("https://beatsbydre.com/public/x")[0] is True


# --- the extraction parser -------------------------------------------------------

PAGE = (
    "Beats Pill Portable Speaker. Colourway: Statement Red. "
    "Model number A3211. Price $149.99. Weight 680 g."
)


def extract(facts, page=PAGE, title="Beats Pill"):
    return parse_extract_tool_input(
        {"product_title": title, "facts": facts}, page_text=page
    )


# --- the excerpt reaches storage --------------------------------------------------


def fixture(tmp_path):
    conn = db.connect(tmp_path / "research.db")
    gateway = Gateway(conn, marketplace="EBAY_US", environment="sandbox", fees=FeeModel())
    sku = gateway.ingest_item(purchase_cost_cents=1800).sku
    return conn, gateway, sku


def test_the_excerpt_is_stored_with_the_fact(tmp_path):
    conn, gateway, sku = fixture(tmp_path)
    gateway.record_candidate_facts(
        sku, candidate_ref="cand-1", source_url="https://beatsbydre.com/p",
        authority="manufacturer",
        facts=[("Colourway: Statement Red", "identity", "Colourway: Statement Red")],
    )
    [row] = candidate_evidence(conn, sku)
    assert row["source_excerpt"] == "Colourway: Statement Red"


def test_a_transcription_stores_no_excerpt(tmp_path):
    """The operator is the witness; there is no quotation to keep, and the absence
    is the honest record rather than missing data."""
    conn, gateway, sku = fixture(tmp_path)
    gateway.record_candidate_facts(
        sku, candidate_ref="cand-1", source_url="https://beatsbydre.com/p",
        authority="manufacturer", facts=[("Colourway: Navy", "identity")],
        retrieval_method="operator_transcribed",
    )
    [row] = candidate_evidence(conn, sku)
    assert row["source_excerpt"] is None


# --- the adapter end to end -------------------------------------------------------


BODY = (
    b"<html><head><title>Beats Pill</title></head><body>"
    b"<p>Colourway: Statement Red</p><p>Model number A3211</p>"
    b"<p>Price $149.99</p></body></html>"
)

GOOD_EXTRACTION = {
    "product_title": "Beats Pill Portable Speaker",
    "facts": [
        {"claim": "Colourway: Statement Red", "domain": "identity",
         "excerpt": "Colourway: Statement Red"},
        {"claim": "Model number A3211", "domain": "identity",
         "excerpt": "Model number A3211"},
        {"claim": "Listed at $149.99", "domain": "retail", "excerpt": "Price $149.99"},
    ],
}


# --- the whole round, through the existing gates -----------------------------------


# --- a stalled transfer must end -------------------------------------------------


class Clock:
    """Advances only when a fake transfer stalls, so the test is instant."""

    def __init__(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def test_a_server_that_trickles_forever_is_abandoned(monkeypatch):
    """The failure this fixes. A comp round sat on one page indefinitely: the
    connection was fine and bytes kept arriving, so the per-read timeout reset on
    every chunk and never fired. Only a deadline ends that."""
    from resell.reasoning.adapters import fetch as fetch_module

    clock = Clock()
    monkeypatch.setattr(fetch_module.time, "monotonic", clock)
    # a byte, a 30-second gap, a byte, another gap -- never an end
    chunks = [b"<html>", 30.0, b"<p>a</p>", 30.0, b"<p>b</p>", 30.0, b"</html>"]

    with pytest.raises(FetchError, match="gave up after"):
        fetcher(FakeResponse(b""), chunks=chunks, clock=clock,
                deadline_seconds=45.0).fetch("https://slow.example/p")


def test_the_abandoned_url_is_named_in_the_error():
    """So a host that reliably stalls can be identified later rather than just
    felt as slowness."""
    from resell.reasoning.adapters import fetch as fetch_module

    clock = Clock()
    original = fetch_module.time.monotonic
    fetch_module.time.monotonic = clock
    try:
        with pytest.raises(FetchError) as caught:
            fetcher(FakeResponse(b""), chunks=[b"x", 60.0, b"y"], clock=clock,
                    deadline_seconds=45.0).fetch("https://slow.example/page")
    finally:
        fetch_module.time.monotonic = original
    assert "slow.example/page" in str(caught.value)
    assert "bytes read" in str(caught.value)


def test_a_prompt_page_is_unaffected_by_the_deadline():
    page = fetcher(
        FakeResponse(b"<html><body><p>Colourway: Navy</p></body></html>"),
        deadline_seconds=45.0,
    ).fetch("https://beatsbydre.com/p")
    assert "Colourway: Navy" in page.text


def test_the_byte_cap_stops_the_download_rather_than_the_keeping():
    """`response.content` had already read the whole body before `max_bytes` was
    consulted, so the cap bounded what was retained and not what was transferred."""
    huge = [b"x" * 1000 for _ in range(100)]
    page = fetcher(
        FakeResponse(b""), chunks=huge, max_bytes=5000,
    ).fetch("https://big.example/p")
    assert page.truncated
    assert page.bytes_read <= 5000


def test_robots_is_fetched_with_a_timeout(monkeypatch):
    """`RobotFileParser.read()` takes no timeout and cannot be given one. It runs
    before the page GET, so the bounded call was never even reached."""
    import httpx

    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["timeout"] = kwargs.get("timeout")
        raise httpx.ConnectTimeout("too slow")

    monkeypatch.setattr(httpx, "get", fake_get)
    allowed, why = PageFetcher(respect_robots=True).allowed("https://slow.example/p")
    assert not allowed
    assert "robots.txt" in why
    assert seen["url"] == "https://slow.example/robots.txt"
    assert seen["timeout"] is not None
