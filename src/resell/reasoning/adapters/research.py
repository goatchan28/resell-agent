"""The retrieval boundary.

Separate from the model adapter on purpose: the thing that fetches documents and the
thing that reasons about them are different services with different prices, different
licences and different failure modes.

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

__all__ = [
    "ADAPTERS", "ManualResearchAdapter", "ResearchAdapter", "ResearchError",
    "ResearchQuery", "RetrievalMethod", "RetrievedDocument", "RetrievedFact",
    "get_research_adapter",
]


# Enough for any plausible product page; a bound is what stops a misbehaving prompt
# from looping regardless of what it returns.
MAX_FACT_LINES = 200


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


@dataclass(frozen=True)
class RetrievedFact:
    """One fact about a candidate product. Never about the item on the table."""

    claim: str
    domain: FactDomain = FactDomain.IDENTITY


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


@dataclass(frozen=True)
class RetrievedDocument:
    candidate_ref: str
    title: str
    url: str
    authority: SourceAuthority
    facts: tuple[RetrievedFact, ...] = ()
    restriction: str | None = None      # set when the licence forbids model ingestion
    raw_excerpt: str = ""
    retrieval_method: RetrievalMethod = RetrievalMethod.AUTOMATED_FETCH

    @property
    def authority_is_asserted(self) -> bool:
        """True when nothing but a person's word establishes the source."""
        return self.retrieval_method is RetrievalMethod.OPERATOR_TRANSCRIBED


@runtime_checkable
class ResearchAdapter(Protocol):
    provider: str

    def search(self, query: ResearchQuery) -> list[RetrievedDocument]: ...

    def cost_micros_per_lookup(self) -> int: ...


class ManualResearchAdapter:
    """Operator-mediated retrieval.

    Prints the planned query and takes what the operator supplies. Not a
    placeholder for lack of ambition: it exercises the whole loop honestly, costs
    nothing, and sidesteps the question of which search provider is licensed for
    this use. An automated adapter implements the same two methods.
    """

    provider = "manual"

    def __init__(self, prompt=None, echo=None):
        # Resolved at call time rather than bound at definition, so the interactive
        # functions can be substituted for a scripted run without patching builtins.
        self._prompt = prompt
        self._echo = echo

    def _ask(self, text: str) -> str:
        return (self._prompt or input)(text)

    def _say(self, text: str) -> None:
        (self._echo or print)(text)

    def cost_micros_per_lookup(self) -> int:
        return 0

    def search(self, query: ResearchQuery) -> list[RetrievedDocument]:
        """Collect one result from the operator.

        Fields are ordered so that the free-text block comes last. Pasting several
        lines into a line-at-a-time prompt used to desync everything after it: a
        two-line page title fed its second line to the authority prompt, which
        silently accepted it and fell back to general_web, and the authority typed
        afterwards was recorded as a fact. Anything that can overflow now overflows
        into the field that expects many lines.
        """
        self._say(f"\n  LOOKUP [{query.source_kind}] {query.query}")
        self._say(f"    why: {query.motivation}")
        self._say("    Recorded as operator_transcribed: the system does not fetch the "
                  "page, so the URL,\n    authority and facts below are your account "
                  "of it, not something it verified.")

        url = self._ask("    result URL (blank to skip this lookup): ").strip()
        if not url:
            self._say("    skipped")
            return []
        title = self._ask("    page title (one line): ").strip()
        authority = self._ask_authority()

        self._say("    facts, one per line. Prefix a retail fact with $ so it stays out "
                  "of identification.")
        self._say("    End with '.' on its own line, or two blank lines.")
        facts: list[RetrievedFact] = []
        blanks = 0
        # A single blank used to end the loop, which truncated pasted blocks that
        # contained one. Requiring an explicit terminator fixed that and introduced a
        # worse failure: a prompt returning empty forever never stopped. Two blanks
        # end it, which is also what someone pressing enter twice expects, and the
        # cap means no input pattern can spin.
        for _ in range(MAX_FACT_LINES):
            try:
                line = self._ask("      ").strip()
            except EOFError:
                break
            if line == ".":
                break
            if not line:
                blanks += 1
                if blanks >= 2:
                    break
                continue
            blanks = 0
            if line.startswith("$"):
                facts.append(RetrievedFact(line[1:].strip(), FactDomain.RETAIL))
            else:
                facts.append(RetrievedFact(line, FactDomain.IDENTITY))

        if not facts:
            self._say("    no facts recorded; the document is not stored")
            return []

        self._say(f"    recorded {len(facts)} fact(s) from a {authority} source")
        return [
            RetrievedDocument(
                candidate_ref=f"cand-{abs(hash(url)) % 10**8:08d}",
                title=title or url,
                url=url,
                authority=authority,
                facts=tuple(facts),
                # Everything above is the operator's account of a page the system
                # never loaded. Recording that keeps a transcription distinguishable
                # from a fetch, which matters when a donated attribute is questioned.
                retrieval_method=RetrievalMethod.OPERATOR_TRANSCRIBED,
            )
        ]

    def _ask_authority(self) -> SourceAuthority:
        """Re-prompt rather than defaulting.

        Authority decides what a match may donate, so guessing it from an empty or
        mistyped answer silently changes what the page is allowed to contribute.
        """
        options = [str(a) for a in SourceAuthority if a is not SourceAuthority.UNKNOWN]
        for attempt in range(3):
            raw = self._ask(f"    authority [{'/'.join(options)}]: ").strip().lower()
            if not raw:
                self._say("      required: it decides what this page may contribute")
                continue
            matches = [o for o in options if o.startswith(raw)]
            if len(matches) == 1:
                return SourceAuthority(matches[0])
            if raw in options:
                return SourceAuthority(raw)
            self._say(f"      not recognised: {raw!r}"
                      + (f" (matches {matches})" if matches else ""))
        self._say("      recording as unknown, which donates nothing")
        return SourceAuthority.UNKNOWN


ADAPTERS: dict[str, type] = {
    "manual": ManualResearchAdapter,
    # "web": WebSearchAdapter,
    # "ebay_catalog": pending the licensing answer -- see the module docstring.
}


def get_research_adapter(provider: str | None = None, **kwargs) -> ResearchAdapter:
    name = (provider or "manual").lower()
    if name not in ADAPTERS:
        raise ResearchError(
            name, f"no adapter registered. Available: {', '.join(sorted(ADAPTERS))}"
        )
    return ADAPTERS[name](**kwargs)
