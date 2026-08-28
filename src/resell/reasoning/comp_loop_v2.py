"""Comp research with no model calls in it.

The V1 round was plan (model) -> search -> fetch -> extract (model) -> judge
(model) -> claim, plus a retail sub-round with two more model calls. This is the
same round with the three model stages replaced by code:

    identity -> static queries -> search -> structured hits -> static filter -> claim

Nothing here takes a `ModelAdapter`. Zero LLM calls is a property of the
signature, not a promise in a docstring.

The extraction step is `hits_as_asking_comps`, unchanged and already in the tree.
It was doing all the work anyway: across the V1 baseline, 161 of 161 comps that
reached a price came from the search index and none from the fetch-and-extract
route that cost 30% of the pricing budget.

`plan_round` is pure and does the whole pipeline in memory, so the replay can run
it against stored evidence without a database. `run_comp_round_v2` is the same
thing with the recording attached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from resell.pricing.comps import Comparability, ceiling_for_identity
from resell.reasoning.comp_match import Verdict, classify
from resell.reasoning.comp_queries import identity_terms, queries_for

MAX_COMPS_PER_SEARCH = 12

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
    identity_resolution: str = "unattempted",
    item_evidence_ids: tuple[str, ...] = ("1",),
    max_comps_per_search: int = MAX_COMPS_PER_SEARCH,
    now: datetime | None = None,
) -> RoundV2:
    """Queries -> search -> observations -> verdicts. No database, no model."""
    from resell.reasoning.adapters.search import hits_as_asking_comps
    from resell.reasoning.adapters.research import ResearchQuery

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
        )
    round_.timing.match_s = time.perf_counter() - started
    return round_


def run_comp_round_v2(conn, gateway, sku: str, *, backend, **kwargs) -> RoundV2:
    """`plan_round`, recorded. Not wired into the orchestrator.

    Kept deliberately thin: the interesting code is pure and above, and this is
    the part that writes. Nothing calls it yet -- the replay measures `plan_round`
    and this exists so wiring it later is not a rewrite.
    """
    import sqlite3

    from resell import store_pricing as sp
    from resell.pricing.comps import CompClaim
    from resell.reasoning.comp_loop import _identity_resolution, _uid

    identification = conn.execute(
        "SELECT brand, model, title FROM identification WHERE sku = ? "
        "ORDER BY version DESC LIMIT 1", (sku,),
    ).fetchone()
    resolution = _identity_resolution(conn, sku)
    evidence = tuple(
        str(r["id"]) for r in conn.execute(
            "SELECT id FROM evidence WHERE sku = ? ORDER BY id LIMIT 3", (sku,))
    ) or ("1",)

    round_ = plan_round(
        brand=identification["brand"], model=identification["model"],
        title=identification["title"], backend=backend,
        identity_resolution=resolution, item_evidence_ids=evidence, **kwargs,
    )

    for obs in round_.observations:
        try:
            sp.record_comp_observation(conn, obs)
        except sqlite3.IntegrityError:
            round_.notes.append(f"already recorded: {obs.comp_id}")
        verdict: Verdict = round_.verdicts[obs.comp_id]
        try:
            sp.record_comp_claim(conn, CompClaim(
                claim_id=_uid("claim"), sku=sku, comp_id=obs.comp_id,
                comparability=verdict.comparability,
                item_citations=evidence, comp_citations=verdict.comp_citations,
                rationale=verdict.rationale, excluded_reason=verdict.excluded_reason,
            ), identity_resolution=resolution)
        except ValueError as exc:
            round_.notes.append(f"{obs.comp_id}: {exc}")
    return round_
