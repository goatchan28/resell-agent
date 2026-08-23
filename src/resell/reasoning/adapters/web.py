"""Retrieval where the operator chooses the page and the system reads it.

The manual adapter asks for a URL, a title, an authority and then every fact, typed
by hand. This asks for the URL and does the rest: fetch the page, extract the facts
through a model, resolve the authority from the final host. It is the same
`ResearchAdapter` protocol and `run_round` cannot tell the difference.

Why this shape first. The bottleneck in the manual workflow is reading a page and
retyping it, not finding it -- and searching is the part that carries a licensing
question, needs an account, and would arrive as a second unvalidated variable. So
the search step stays with the operator for now and slots in behind `SearchBackend`
later, while everything after it becomes the thing that gets exercised.

What changes in the record, and it matters: these facts are `automated_fetch`, not
`operator_transcribed`. The system loaded the bytes and can quote them, so a donated
attribute is checkable against text rather than resting on somebody's recollection.
In exchange the operator is no longer the witness, which is why every extracted fact
must carry an excerpt that is actually present in what was fetched.
"""

from __future__ import annotations

from dataclasses import dataclass

from resell.reasoning.adapters.fetch import FetchedPage, FetchError, PageFetcher
from resell.reasoning.adapters.research import (
    MAX_FACT_LINES,
    ResearchError,
    ResearchQuery,
    RetrievalMethod,
    RetrievedDocument,
    RetrievedFact,
)
from resell.reasoning.authority import authority_for_url
from resell.reasoning.research import FactDomain, SourceAuthority

__all__ = ["ExtractionOutcome", "OperatorUrlResearchAdapter", "extract_facts"]


@dataclass
class ExtractionOutcome:
    facts: tuple[RetrievedFact, ...]
    title: str
    notes: list[str]
    call_id: int | None = None


def extract_facts(
    conn,
    sku: str,
    page: FetchedPage,
    query: ResearchQuery,
    *,
    model_adapter,
    budget=None,
    spent=None,
) -> ExtractionOutcome:
    """Run the extraction stage over one fetched page.

    Ledgered and budgeted exactly like planning and matching, under its own purpose
    so its spend is separable -- this is the stage most likely to move to a local
    model, and the case for moving it is an accounting question.

    A page that yields nothing usable returns no facts and says why. That is not a
    failure to handle here: `run_round` already treats a document with no facts as a
    document not worth storing.
    """
    from resell.reasoning.budget import StageBudget, StageSpend, check, estimate_cost
    from resell.reasoning.ledger import CallStatus, begin_call, finalize_call
    from resell.reasoning.stages import extraction_stage, page_body_for_extraction
    from resell.reasoning.tools import parse_extract_tool_input

    budget = budget or StageBudget.from_env("research_extract")
    spent = spent or StageSpend()

    request = extraction_stage(
        page_text=page.text, url=page.final_url,
        query=query.query, motivation=query.motivation,
        max_output_tokens=budget.max_output_tokens,
    )
    rates = model_adapter.rates()
    estimate = estimate_cost(model_adapter.estimate_input_tokens(request), budget, rates)
    check(budget, spent, estimate)

    call_id = begin_call(
        conn, sku, purpose="research_extract", provider=model_adapter.provider,
        model=model_adapter.model, estimated_cost_micros=estimate.worst_case_micros,
        rate_basis=str(rates.basis), request_key=request.replay_key(),
    )
    try:
        result = model_adapter.run(request)
    except Exception as exc:  # noqa: BLE001 - AdapterError and anything it wraps
        finalize_call(conn, call_id, status=CallStatus.PROVIDER_ERROR, error=str(exc)[:2000])
        raise ResearchError("fetch", f"extraction failed: {exc}") from exc

    # Checked against the page body alone -- not the whole page, and not the whole
    # prompt. Beyond the truncation point is text the model never saw, so a
    # quotation from there cannot be one; and the prompt also carries the URL, the
    # query and the motivation, which are our words rather than the page's.
    extracted = parse_extract_tool_input(
        result.tool_input, page_text=page_body_for_extraction(page.text)
    )
    finalize_call(
        conn, call_id, status=CallStatus.COMPLETED,
        input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
        cost_micros=rates.cost_micros(result.usage.input_tokens, result.usage.output_tokens),
        latency_ms=result.latency_ms, response=result.raw_response,
        raw_usage=result.usage.raw,
        error="; ".join(extracted.malformed)[:2000] if extracted.malformed else None,
    )

    facts = tuple(
        RetrievedFact(claim=claim, domain=FactDomain(domain), excerpt=excerpt)
        for claim, domain, excerpt in extracted.facts[:MAX_FACT_LINES]
    )
    return ExtractionOutcome(
        facts=facts,
        title=extracted.product_title,
        notes=list(extracted.malformed),
        call_id=call_id,
    )


