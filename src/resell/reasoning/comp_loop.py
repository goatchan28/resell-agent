"""Comp research: identity -> static queries -> search -> filter -> claim.

No model calls. The stage that used to plan searches, read pages and judge
comparability with three LLM stages now does the same work with string rules,
and the replay that justified the change is in `bench/`: across the V1 baseline,
161 of 161 comps that reached a price came from the search index and none from
the fetch-and-extract route that cost 30% of the pricing budget.

One round of four fixed queries. No planner, no research ladder, no adaptive
rounds, no fallback to a model. If the queries are wrong the fix is the queries.

**The invariant this module exists to protect.** *"Set a price yourself" is a
statement about the market: we looked, and there is not enough to price from.* A
search that failed is not that. A query that raises, or a round the budget never
let search at all, leaves `incomplete_reason` set and `judging_complete` false,
and the orchestrator raises rather than concluding. A round where every search
succeeded and found nothing is still allowed to say so, because that is the case
the sentence is for.

Classification is deterministic and happens in memory, so there is no judging
stage that can run out of budget partway and leave a listing unjudged --
`unjudged` is empty by construction rather than by luck.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from resell import progress
from resell.db import log_event
from resell.pricing.comps import Comparability, ceiling_for_identity
from resell.reasoning.adapters.research import ResearchQuery
from resell.reasoning.adapters.search import hits_as_asking_comps
from resell.reasoning.comp_match import classify
from resell.reasoning.comp_queries import identity_terms, queries_for


class CompLoopError(RuntimeError):
    """The round could not run at all."""


@dataclass
class CompRoundOutcome:
    """What one round did, in the vocabulary the orchestrator and /ops read."""

    performed: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    deferral_reason: str = ""
    listings_found: int = 0
    comps_recorded: int = 0
    # Every comp is classified, so this equals `comps_recorded`. Kept because
    # `CompRoundIncomplete` reports it as the denominator for "how many came back
    # without a verdict", and that number should stay meaningful.
    promptable_recorded: int = 0
    claims_recorded: int = 0
    refused: list[str] = field(default_factory=list)
    ladder: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, int] = field(default_factory=dict)
    stopped: str | None = None
    stop_reason: str = ""
    notes: list[str] = field(default_factory=list)
    # Listings that went into judging and came back without a verdict. Always
    # empty here -- classification cannot fail partway -- and kept because the
    # orchestrator's incompleteness check reads it.
    unjudged: list[str] = field(default_factory=list)
    incomplete_reason: str = ""
    retail_hits_dropped: int = 0

    @property
    def judging_complete(self) -> bool:
        return not self.unjudged and not self.incomplete_reason


def _uid(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _identity_resolution(conn, sku: str) -> str:
    from resell.reasoning.research_loop import identity_resolution

    return str(identity_resolution(conn, sku))


MAX_COMPS_PER_SEARCH = 12

# Bounds on the diagnostic event, so one noisy round cannot write a huge row.
MAX_NOTES = 120
NOTE_CHARS = 400


# A comp is a resale listing. A retailer's shelf price is retail evidence, which
# is a different kind of thing and is deliberately not built in this pass.
#
# V1 never needed this rule because it had one by accident: it only took
# search-index comps from hosts it was *not permitted to fetch*, which in
# practice meant eBay, and 88% of every contributing comp in the record
# (324 of 369) is `ebay.com`. Feeding all priced hits to the extractor removed
# that accident, and the replay immediately priced a used moisturiser from
# Walmart, Walgreens, Ulta, CVS and Costco -- fifteen of its sixteen comps were
# shops selling it new.
#
# So the rule is explicit now, and small. Adding a marketplace is a deliberate
# act; a host nobody has vouched for contributes nothing.
RESALE_MARKETPLACES = frozenset({
    "ebay.com", "ebay.co.uk", "ebay.ca",
    "mercari.com", "poshmark.com", "depop.com", "grailed.com", "vinted.com",
    "offerup.com", "swappa.com", "reverb.com", "stockx.com", "goat.com",
    "therealreal.com", "vestiairecollective.com",
})


def is_resale_marketplace(host: str) -> bool:
    """Whether a price from this host is a resale comp at all."""
    host = (host or "").casefold().removeprefix("www.")
    return any(host == m or host.endswith(f".{m}") for m in RESALE_MARKETPLACES)


@dataclass
class Timing:
    """Wall-clock per phase. Recorded because the point of this path is speed."""

    search_s: float = 0.0
    extract_s: float = 0.0
    match_s: float = 0.0
    price_s: float = 0.0

    @property
    def total_s(self) -> float:
        return self.search_s + self.extract_s + self.match_s + self.price_s


@dataclass
class RoundV2:
    queries: tuple[str, ...] = ()
    hits: int = 0
    observations: list = field(default_factory=list)
    verdicts: dict = field(default_factory=dict)     # comp_id -> Verdict
    notes: list[str] = field(default_factory=list)
    timing: Timing = field(default_factory=Timing)
    retail_hits_dropped: int = 0

    @property
    def contributing(self) -> list:
        return [o for o in self.observations
                if self.verdicts[o.comp_id].comparability is not Comparability.EXCLUDED]

    @property
    def ladder(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.verdicts.values():
            out[str(v.comparability)] = out.get(str(v.comparability), 0) + 1
        return out


def plan_round(
    *,
    brand: str | None,
    model: str | None,
    title: str | None,
    backend,
    item_type: str | None = None,
    identity_resolution: str = "unattempted",
    item_evidence_ids: tuple[str, ...] = ("1",),
    max_comps_per_search: int = MAX_COMPS_PER_SEARCH,
    now: datetime | None = None,
) -> RoundV2:
    """Queries -> search -> observations -> verdicts. No database, no model."""
    now = now or datetime.now(UTC)
    ceiling = ceiling_for_identity(identity_resolution)
    round_ = RoundV2(queries=queries_for(brand, model, title))
    terms = identity_terms(brand, model, title)
    seen: set[str] = set()

    for query in round_.queries:  # noqa: B007 - `round_` is built up as we go
        started = time.perf_counter()
        try:
            hits = backend.find(ResearchQuery(query, "marketplace", "comp research"),
                                limit=40)
        except Exception as exc:  # noqa: BLE001 - one bad query must not lose the round
            round_.timing.search_s += time.perf_counter() - started
            round_.notes.append(f"search failed ({query}): {type(exc).__name__}: {exc}")
            continue
        round_.timing.search_s += time.perf_counter() - started
        round_.hits += len(hits)

        started = time.perf_counter()
        # Resale listings only. Retailers are dropped here rather than judged
        # later: a shop price is not a worse comp, it is not a comp.
        resale = [h for h in hits if is_resale_marketplace(h.host)]
        dropped = len(hits) - len(resale)
        if dropped:
            round_.retail_hits_dropped += dropped
        observations, notes = hits_as_asking_comps(
            resale, identity_terms=terms, now=now,
            max_comps=max_comps_per_search, seen=seen,
        )
        round_.timing.extract_s += time.perf_counter() - started
        round_.observations.extend(observations)
        round_.notes.extend(notes)

    started = time.perf_counter()
    for obs in round_.observations:
        round_.verdicts[obs.comp_id] = classify(
            brand=brand, model=model, item_title=title, comp_title=obs.title,
            item_evidence_ids=item_evidence_ids, ceiling=ceiling,
            item_type=item_type,
        )
    round_.timing.match_s = time.perf_counter() - started
    return round_


def run_comp_round(
    conn,
    gateway,
    sku: str,
    *,
    backend,
    lookup_budget=None,
    lookup_rates=None,
    max_comps_per_search: int = MAX_COMPS_PER_SEARCH,
):
    """One deterministic round, recorded. Returns V1's `CompRoundOutcome`.

    Returning V1's outcome type rather than inventing a second one is the whole
    integration: the orchestrator, `/ops` and `round_detail` already read it, and
    a parallel shape would mean teaching three readers about a fourth vocabulary.
    Fields V2 has no equivalent for -- the research ladder, retail counters,
    deferral of a planner's proposals -- are simply left at their defaults.

    **The invariant this function exists to protect.** "Set a price yourself" is a
    statement about the market: we looked, and there is not enough to price from.
    A search that *failed* is not that. V1 protects this with
    `judging_complete`, and V2 sets the same flag from the same idea -- a query
    that raised, or a round that was never allowed to search at all, leaves
    `incomplete_reason` set, and the orchestrator raises rather than concluding.

    One round of the fixed queries. No second round, no adaptive research.
    """
    import json as _json
    import sqlite3

    from resell import store_pricing as sp
    from resell.pricing.comps import CompClaim
    from resell.reasoning.budget import (
        LookupBudget, LookupRates, LookupSpend, check_lookup_plan,
    )

    outcome = CompRoundOutcome()
    identification = conn.execute(
        "SELECT brand, model, title, aspects FROM identification WHERE sku = ? "
        "ORDER BY version DESC LIMIT 1", (sku,),
    ).fetchone()
    if identification is None:
        outcome.stopped = "no_identification"
        outcome.incomplete_reason = f"{sku} has no identification to search from"
        return outcome

    aspects = _json.loads(identification["aspects"] or "{}") or {}
    types = aspects.get("Type") or aspects.get("Product Type") or []
    item_type = types[0] if types else None
    resolution = _identity_resolution(conn, sku)
    ceiling = ceiling_for_identity(resolution)
    evidence = tuple(
        str(r["id"]) for r in conn.execute(
            "SELECT id FROM evidence WHERE sku = ? AND subject = 'this_item' "
            "ORDER BY id LIMIT 3", (sku,))
    )
    if not evidence:
        # `validate_claim` refuses a claim citing nothing, so a round with no
        # observations to cite would record comps it could never claim.
        outcome.stopped = "no_observations"
        outcome.incomplete_reason = (
            f"{sku} has no observations to cite; run: resell item observe {sku}"
        )
        return outcome

    queries = queries_for(identification["brand"], identification["model"],
                          identification["title"])
    if not queries:
        outcome.stopped = "no_identity"
        outcome.stop_reason = "the item has no brand, model or title to search for"
        return outcome

    lookup_budget = lookup_budget or LookupBudget.from_env("pricing")
    lookup_rates = lookup_rates or LookupRates.from_env(
        getattr(backend, "provider", "search"))
    performed_count = conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'pricing'",
        (sku,),
    ).fetchone()[0]
    allocation = check_lookup_plan(
        lookup_budget, LookupSpend(lookups=performed_count, cost_micros=0),
        len(queries), lookup_rates,
    )
    outcome.deferral_reason = allocation.reason
    outcome.deferred = [queries[i] for i in allocation.deferred]
    if allocation.allowed <= 0:
        # Nothing was searched, so nothing was learned. Reported as incomplete
        # rather than as an empty market -- this is exactly the shape that made
        # MP-000039 ask its owner to name a price.
        outcome.stopped = "exhausted"
        outcome.stop_reason = allocation.reason
        outcome.incomplete_reason = (
            f"no pricing lookup was permitted for {sku}: {allocation.reason}"
        )
        return outcome

    terms = identity_terms(identification["brand"], identification["model"],
                           identification["title"])
    now = datetime.now(UTC)
    seen: set[str] = set()
    observations: list = []
    failures: list[str] = []

    for index, query in enumerate(queries[: allocation.allowed], start=1):
        progress.report(progress.Phase.SEARCHING,
                        f"search {index}/{allocation.allowed}: {query[:60]}")
        try:
            hits = backend.find(ResearchQuery(query, "marketplace", "comp research"),
                                limit=40)
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            failures.append(f"{query}: {type(exc).__name__}: {exc}")
            outcome.notes.append(f"lookup failed ({query}): {type(exc).__name__}: {exc}")
            continue

        resale = [h for h in hits if is_resale_marketplace(h.host)]
        found, notes = hits_as_asking_comps(
            resale, identity_terms=terms, now=now,
            max_comps=max_comps_per_search, seen=seen,
        )
        observations.extend(found)
        outcome.notes.extend(notes)
        if len(hits) - len(resale):
            outcome.notes.append(
                f"{len(hits) - len(resale)} priced result(s) from {query!r} were not "
                f"resale marketplaces and are not comps"
            )
        gateway.record_lookup(
            sku, provider=getattr(backend, "provider", "search"), query=query,
            motivation="comp research", evidence_ids=[int(e) for e in evidence],
            result_count=len(hits), scope="pricing",
            cost_micros=backend.cost_micros_per_search(),
        )
        outcome.performed.append(query)

    # A query that raised is a technical failure, and a technical failure must
    # never read as an empty market. Even a partially successful round is
    # reported incomplete: what it found is a partial answer, worth keeping and
    # never worth presenting as though the market had been searched.
    if failures:
        outcome.incomplete_reason = (
            f"{len(failures)} of {allocation.allowed} search(es) failed: "
            + "; ".join(f[:120] for f in failures[:3])
        )

    outcome.listings_found = len(observations)
    stored = []
    for obs in observations:
        try:
            sp.record_comp_observation(conn, obs)
        except sqlite3.IntegrityError:
            outcome.notes.append(f"already recorded: {obs.comp_id}")
        stored.append(obs)
        outcome.kinds[str(obs.price_kind)] = outcome.kinds.get(str(obs.price_kind), 0) + 1
    outcome.comps_recorded = len(stored)
    # Everything retrieved is classified, deterministically and in memory. There
    # is no judging stage to run out of budget partway, so `unjudged` stays empty
    # by construction rather than by luck.
    outcome.promptable_recorded = len(stored)

    for obs in stored:
        verdict = classify(
            brand=identification["brand"], model=identification["model"],
            item_title=identification["title"], comp_title=obs.title,
            item_evidence_ids=evidence, ceiling=ceiling, item_type=item_type,
        )
        try:
            sp.record_comp_claim(conn, CompClaim(
                claim_id=_uid("claim"), sku=sku, comp_id=obs.comp_id,
                comparability=verdict.comparability,
                item_citations=evidence, comp_citations=verdict.comp_citations,
                rationale=verdict.rationale, excluded_reason=verdict.excluded_reason,
            ), identity_resolution=resolution)
        except ValueError as exc:
            # The ladder ceiling, refused where it is enforced.
            outcome.refused.append(f"{obs.comp_id}: {exc}")
            continue
        except sqlite3.IntegrityError:
            # This listing was already judged for this item, by an earlier round
            # or by the V1 pipeline. The verdict on record stands: re-judging it
            # would be the same deterministic answer, and replacing it would
            # discard a claim the item may already have been priced from.
            #
            # Not a refusal and not a failure -- the comp is judged, which is the
            # only thing the round needs to be true before it may conclude.
            outcome.notes.append(f"already judged: {obs.comp_id}")
            outcome.claims_recorded += 1
            key = str(verdict.comparability)
            outcome.ladder[key] = outcome.ladder.get(key, 0) + 1
            continue
        outcome.claims_recorded += 1
        key = str(verdict.comparability)
        outcome.ladder[key] = outcome.ladder.get(key, 0) + 1

    if not stored and not outcome.incomplete_reason:
        outcome.stopped = "searched_not_found"
        outcome.stop_reason = (
            f"{len(outcome.performed)} search(es) returned no usable listing"
        )

    log_event(
        conn, "comp_research.round_complete",
        {"lookups": outcome.performed, "comps": outcome.comps_recorded,
         "claims": outcome.claims_recorded, "candidates": 0,
         "ladder": outcome.ladder, "kinds": outcome.kinds,
         "refused": len(outcome.refused)},
        item_id=sku,
    )
    _record_round_detail(conn, sku, outcome)
    return outcome


def _record_round_detail(conn, sku: str, outcome) -> None:
    """Everything the round noticed, kept where it can be read afterwards.

    `outcome.notes` was the whole diagnostic trail and it went to a browser flash
    or a terminal and then nowhere. Reconstructing MP-000047 meant re-running its
    searches, because nothing recorded that a page carrying the answer had been
    seen and skipped.

    Summary counts already live in `round_complete`. This is the reasons.

    Bounded, and stated rather than accidental: a round that noticed three
    hundred things must not put three hundred things in one row.
    """
    log_event(
        conn, "comp_research.round_detail",
        {
            "run_id": progress.current_run_id(),
            "notes": [n[:NOTE_CHARS] for n in outcome.notes][:MAX_NOTES],
            "performed": outcome.performed,
            "deferred": outcome.deferred[:MAX_NOTES],
            "deferral_reason": outcome.deferral_reason[:NOTE_CHARS],
            "stopped": outcome.stopped,
            "stop_reason": outcome.stop_reason[:NOTE_CHARS],
            "promptable_recorded": outcome.promptable_recorded,
            "unjudged": list(outcome.unjudged)[:40],
            "incomplete_reason": outcome.incomplete_reason[:NOTE_CHARS],
            "listings_found": outcome.listings_found,
            "comps_recorded": outcome.comps_recorded,
            "claims_recorded": outcome.claims_recorded,
            "refused": outcome.refused[:40],
            "ladder": outcome.ladder,
            "retail_hits_dropped": outcome.retail_hits_dropped,
        },
        item_id=sku,
    )
