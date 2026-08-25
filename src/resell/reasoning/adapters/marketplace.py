"""Marketplace pages for comp research: the operator names them, this reads them.

Deliberately narrower than the identity adapter, which fetches *and* extracts. Here
the adapter returns page text and stops, because comp extraction needs things the
adapter has no business holding: the search that produced the page, whether that
search was after sales or offers, and a ledger entry per page. Retrieval is a page
source; interpretation belongs to the loop.

That split is also what leaves room for a search backend. When one arrives it
replaces how a URL is chosen and nothing downstream of it.

One host family is refused outright and it is the obvious one. Comparable sold
listings are eBay's core data, so the pull toward fetching them is structural --
which is exactly why the refusal lives in `PageFetcher.allowed`, below every
adapter, rather than in a rule each new adapter has to remember. An operator
reading a page themselves and transcribing it is a person using a site they are
entitled to use; `price comp-add` remains for that.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from resell.reasoning.adapters.fetch import FetchError, PageFetcher
from resell.reasoning.adapters.research import ResearchError, ResearchQuery
from resell.reasoning.authority import authority_for_url, fetch_permitted
from resell.reasoning.research import SourceAuthority

__all__ = [
    "MarketplaceDocument", "OperatorUrlMarketplaceAdapter",
    "SuppliedUrlsMarketplaceAdapter", "marketplace_for_url",
]


@dataclass(frozen=True)
class MarketplaceDocument:
    """One retrieved page of listings, before anything has been read out of it."""

    url: str
    marketplace: str
    authority: SourceAuthority
    page_text: str
    # The markup, for structured-data validation. See `FetchedPage.html`.
    raw_html: str = ""
    title: str = ""
    adapter: str = "fetch"
    truncated: bool = False


def marketplace_for_url(url: str) -> str:
    """The host, as the marketplace name.

    Not prettified into a brand name. `comp_observation.marketplace` is a
    provenance field and the host is the thing that is actually true about where a
    listing was seen; mapping "poshmark.com" to "Poshmark" would be a display
    decision baked into storage.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host.removeprefix("www.") or "unknown"


class OperatorUrlMarketplaceAdapter:
    """Operator names a results page; this fetches it and hands over the text."""

    provider = "fetch"

    def __init__(
        self,
        *,
        fetcher: PageFetcher | None = None,
        prompt=None,
        echo=None,
        respect_robots: bool = True,
        max_urls: int = 3,
    ):
        self._fetcher = fetcher or PageFetcher(respect_robots=respect_robots)
        self._prompt = prompt
        self._echo = echo
        self.max_urls = max_urls
        self.notes: list[str] = []

    def _ask(self, text: str) -> str:
        return (self._prompt or input)(text)

    def _say(self, text: str) -> None:
        (self._echo or print)(text)

    def cost_micros_per_lookup(self) -> int:
        """The fetch is free. Extraction is charged where the call is made."""
        return 0

    def search(self, query: ResearchQuery) -> list[MarketplaceDocument]:
        self._say(f"\n  SEARCH {query.query}")
        self._say(f"    why: {query.motivation}")
        self._say("    Paste results-page URLs, one per line. Each page is fetched and "
                  "read here.\n    Blank line to finish.")

        documents: list[MarketplaceDocument] = []
        for index in range(self.max_urls):
            raw = self._ask(f"    url {index + 1} (blank to finish): ").strip()
            if not raw:
                break
            document = self._retrieve(raw)
            if document is not None:
                documents.append(document)
        if not documents:
            self._say("    nothing retrieved for this search")
        return documents

    def _retrieve(self, url: str) -> MarketplaceDocument | None:
        # Checked here as well as in the fetcher so the operator gets the reason
        # immediately, with the alternative, rather than a bare refusal.
        permitted, why = fetch_permitted(url)
        if not permitted:
            self._say(f"      REFUSED {why}")
            self._say("      Use: resell price comp-add ... to record it by hand.")
            self.notes.append(f"{url}: {why}")
            return None

        try:
            page = self._fetcher.fetch(url)
        except FetchError as exc:
            self._say(f"      not usable: {exc}")
            self.notes.append(f"{url}: {exc}")
            return None

        if not page.text.strip():
            self._say("      the page yielded no readable text")
            self.notes.append(f"{page.final_url}: no readable text")
            return None

        authority, authority_why = authority_for_url(page.final_url)
        marketplace = marketplace_for_url(page.final_url)
        self._say(f"      {len(page.text)} chars read from {marketplace} "
                  f"· authority {authority}")
        if authority is SourceAuthority.UNKNOWN:
            # Not fatal for a comp. Authority gates what a candidate product may
            # *donate* to identification; a comp's weight comes from its
            # comparability rung and its price kind, which are judged separately.
            self._say(f"      note: {authority_why}")

        return MarketplaceDocument(
            url=page.final_url,
            marketplace=marketplace,
            authority=authority,
            page_text=page.text,
            raw_html=getattr(page, "html", "") or "",
            title=page.final_url,
            adapter=self.provider,
            truncated=page.truncated,
        )


