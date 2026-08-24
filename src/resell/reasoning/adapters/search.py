"""Finding pages, and nothing else.

The seam both research loops were missing. They could plan queries and read pages;
they could not turn a query into a URL, so every lookup ended at an operator typing
one in. This closes that and stops there: a query goes in, results come out, and
what happens to them -- fetch, extract, judge -- is unchanged above.

Deliberately not a document source. `ResearchAdapter` returns documents with facts
already extracted; this returns candidates for retrieval. Keeping it to URLs is what
lets one backend serve identification research and comp discovery at once, since
those two disagree about everything downstream and agree about this.

What a hit may and may not become
---------------------------------
A snippet is a search engine's description of a page. It is not the page, and a
fact drawn from it has no excerpt in any sense this codebase recognises -- the
bytes were never loaded. So snippets route; they do not testify. The one exception
is deliberate and narrow: a *structured* price, which Brave returns as schema.org
`offers`, is a datum rather than prose, and `hits_as_asking_comps` turns those into
asking observations with their provenance recorded as `search_index`. See the note
there; it is a policy decision, not an oversight.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from resell import progress
from resell.reasoning.adapters.research import ResearchError, ResearchQuery

__all__ = [
    "SEARCH_BACKENDS", "BraveSearchBackend", "NoSearchBackend", "SearchBackend",
    "SearchHit", "get_search_backend", "host_of",
]

# Brave bills per request, so the ceiling on results per request is free money.
MAX_RESULTS_PER_QUERY = 20


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".").removeprefix("www.")


@dataclass(frozen=True)
class SearchHit:
    """One result. Mostly a URL; occasionally a URL with a price attached."""

    url: str
    title: str = ""
    snippet: str = ""
    extra_snippets: tuple[str, ...] = ()
    rank: int = 0
    # Present only when the index itself carried structured commerce data. Never
    # parsed out of prose: a number lifted from a sentence is a guess about what
    # the sentence meant, and this field is read as a price by everything below.
    price_cents: int | None = None
    currency: str = "USD"
    # When the index last saw the page. Not when the listing was posted, and not
    # when it sold -- neither of those is knowable from here.
    page_age: datetime | None = None
    query: str = ""

    @property
    def host(self) -> str:
        return host_of(self.url)

    @property
    def priced(self) -> bool:
        return self.price_cents is not None and self.price_cents > 0

    @property
    def text(self) -> str:
        """Everything the index said about this page, as one block.

        Used for the record of what a price was read from. It is the index's
        words, not the page's, which is exactly why it is stored rather than
        quoted as an excerpt.
        """
        return "\n".join([self.title, self.snippet, *self.extra_snippets]).strip()


@runtime_checkable
class SearchBackend(Protocol):
    provider: str

    def find(self, query: ResearchQuery, *, limit: int = 10) -> list[SearchHit]: ...

    def cost_micros_per_search(self) -> int: ...


class NoSearchBackend:
    """The default. Refuses rather than returning nothing.

    Same reasoning as `NoRetrievalAdapter`: an empty result set is a claim that
    somebody looked and the web had nothing, which would move an item from
    `unattempted` to `searched_not_found` -- a fact about the object that would
    not be true.
    """

    provider = "none"

    def cost_micros_per_search(self) -> int:
        return 0

    def find(self, query: ResearchQuery, *, limit: int = 10) -> list[SearchHit]:
        raise ResearchError(
            self.provider,
            "no search backend is configured. Set RESELL_SEARCH_BACKEND=brave and "
            "BRAVE_API_KEY to enable autonomous discovery.",
        )


def _cents(value: object) -> int | None:
    """A price from structured data, or nothing.

    Refuses anything that is not already a number in a price field. Brave sends
    prices as strings ("78.0"), which parse cleanly; it also sends `gtin13` values
    like "Does Not Apply", and a looser parser eventually meets one of those in a
    field it trusts.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(str(value).replace(",", "").lstrip("$").strip())
    except (TypeError, ValueError):
        return None
    if amount <= 0 or amount > 10_000_000:
        return None
    return round(amount * 100)


