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
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

__all__ = [
    "FetchError", "FetchedPage", "PageFetcher", "html_to_text", "USER_AGENT",
]

# Identifies the tool and points at a human. A fetcher that lies about who it is
# has no business complaining when it gets blocked.
USER_AGENT = (
    "resell-agent/0.1 (personal reselling tool; one operator, one item at a time)"
)

DEFAULT_TIMEOUT_SECONDS = 20.0
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

    @property
    def redirected(self) -> bool:
        return _normalise(self.requested_url) != _normalise(self.final_url)


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
        max_bytes: int = DEFAULT_MAX_BYTES,
        respect_robots: bool = True,
        client=None,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self._client = client
        self._robots: dict[str, RobotFileParser | None] = {}

    # --- robots ---------------------------------------------------------------

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        parser = RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            parser.read()
        except Exception:  # noqa: BLE001 - unreadable is not permission
            parser = None
        self._robots[origin] = parser
        return parser

    def allowed(self, url: str) -> tuple[bool, str]:
        if not self.respect_robots:
            return True, "robots checking is off for this fetch"
        parser = self._robots_for(url)
        if parser is None:
            return False, "robots.txt could not be read, so the page is left alone"
        if parser.can_fetch(USER_AGENT, url):
            return True, "permitted by robots.txt"
        return False, "robots.txt disallows this path for our user agent"

    # --- fetching -------------------------------------------------------------

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

        client = self._client or httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
        )
        close_after = self._client is None
        try:
            response = client.get(url)
        except Exception as exc:  # noqa: BLE001 - httpx has many; none should escape
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
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

        raw = response.content
        truncated = len(raw) > self.max_bytes
        body = raw[: self.max_bytes].decode(response.encoding or "utf-8", errors="replace")
        text, _title = html_to_text(body)
        final_url = str(response.url)

        return FetchedPage(
            requested_url=url,
            final_url=final_url,
            status_code=response.status_code,
            text=text,
            bytes_read=min(len(raw), self.max_bytes),
            truncated=truncated,
        )