class SuppliedUrlsMarketplaceAdapter(OperatorUrlMarketplaceAdapter):
    """The same fetching, with the URLs handed over in advance.

    `OperatorUrlMarketplaceAdapter` asks for them on stdin, which is right for a
    terminal and catastrophic anywhere else: running it inside the web server
    parked a request on `input()`, blocked on the terminal the server happened to
    be launched from, and the item never left pricing. A browser cannot answer a
    stdin prompt, and the failure looks like a hang rather than an error.

    So the UI passes a list instead. Every URL is still fetched, checked against
    the licence blocklist and read here -- only the asking is gone.
    """

    provider = "supplied"

    def __init__(self, urls, **kwargs):
        kwargs.pop("prompt", None)
        super().__init__(prompt=self._refuse_to_prompt, **kwargs)
        self.urls = [u.strip() for u in urls if u and u.strip()]
        self._unread = list(self.urls)

    @staticmethod
    def _refuse_to_prompt(_text: str) -> str:
        raise RuntimeError(
            "this adapter never prompts; URLs are supplied when it is constructed"
        )

    def search(self, query: ResearchQuery) -> list[MarketplaceDocument]:
        """Every supplied page, once, on the first query -- and nothing after.

        The planner proposes several searches; a search backend would answer each
        with different results. This adapter has one fixed set of pages and cannot
        answer a query at all, so returning them again for the second and third
        query is not more evidence -- it is the same page fetched again, extracted
        again, and paid for again.

        That is what happened: four planner queries against one pasted URL became
        four extraction calls on identical text and exhausted the stage's
        three-call budget on duplicated work.
        """
        if not self._unread:
            self._say(f"\n  (no more supplied pages for: {query.query})")
            return []

        self._say(f"\n  READING {len(self._unread)} supplied page(s)")
        documents = []
        for url in self._unread[: self.max_urls]:
            document = self._retrieve(url)
            if document is not None:
                documents.append(document)
        self._unread = []
        return documents


def get_marketplace_adapter(provider: str | None = None, **kwargs):
    name = (provider or "fetch").lower()
    if name != "fetch":
        raise ResearchError(
            name, "no marketplace adapter registered under that name. Available: fetch"
        )
    return OperatorUrlMarketplaceAdapter(**kwargs)


class SearchedMarketplaceAdapter(OperatorUrlMarketplaceAdapter):
    """Comp discovery from a search backend, by two routes at once.

    Route one is the old one: a marketplace page we are permitted to fetch is
    fetched, and the extraction stage reads listings out of it. That is unchanged
    and remains the stronger evidence, because the bytes were loaded.

    Route two is new and is the policy decision. A search index returns structured
    prices for listings on hosts we will not fetch -- eBay above all -- and those
    prices are real information about the asking market. They are recorded as
    asking comps with an unstated condition and provenance of `search_index`, and
    they are never realized sales: an inspection of five live Brave responses found
    69 eBay results and no sold or completed listings at all, because search
    engines do not index those pages.

    Direct comps are drained by the caller rather than returned from `search`,
    which returns documents. Duck-typed on purpose: every other adapter simply has
    no `take_direct_comps`, and the loop treats that as "none".
    """

    provider = "search"

    def __init__(self, backend, *, identity_terms=(), max_comps_per_search=12,
                 max_urls: int = 3, **kwargs):
        kwargs.pop("prompt", None)
        super().__init__(prompt=self._refuse_to_prompt, max_urls=max_urls, **kwargs)
        self.backend = backend
        self.identity_terms = tuple(identity_terms)
        self.max_comps_per_search = max_comps_per_search
        self.searches = 0
        self._direct: list = []
        # Spans the adapter's life, not one query: the planner's searches overlap
        # heavily and the same listing is in most of them.
        self._seen: set[str] = set()

    @staticmethod
    def _refuse_to_prompt(_text: str) -> str:
        raise RuntimeError("this adapter never prompts; it searches")

    def cost_micros_per_lookup(self) -> int:
        return self.backend.cost_micros_per_search()

    def take_direct_comps(self) -> list:
        """Everything found since the last drain. Empties the buffer.

        Drained rather than accumulated so a loop running several queries records
        each query's comps against that query, and a comp is never written twice.
        """
        found, self._direct = self._direct, []
        return found

    def search(self, query: ResearchQuery) -> list[MarketplaceDocument]:
        from datetime import UTC, datetime

        from resell.reasoning.adapters.search import hits_as_asking_comps

        self._say(f"\n  SEARCH {query.query}")
        hits = self.backend.find(query, limit=40)
        self.searches += 1

        # Priced results from hosts we cannot fetch become observations directly.
        # Nothing is fetched here and nothing pretends to have been.
        unfetchable = [h for h in hits if not fetch_permitted(h.url)[0]]
        observations, notes = hits_as_asking_comps(
            unfetchable, identity_terms=self.identity_terms,
            now=datetime.now(UTC), max_comps=self.max_comps_per_search,
            seen=self._seen,
        )
        self._direct.extend(observations)
        for note in notes:
            self._say(f"    {note}")
            self.notes.append(note)
        if observations:
            lo = min(o.price_cents for o in observations) / 100
            hi = max(o.price_cents for o in observations) / 100
            self._say(
                f"    {len(observations)} asking price(s) read from the search index "
                f"(${lo:.2f}-${hi:.2f}); condition unstated, not sales"
            )

        # Pages we may fetch still go the strong route.
        documents: list[MarketplaceDocument] = []
        for hit in hits:
            if len(documents) >= self.max_urls:
                break
            if not fetch_permitted(hit.url)[0]:
                continue
            document = self._retrieve(hit.url)
            if document is not None:
                documents.append(document)
        return documents