def _page_age(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


class BraveSearchBackend:
    """Brave's Web Search API.

    Chosen over cheaper SERP proxies because it licenses results for exactly this
    use, which is the same reason eBay is not fetched: a source's terms are part of
    whether evidence may be used, not a detail to be worked around.

    Two shapes of commerce data come back and both are read. A result for a single
    listing carries `product.offers[]`; a result for a category or catalogue page
    carries `product_cluster[]`, each entry a listing with its own URL and price.
    The cluster is the more valuable of the two -- one request can return a dozen
    live asks -- and was the thing an inspection of the raw response turned up that
    the documentation does not mention.
    """

    provider = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0,
                 client=None):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "").strip()
        self.timeout = timeout
        self._client = client
        self.notes: list[str] = []

    def cost_micros_per_search(self) -> int:
        """$5.00 per 1,000 requests, as micros. Verified against Brave's plan page.

        A request, not a result: asking for 20 results costs the same as asking
        for 3, which is why `find` always asks for the maximum and trims locally.
        """
        return 5000

    def _get(self, params: dict) -> dict:
        import httpx

        client = self._client
        headers = {
            "X-Subscription-Token": self.api_key,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        try:
            if client is not None:
                response = client.get(self.endpoint, params=params, headers=headers)
            else:
                response = httpx.get(
                    self.endpoint, params=params, headers=headers, timeout=self.timeout
                )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - httpx raises several unrelated types
            raise ResearchError(self.provider, f"search failed: {exc}") from exc

    def find(self, query: ResearchQuery, *, limit: int = 10) -> list[SearchHit]:
        if not self.api_key:
            raise ResearchError(
                self.provider, "BRAVE_API_KEY is not set; no request was made"
            )

        progress.report(
            progress.Phase.SEARCHING, f"searching for {query.query[:60]!r}"
        )
        payload = self._get({
            "q": query.query,
            "count": MAX_RESULTS_PER_QUERY,
            # Up to five further excerpts per result. Free, and the specification
            # text -- "MPN · A3211 · Charger Included · No" -- lives in them.
            "extra_snippets": "true",
        })
        return self._hits(payload, query)[:limit]

    def _hits(self, payload: dict, query: ResearchQuery) -> list[SearchHit]:
        results = ((payload or {}).get("web") or {}).get("results") or []
        hits: list[SearchHit] = []
        for rank, result in enumerate(results):
            if not isinstance(result, dict):
                continue
            url = result.get("url") or ""
            if not url:
                continue
            snippets = tuple(
                s for s in (result.get("extra_snippets") or []) if isinstance(s, str)
            )
            age = _page_age(result.get("page_age"))
            product = result.get("product") if isinstance(result.get("product"), dict) else {}

            hits.append(SearchHit(
                url=url,
                title=result.get("title") or "",
                snippet=result.get("description") or "",
                extra_snippets=snippets,
                rank=rank,
                price_cents=_cents(product.get("price")),
                currency=self._currency(product),
                page_age=age,
                query=query.query,
            ))

            # Each cluster entry is its own listing with its own URL and price, so
            # it becomes its own hit rather than being flattened into the page it
            # was found on. Flattening would attribute a dozen different prices to
            # one URL, and nothing downstream could tell them apart.
            for entry in result.get("product_cluster") or []:
                if not isinstance(entry, dict):
                    continue
                entry_url = entry.get("url") or ""
                price = _cents(entry.get("price"))
                if not entry_url or price is None:
                    continue
                hits.append(SearchHit(
                    url=entry_url,
                    title=entry.get("name") or "",
                    snippet="",
                    rank=rank,
                    price_cents=price,
                    currency=self._currency(entry),
                    page_age=age,
                    query=query.query,
                ))
        return hits

    @staticmethod
    def _currency(product: dict) -> str:
        for offer in product.get("offers") or []:
            if isinstance(offer, dict) and offer.get("priceCurrency"):
                return str(offer["priceCurrency"])
        return "USD"


SEARCH_BACKENDS: dict[str, type] = {
    "none": NoSearchBackend,
    "brave": BraveSearchBackend,
}


def get_search_backend(provider: str | None = None, **kwargs) -> SearchBackend:
    name = (provider or os.environ.get("RESELL_SEARCH_BACKEND") or "none").lower()
    if name not in SEARCH_BACKENDS:
        raise ResearchError(
            name,
            f"no search backend registered. Available: {', '.join(sorted(SEARCH_BACKENDS))}",
        )
    return SEARCH_BACKENDS[name](**kwargs)


# --- search results as asking evidence ------------------------------------------
#
# The policy decision this module's docstring points at.
#
# A structured price from a search index is weaker evidence than a fetched listing
# and stronger than nothing, and the pricing engine already has a place for exactly
# that: an asking comp whose condition is unknown. What it must never become is a
# realized sale. An inspection of five real Brave responses found 69 eBay results,
# 33 with structured prices, and *zero* sold or completed listings -- search engines
# do not index eBay's sold pages. So `price_kind` here is a constant, not a guess.
#
# Condition is likewise absent: every `offers` object Brave returned carried exactly
# `url`, `priceCurrency` and `price`. Condition words appear in the prose, but on
# catalogue boilerplate ("Find many great new & used options...") that is present
# whatever is listed -- so reading one would be inventing a fact about a stranger's
# goods. `unknown` is the honest band and `unstated` the honest source.

# eBay's own related-items carousels come back inside `product_cluster`, so one
# search can yield two hundred priced entries, most of them accessories for the
# product rather than the product. The deterministic gate below removes what is
# certainly off-product; the judging stage removes what is merely not comparable.
# One distinctive term is the gate. It was two, which with exactly two terms meant
# *both* -- the strictest point on the curve, and where MP-000022 sat: "Bowflex"
# and "Adjustable", so every "Bowflex SelectTech 552 Dumbbells" was discarded for
# lacking the second word.
#
# Deliberately permissive now, because it is not the quality filter. It removes
# what is certainly a different product; the judging stage decides what is
# comparable, and it is much better at telling a dumbbell from a weight plate than
# a word count ever was.
MIN_IDENTITY_TERMS = 1
DEFAULT_MAX_COMPS_PER_SEARCH = 12


def _external_id(url: str) -> str:
    """A stable identity for the listing behind a URL.

    Marketplace URLs carry tracking parameters that change between searches, so
    the path is used and the query string discarded. Without this the same listing
    found twice is two comps and the sample silently double-counts.
    """
    path = urlsplit(url).path.strip("/")
    return path.rsplit("/", 1)[-1] or path or url


def _matches_identity(text: str, terms: tuple[str, ...]) -> bool:
    if not terms:
        return True
    folded = text.casefold()
    hits = sum(1 for t in terms if t.casefold() in folded)
    return hits >= min(MIN_IDENTITY_TERMS, len(terms))


def hits_as_asking_comps(
    hits,
    *,
    identity_terms: tuple[str, ...] = (),
    now: datetime,
    max_comps: int = DEFAULT_MAX_COMPS_PER_SEARCH,
    marketplace: str | None = None,
    seen: set[str] | None = None,
):
    """Priced hits, as asking observations. Returns (observations, notes).

    `seen` carries external ids already recorded, and is updated in place.

    `observed_at` is the index's page age when it has one and the capture time
    otherwise, so a price Brave last saw nine months ago is stale on arrival and
    the staleness qualifier fires without special-casing. The capture time is not
    lost: `created_at` on the row is written when the row is.
    """
    from resell.pricing.comps import (
        CompBasis, CompObservation, ConditionBand, ConditionSource,
        ModelVisibility, PriceKind, RetrievalMethod,
    )

    # The caller may own this so that dedup spans a whole round rather than one
    # query. A planner proposes several overlapping searches and the same listing
    # comes back in most of them; deduping per query recorded it once per query,
    # which the UNIQUE constraint on comp_id caught only after the round had
    # already written half its findings.
    seen = set() if seen is None else seen
    observations = []
    notes: list[str] = []
    skipped_offtopic = 0

    for hit in hits:
        if not hit.priced:
            continue
        if marketplace and hit.host != marketplace:
            continue
        if not _matches_identity(f"{hit.title} {hit.snippet}", identity_terms):
            skipped_offtopic += 1
            continue
        external_id = _external_id(hit.url)
        if external_id in seen:
            continue
        seen.add(external_id)

        observations.append(CompObservation(
            comp_id=f"srch-{hit.host.split('.')[0]}-{external_id}"[:64],
            marketplace=hit.host,
            external_id=external_id,
            price_kind=PriceKind.ASKING,        # never REALIZED; see the note above
            basis=CompBasis.ACTIVE_SIMILAR,
            price_cents=hit.price_cents,
            currency=hit.currency,
            observed_at=hit.page_age or now,
            condition_band=ConditionBand.UNKNOWN,
            condition_declared_raw=None,
            condition_source=ConditionSource.UNSTATED,
            # Not reported, which is not zero. The comparison basis follows from
            # this and the estimator flags the sample accordingly.
            shipping_cents=None,
            days_on_market=None,
            url=hit.url,
            title=hit.title or None,
            retrieval_method=RetrievalMethod.SEARCH_INDEX,
            adapter="brave",
            query_text=hit.query or None,
            # What the price was read from, kept as the index's words. It is not an
            # excerpt of the listing and is not offered as one.
            source_excerpt=hit.text[:2000] or None,
            # Brave licenses its results for this use, which is why these may reach
            # a judging prompt at all. The eBay refusal is about fetching eBay;
            # it is not a claim that no third party may describe a listing.
            model_visibility=ModelVisibility.FULL,
        ))
        if len(observations) >= max_comps:
            break

    if skipped_offtopic:
        notes.append(
            f"{skipped_offtopic} priced result(s) dropped as off-product: the title "
            f"did not carry {MIN_IDENTITY_TERMS} of {list(identity_terms)}"
        )
    if len(observations) >= max_comps:
        notes.append(f"kept the first {max_comps} priced results; more were available")
    return observations, notes