class OperatorUrlResearchAdapter:
    """Operator names the page; this fetches and extracts it.

    Holds `conn` and `sku` because extraction is a ledgered model call against an
    item, and the `ResearchAdapter` protocol passes neither. That is a sign the
    protocol will want widening once a search backend arrives and retrieval stops
    being a per-item conversation -- worth doing then, with two implementations to
    generalise from, rather than guessed at now.
    """

    provider = "fetch"

    def __init__(
        self,
        conn=None,
        sku: str = "",
        *,
        model_adapter=None,
        fetcher: PageFetcher | None = None,
        prompt=None,
        echo=None,
        respect_robots: bool = True,
        max_urls: int = 3,
    ):
        self.conn = conn
        self.sku = sku
        self._model_adapter = model_adapter
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
        """The fetch is free; the extraction call is charged where it happens.

        Counting an estimate of the model call here would double-count it against
        the lookup budget, which exists to bound retrieval, while the stage budget
        already bounds inference.
        """
        return 0

    def search(self, query: ResearchQuery) -> list[RetrievedDocument]:
        if self.conn is None or not self.sku:
            raise ResearchError(
                self.provider,
                "no database handle or sku; this adapter records a ledgered model "
                "call and needs both",
            )
        if self._model_adapter is None:
            raise ResearchError(
                self.provider, "no model adapter supplied for fact extraction"
            )

        self._say(f"\n  LOOKUP [{query.source_kind}] {query.query}")
        self._say(f"    why: {query.motivation}")
        self._say("    Paste result URLs, one per line. The page is fetched and read "
                  "here, so you do not\n    need to type the facts. Blank line to "
                  "finish.")

        documents: list[RetrievedDocument] = []
        for index in range(self.max_urls):
            raw = self._ask(f"    url {index + 1} (blank to finish): ").strip()
            if not raw:
                break
            document = self._retrieve(raw, query)
            if document is not None:
                documents.append(document)
        if not documents:
            self._say("    nothing retrieved for this lookup")
        return documents

    def _retrieve(self, url: str, query: ResearchQuery) -> RetrievedDocument | None:
        try:
            page = self._fetcher.fetch(url)
        except FetchError as exc:
            self._say(f"      not usable: {exc}")
            self.notes.append(f"{url}: {exc}")
            return None

        if page.redirected:
            # Authority is resolved from where we ended up, never from what was
            # typed. A link that lands somewhere else is the ordinary case on the
            # web and the interesting case for anyone trying to borrow a domain's
            # standing.
            self._say(f"      redirected to {page.final_url}")

        authority, why = authority_for_url(page.final_url)
        self._say(f"      {len(page.text)} chars read · authority {authority}")
        if authority is SourceAuthority.UNKNOWN:
            self._say(f"      {why}")
            self.notes.append(f"{page.final_url}: {why}")

        if not page.text.strip():
            self._say("      the page yielded no readable text")
            self.notes.append(f"{page.final_url}: no readable text")
            return None

        outcome = extract_facts(
            self.conn, self.sku, page, query, model_adapter=self._model_adapter
        )
        for note in outcome.notes:
            self._say(f"      DROPPED {note[:100]}")
        self.notes.extend(outcome.notes)

        if not outcome.facts:
            self._say("      no usable facts; the document is not stored")
            return None

        identity = sum(1 for f in outcome.facts if f.domain is FactDomain.IDENTITY)
        self._say(f"      {identity} identity + {len(outcome.facts) - identity} retail "
                  f"fact(s), each quoting the page")

        return RetrievedDocument(
            candidate_ref=f"cand-{abs(hash(page.final_url)) % 10**8:08d}",
            title=outcome.title or page.final_url,
            url=page.final_url,
            authority=authority,
            facts=outcome.facts,
            retrieval_method=RetrievalMethod.AUTOMATED_FETCH,
        )


class SearchedResearchAdapter(OperatorUrlResearchAdapter):
    """The same fetch-and-extract, with the URLs found rather than typed.

    This is the whole of autonomous identification research: the planner already
    decided what to look for and the extraction stage already reads what comes
    back. The only thing missing was a way to turn a query into pages, and the
    operator was standing in that gap.

    Forbidden hosts are filtered here as well as in the fetcher, because a general
    web search returns eBay results constantly and a refusal per result would
    otherwise be the loudest thing in the log. What is dropped is counted and said
    once.
    """

    provider = "search"

    def __init__(self, backend, conn=None, sku: str = "", *, max_urls: int = 3, **kwargs):
        kwargs.pop("prompt", None)
        super().__init__(conn=conn, sku=sku, max_urls=max_urls, **kwargs)
        self.backend = backend
        self.searches = 0

    def cost_micros_per_lookup(self) -> int:
        """What the search costs. The fetch is free and extraction is charged
        where the model call is made, so this is the retrieval price and nothing
        else -- which is what the lookup budget is bounding."""
        return self.backend.cost_micros_per_search()

    def search(self, query: ResearchQuery) -> list[RetrievedDocument]:
        from resell.reasoning.authority import fetch_permitted

        if self.conn is None or not self.sku:
            raise ResearchError(
                self.provider, "no database handle or sku; extraction is ledgered"
            )
        if self._model_adapter is None:
            raise ResearchError(self.provider, "no model adapter for fact extraction")

        self._say(f"\n  LOOKUP [{query.source_kind}] {query.query}")
        hits = self.backend.find(query, limit=self.max_urls * 4)
        self.searches += 1

        allowed, refused = [], 0
        for hit in hits:
            permitted, _ = fetch_permitted(hit.url)
            if permitted:
                allowed.append(hit)
            else:
                refused += 1
        if refused:
            self._say(f"    {refused} result(s) skipped: their host must not be fetched")
            self.notes.append(f"{refused} result(s) on forbidden hosts for: {query.query}")

        documents: list[RetrievedDocument] = []
        for hit in allowed[: self.max_urls]:
            document = self._retrieve(hit.url, query)
            if document is not None:
                documents.append(document)
        if not documents:
            self._say("    nothing usable retrieved for this lookup")
        return documents
