"""Fetching one page, with the limits that make it safe to do unattended.

No model here and no judgement: a URL goes in, text comes out or an explanation of
why it did not. Everything vendor-shaped stays in the search backend above it and
everything interpretive stays in the extraction stage after it.

The bounds are the substance of this module. A fetcher without them is a way to
hang the CLI on a slow host, fill the database from a 200MB download, or annoy
somebody's server on the operator's behalf and under their IP address.

HTML to text is stdlib. `httpx` is already a dependency for the eBay client; adding
a parser library to strip tags off a product page is not worth the supply chain.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from resell import progress

__all__ = [
    "FetchError", "FetchedPage", "PageFetcher", "html_to_text", "USER_AGENT",
]

# Identifies the tool and points at a human. A fetcher that lies about who it is
# has no business complaining when it gets blocked.
USER_AGENT = (
    "resell-agent/0.1 (personal reselling tool; one operator, one item at a time)"
)

# Per socket operation: how long a single connect or read may stall.
DEFAULT_TIMEOUT_SECONDS = 20.0
# Wall clock for the whole retrieval, redirects and body included. The per-operation
# timeout does not bound this: a server that trickles one byte every 19 seconds
# resets the read timer forever, which is exactly how a comp round came to sit on
# one page indefinitely. A deadline is the only thing that actually ends it.
DEFAULT_DEADLINE_SECONDS = 45.0
# robots.txt is fetched before the page and had no timeout of any kind, so the
# stall happened before the bounded call was ever reached.
DEFAULT_ROBOTS_TIMEOUT_SECONDS = 10.0
# Enough for any product page's markup, and small enough that a surprise is cheap.
DEFAULT_MAX_BYTES = 2_000_000
TEXTUAL_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class FetchError(RuntimeError):
    """A page could not be retrieved, or should not have been."""


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status_code: int
    text: str
    bytes_read: int
    truncated: bool
    # The markup as fetched. `text` is what a model reads; this is what a
    # machine-readable claim lives in -- `html_to_text` strips <script>, and
    # `schema.org/Product` JSON-LD is inside one. Kept so an unknown shop can be
    # admitted on what its page states rather than on who it is.
    html: str = ""

    @property
    def redirected(self) -> bool:
        return _normalise(self.requested_url) != _normalise(self.final_url)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or url).removeprefix("www.")


def _normalise(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


class _TextExtractor(HTMLParser):
    """Visible text, with the furniture dropped.

    `script` and `style` contents are the important exclusions: a modern product
    page embeds its whole catalogue as JSON in a script tag, and feeding that to the
    extraction stage buries the visible specification under machine noise while
    tripling the token count.
    """

    SKIP = {"script", "style", "noscript", "template", "svg"}
    # Tags whose boundaries are sentence boundaries, so "Navy" and "Wool" from two
    # table cells do not become "NavyWool" and defeat the excerpt check.
    BREAK = {
        "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5",
        "h6", "section", "article", "header", "footer", "nav", "table", "dt", "dd",
        "option", "span",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skipping += 1
        elif tag == "title":
            self._in_title = True
        if tag in self.BREAK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1
        elif tag == "title":
            self._in_title = False
        if tag in self.BREAK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skipping:
            return
        if self._in_title:
            self.title += data
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    def text(self) -> str:
        joined = " ".join(self._parts)
        # Collapse runs of whitespace but keep single newlines: the extraction stage
        # quotes from this text and the parser compares whitespace-normalised, so
        # the only thing that matters here is that separate fields stay separate.
        joined = re.sub(r"[ \t]+", " ", joined)
        return re.sub(r"\n\s*", "\n", joined).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Visible text and the document title. Malformed markup yields what it can."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a broken page is a page, not a crash
        pass
    return parser.text(), " ".join(parser.title.split())


class PageFetcher:
    """One GET, bounded and robots-aware.

    `respect_robots` defaults on and the failure is closed: a robots.txt we cannot
    read leaves the question unanswered, and proceeding anyway would make the
    setting decorative. It can be turned off for a page the operator has explicitly
    named -- which is the whole of the first adapter's behaviour -- but that is the
    caller's decision to state, not this class's to assume.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        robots_timeout_seconds: float = DEFAULT_ROBOTS_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        respect_robots: bool = True,
        client=None,
    ):
        self.timeout_seconds = timeout_seconds
        self.deadline_seconds = deadline_seconds
        self.robots_timeout_seconds = robots_timeout_seconds
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self._client = client
        self._robots: dict[str, RobotFileParser | None] = {}
        # Hosts that have already cost us a timeout. A measured run spent 38 of
        # its 67 seconds on bestbuy.com: twenty seconds to a read timeout, then
        # eighteen more to a protocol error on a second URL at the same host. The
        # second one was pure waste -- the host had already demonstrated it would
        # not answer, and nothing about a different path changes that.
        self._dead_hosts: dict[str, str] = {}

    # --- robots ---------------------------------------------------------------

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        parser = RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            # `parser.read()` goes through urllib with no timeout argument and no
            # way to pass one. That is where a fetch actually hung -- blocked in
            # http.client's readline on a host that accepted the connection and
            # never finished the response -- and it happens before the bounded
            # page GET is reached. Fetching the bytes ourselves is the only way to
            # put a clock on it.
            parser.parse(self._robots_text(origin).splitlines())
        except Exception:  # noqa: BLE001 - unreadable is not permission
            parser = None
        self._robots[origin] = parser
        return parser

    def _robots_text(self, origin: str) -> str:
        import httpx

        progress.report(progress.Phase.FETCHING, f"checking {_host(origin)} robots.txt")

        response = httpx.get(
            f"{origin}/robots.txt",
            timeout=httpx.Timeout(self.robots_timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code >= 400:
            # A 404 is a real answer: nothing is disallowed. Anything else is not,
            # and the empty string would read as permission.
            if response.status_code == 404:
                return ""
            raise FetchError(f"HTTP {response.status_code} for robots.txt")
        return response.text[:DEFAULT_MAX_BYTES]

    def allowed(self, url: str) -> tuple[bool, str]:
        # Licensing first, and it is not subject to `respect_robots`. That flag
        # exists so an operator can name a page they have decided to read; it is
        # not a way to opt out of an agreement we are bound by.
        from resell.reasoning.authority import fetch_permitted

        permitted, why = fetch_permitted(url)
        if not permitted:
            return False, why

        if not self.respect_robots:
            return True, "robots checking is off for this fetch"
        parser = self._robots_for(url)
        if parser is None:
            return False, "robots.txt could not be read, so the page is left alone"
        if parser.can_fetch(USER_AGENT, url):
            return True, "permitted by robots.txt"
        return False, "robots.txt disallows this path for our user agent"

    # --- fetching -------------------------------------------------------------

    def _read_bounded(self, client, url: str, elapsed):
        """Stream the body, stopping at the byte cap or the deadline.

        `client.get()` reads the whole response before returning, so neither the
        byte cap nor any wall clock applied to it: `max_bytes` was measured on
        something already fully downloaded, and the read timeout restarts on every
        chunk that arrives. Reading chunk by chunk is what makes both real, and it
        is the only shape in which a stalled transfer can be abandoned.
        """
        import httpx

        chunks: list[bytes] = []
        size = 0
        with client.stream("GET", url) as response:
            if response.status_code >= 400 or not self._is_textual(response):
                response.close()
                return b"", response
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= self.max_bytes:
                    break
                if elapsed() > self.deadline_seconds:
                    raise FetchError(
                        f"gave up after {elapsed():.1f}s (deadline "
                        f"{self.deadline_seconds:.0f}s) with {size} bytes read from "
                        f"{url}: the server accepted the connection and kept the "
                        f"response open"
                    )
        return b"".join(chunks), response

    @staticmethod
    def _is_textual(response) -> bool:
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        return not content_type or any(
            content_type.startswith(t) for t in TEXTUAL_TYPES
        )

    def fetch(self, url: str) -> FetchedPage:
        import httpx

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise FetchError(f"{parts.scheme or 'no'} is not a fetchable scheme")
        if not parts.hostname:
            raise FetchError(f"no host in {url!r}")

        permitted, why = self.allowed(url)
        if not permitted:
            raise FetchError(f"not fetched: {why}")

        host = _host(url)
        if host in self._dead_hosts:
            progress.report(
                progress.Phase.FETCHING,
                f"skipping {host}: it already {self._dead_hosts[host]}",
            )
            raise FetchError(
                f"not fetched: {host} already {self._dead_hosts[host]} in this run"
            )

        client = self._client or httpx.Client(
            # Named rather than positional: one number for everything hides which
            # phase is slow, and the error should be able to say.
            timeout=httpx.Timeout(
                connect=self.timeout_seconds, read=self.timeout_seconds,
                write=self.timeout_seconds, pool=self.timeout_seconds,
            ),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
        )
        close_after = self._client is None
        started = time.monotonic()
        progress.report(progress.Phase.FETCHING, f"reading {_host(url)}")

        def elapsed() -> float:
            return time.monotonic() - started

        try:
            raw, response = self._read_bounded(client, url, elapsed)
        except FetchError as exc:
            self._dead_hosts[host] = f"timed out after {elapsed():.0f}s"
            progress.report(
                progress.Phase.FETCHING,
                f"{host} timed out after {elapsed():.0f}s; skipping it for the "
                f"rest of this run",
                ok=False,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - httpx has many; none should escape
            self._dead_hosts[host] = f"failed after {elapsed():.0f}s ({type(exc).__name__})"
            progress.report(
                progress.Phase.FETCHING,
                f"{host} failed after {elapsed():.0f}s "
                f"({type(exc).__name__}); skipping it for the rest of this run",
                ok=False,
            )
            raise FetchError(
                f"{type(exc).__name__} after {elapsed():.1f}s on {url}: {exc}"
            ) from exc
        finally:
            if close_after:
                client.close()

        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} from {url}")

        content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        if content_type and not any(content_type.startswith(t) for t in TEXTUAL_TYPES):
            raise FetchError(
                f"content-type {content_type} is not a page; nothing was read"
            )

        truncated = len(raw) >= self.max_bytes
        body = raw[: self.max_bytes].decode(response.encoding or "utf-8", errors="replace")
        text, _title = html_to_text(body)
        final_url = str(response.url)

        return FetchedPage(
            html=body,
            requested_url=url,
            final_url=final_url,
            status_code=response.status_code,
            text=text,
            bytes_read=min(len(raw), self.max_bytes),
            truncated=truncated,
        )
