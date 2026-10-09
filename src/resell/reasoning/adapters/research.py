"""The retrieval boundary.

Separate from the model adapter on purpose: the thing that fetches documents and the
thing that reasons about them are different services with different prices, different
licences and different failure modes.

What is left here is the vocabulary -- a query, an error, and how a fact was come
by. The document-and-facts adapters that used to sit behind it existed to feed a
model that read pages, and both the reader and the loop that called it are gone;
identity and pricing each ask a `SearchBackend` directly and read the index's own
summary. `RetrievalMethod` outlives them because it is what the *evidence* records,
and the distinction it draws -- who is the witness to what a page said -- did not
depend on which adapter did the retrieving.

No implementation fetches from eBay. Their updated API agreement restricts ingesting
Restricted API data into a third-party AI without written consent, and their user
agreement now prohibits LLM-driven scraping of the site. An eBay Catalog adapter can
be added once that question is answered in writing; nothing here depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from resell.reasoning.research import FactDomain, SourceAuthority

__all__ = ["ResearchError", "ResearchQuery", "RetrievalMethod"]


class ResearchError(RuntimeError):
    def __init__(self, provider: str, message: str):
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


@dataclass(frozen=True)
class ResearchQuery:
    query: str
    source_kind: str          # manufacturer | reference | general_web
    motivation: str
    max_results: int = 5


class RetrievalMethod(StrEnum):
    """How the content reached us, which is not the same as where it came from.

    `source_url` and `source_authority` record what is claimed about a document.
    This records who is doing the claiming. When an operator reads a page and types
    what it says, the system has verified nothing: not that the URL serves that
    content, not that the page is what they believe it is, not that the
    transcription is complete. That may still be the most reliable route available
    -- the operator is the principal here, not an anonymous scraper -- but it should
    never be indistinguishable from a fetch the system performed itself.
    """

    AUTOMATED_FETCH = "automated_fetch"
    OPERATOR_TRANSCRIBED = "operator_transcribed"
    # A search index's own title and summary, with no page loaded. Weaker than a
    # fetch and it should look weaker: nobody read the document, so a claim resting
    # on this is a claim about what a search engine said a page contains.
    SEARCH_INDEX = "search_index"
