"""The comp research loop: plan, retrieve, extract, judge, record.

Same shape as identity research and the same rule at its centre -- the model
proposes, deterministic code decides what the proposal is permitted to become. What
differs is what is at stake. Identity research that goes wrong attaches the wrong
attributes to an object. Comp research that goes wrong produces a *number*, and a
number carries an authority that prose does not.

So three things are computed here rather than accepted from the model:

  sold or asking   from the text the extractor quoted -- see `comp_reading`
  condition band   from the seller's own wording, mapped, defaulting to unknown
  the ladder       `record_comp_claim` refuses any rung above the item's identity
                   ceiling, whatever the judge said

**This module does not price anything.** It records observations and claims, and
stops. `price recommend` reads them, `build_strategies` turns them into options and
`price propose` commits to one. Nothing here computes a central estimate, suggests a
figure, or so much as sorts the comps by price -- a research stage that emits a
number is a pricing engine nobody reviewed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from resell import store_pricing as sp
from resell import progress
from resell.db import log_event
from resell.pricing.comps import (
    CompClaim,
    CompObservation,
    Comparability,
    ConditionSource,
    ModelVisibility,
    RetrievalMethod,
    ceiling_for_identity,
)
from resell.reasoning.adapters import AdapterError, ModelAdapter, get_adapter
from resell.reasoning.adapters.research import ResearchError, ResearchQuery
from resell.reasoning.budget import (
    BudgetExceeded,
    LookupBudget,
    LookupRates,
    LookupSpend,
    StageBudget,
    StageSpend,
    check,
    check_lookup_plan,
    estimate_cost,
)
from resell.reasoning.comp_reading import (
    band_for_declared_condition, basis_for, read_price_kind, retail_from_source,
)
from resell.reasoning.ledger import CallStatus, begin_call, finalize_call
from resell.reasoning.stages import (
    comp_judging_stage,
    comp_planning_stage,
    render_observations,
)
from resell.reasoning.tools import (
    parse_comp_judge_tool_input,
    parse_comp_plan_tool_input,
)


class CompLoopError(RuntimeError):
    pass


@dataclass
class CompRoundOutcome:
    plan: object | None = None
    performed: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    deferral_reason: str = ""
    listings_found: int = 0
    comps_recorded: int = 0
    claims_recorded: int = 0
    candidates_offered: int = 0
    refused: list[str] = field(default_factory=list)
    ladder: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, int] = field(default_factory=dict)
    downgraded: list[str] = field(default_factory=list)
    withheld_from_model: list[str] = field(default_factory=list)
    stopped: str | None = None
    stop_reason: str = ""
    notes: list[str] = field(default_factory=list)
    # The extraction budget ran out partway. Not a failure: the round keeps what
    # it retrieved, judges it, and stops searching for pages it cannot read.
    stopped_early: bool = False


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


STAGE_SAYS: dict[str, str] = {
    "comp_plan": "working out what to search for",
    "comp_extract": "reading the listings on the page",
    "comp_judge": "deciding which are comparable",
}


def _run_stage(conn, sku, adapter: ModelAdapter, request, *, purpose: str,
               budget: StageBudget, spent: StageSpend):
    """A ledgered, budgeted model call. Same as every other stage."""
    rates = adapter.rates()
    estimate = estimate_cost(adapter.estimate_input_tokens(request), budget, rates)
    check(budget, spent, estimate)
    said = STAGE_SAYS.get(purpose, purpose.replace("_", " "))

    call_id = begin_call(
        conn, sku, purpose=purpose, provider=adapter.provider, model=adapter.model,
        estimated_cost_micros=estimate.worst_case_micros, rate_basis=str(rates.basis),
        request_key=request.replay_key(),
    )
    try:
        with progress.timed(progress.Phase.THINKING, said):
            result = adapter.run(request)
    except AdapterError as exc:
        finalize_call(conn, call_id, status=CallStatus.PROVIDER_ERROR, error=str(exc)[:2000])
        raise CompLoopError(str(exc)) from exc

    finalize_call(
        conn, call_id, status=CallStatus.COMPLETED,
        input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
        cost_micros=rates.cost_micros(result.usage.input_tokens, result.usage.output_tokens),
        latency_ms=result.latency_ms, response=result.raw_response,
        raw_usage=result.usage.raw,
    )
    return result


def _stage_spend(conn, sku, purpose) -> StageSpend:
    from resell.reasoning.vision import spend_so_far

    return spend_so_far(conn, sku, purpose)


def render_identification(conn: sqlite3.Connection, sku: str) -> str:
    """What the item is believed to be, as the planner and judge see it."""
    from resell.gateway import current_identification

    row = current_identification(conn, sku)
    if row is None:
        return "(nothing identified yet)"
    lines = []
    for field_name in ("brand", "model", "variant", "title", "category_id", "condition_id"):
        if row[field_name]:
            lines.append(f"{field_name}: {row[field_name]}")
    if row["aspects"]:
        for name, values in sorted(json.loads(row["aspects"]).items()):
            lines.append(f"aspect {name}: {' + '.join(map(str, values))}")
    mode = row["mode"] if "mode" in row.keys() else None
    if mode:
        lines.append(f"identification mode: {mode}")
    return "\n".join(lines) or "(nothing identified yet)"


def render_recorded_comps(conn: sqlite3.Connection, sku: str) -> str:
    """Comps already on file, so the planner does not re-find what it has.

    Prices are shown because the planner needs to see where the sample is thin,
    not so it can form a view about the number. It has no tool with which to say
    one.
    """
    scored = sp.load_scored_comps(conn, sku)
    if not scored:
        return ""
    lines = []
    for entry in scored:
        obs = entry.observation
        lines.append(
            f"[{obs.comp_id}] {obs.price_kind.verb} {obs.price_cents / 100:.2f} "
            f"({obs.condition_band}) {entry.claim.comparability} - {(obs.title or '')[:60]}"
        )
    return "\n".join(lines)


def render_retrieved(comps: list[CompObservation]) -> str:
    """Retrieved listings as the judge sees them. Field names are the citable ones."""
    lines = []
    for obs in comps:
        shipping = (
            f"{obs.shipping_cents / 100:.2f}" if obs.shipping_cents is not None
            else "not stated"
        )
        lines.append(
            f"[{obs.comp_id}]\n"
            f"  title: {obs.title}\n"
            f"  price: {obs.price_cents / 100:.2f} ({obs.price_kind})\n"
            f"  shipping: {shipping}\n"
            f"  condition_text: {obs.condition_declared_raw or 'not stated'} "
            f"({obs.condition_band})\n"
            f"  marketplace: {obs.marketplace}"
        )
    return "\n".join(lines)


def _prior_lookups(conn, sku: str) -> list[str]:
    return [
        row["query"] for row in conn.execute(
            "SELECT query FROM research_lookup WHERE sku = ? AND scope = 'pricing'",
            (sku,),
        )
    ]


# --- the round ---------------------------------------------------------------


def run_comp_round(
    conn: sqlite3.Connection,
    gateway,
    sku: str,
    *,
    model_adapter: ModelAdapter | None = None,
    research_adapter=None,
    provider: str | None = None,
    stage_budget: StageBudget | None = None,
    lookup_budget: LookupBudget | None = None,
    lookup_rates: LookupRates | None = None,
    dry_run: bool = False,
    propose_only: bool = False,
) -> CompRoundOutcome:
    """One comp research round. Records comps and claims; recommends nothing.

    `propose_only` writes comp *candidates* instead of claims: the judge's rung is
    recorded as a proposal and an operator accepts or rejects it in one action.
    That is what the UI runs, so the observation/claim split never surfaces as two
    things to do. The CLI keeps the direct path, where the judgement stands on its
    own.
    """
    from resell.gateway import observations_in_scope

    outcome = CompRoundOutcome()
    observations = observations_in_scope(conn, sku)
    if not observations:
        raise CompLoopError(f"{sku} has no observations; run: resell item observe {sku}")
    if research_adapter is None:
        raise CompLoopError("no retrieval adapter supplied")

    model_adapter = model_adapter or get_adapter(provider)
    stage_budget = stage_budget or StageBudget.from_env("comp_research")
    # A separate allowance from identity research, so a hard-to-identify item
    # cannot spend the comp budget before pricing has started.
    lookup_budget = lookup_budget or LookupBudget.from_env("pricing")
    lookup_rates = lookup_rates or LookupRates.from_env(research_adapter.provider)

    resolution = _identity_resolution(conn, sku)
    ceiling = ceiling_for_identity(resolution)

    performed_count = conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'pricing'",
        (sku,),
    ).fetchone()[0]
    if performed_count >= lookup_budget.max_lookups:
        outcome.stopped = "exhausted"
        outcome.stop_reason = (
            f"{performed_count} pricing lookup(s) already performed, which is the "
            f"budget for this item"
        )
        return outcome

    # --- plan -----------------------------------------------------------------
    request = comp_planning_stage(
        identification=render_identification(conn, sku),
        observations=render_observations(observations),
        identity_resolution=resolution,
        existing_comps=render_recorded_comps(conn, sku),
        prior_lookups="\n".join(_prior_lookups(conn, sku)),
        max_output_tokens=stage_budget.max_output_tokens,
    )
    result = _run_stage(
        conn, sku, model_adapter, request, purpose="comp_plan",
        budget=stage_budget, spent=_stage_spend(conn, sku, "comp_plan"),
    )
    plan = parse_comp_plan_tool_input(
        result.tool_input,
        valid_evidence_ids={row["id"] for row in observations},
        already_searched=set(_prior_lookups(conn, sku)),
    )
    outcome.plan = plan
    outcome.notes.extend(f"plan: {note}" for note in plan.malformed)

    if not plan.usable:
        outcome.stopped = "plan_unusable"
        outcome.stop_reason = (
            "the planner's arguments could not be read. Nothing was recorded and no "
            "lookup was spent; re-run to try again."
        )
        return outcome
    if plan.sufficient or not plan.lookups:
        outcome.stopped = "sufficient"
        outcome.stop_reason = plan.rationale or "the planner proposed no searches"
        return outcome

    spent = LookupSpend(lookups=performed_count, cost_micros=0)
    allocation = check_lookup_plan(lookup_budget, spent, len(plan.lookups), lookup_rates)
    outcome.deferral_reason = allocation.reason
    outcome.deferred = [plan.lookups[i].query for i in allocation.deferred]
    if allocation.trimmed and not dry_run:
        log_event(
            conn, "comp_research.lookups_deferred",
            {"reason": allocation.reason, "deferred": outcome.deferred}, item_id=sku,
        )

    if dry_run:
        outcome.performed = [l.query for l in plan.lookups[: allocation.allowed]]
        return outcome

    # --- retrieve and extract --------------------------------------------------
    recorded: list[CompObservation] = []
    planned_total = len(plan.lookups[: allocation.allowed])
    for index, planned in enumerate(plan.lookups[: allocation.allowed], start=1):
        progress.report(
            progress.Phase.SEARCHING,
            f"search {index}/{planned_total}: {planned.query[:60]}",
        )
        try:
            documents = research_adapter.search(
                ResearchQuery(planned.query, "marketplace", planned.motivation)
            )
        except ResearchError as exc:
            outcome.notes.append(f"lookup failed ({planned.query}): {exc}")
            _drain_adapter_notes(research_adapter, outcome)
            continue
        except Exception as exc:  # noqa: BLE001 - one bad page must not lose the round
            # Everything already retrieved is still worth recording. Losing a
            # round's work to the last query in it is the failure this prevents.
            outcome.notes.append(
                f"lookup failed ({planned.query}): {type(exc).__name__}: {exc}"
            )
            _drain_adapter_notes(research_adapter, outcome)
            continue

        for position, document in enumerate(documents, start=1):
            progress.report(
                progress.Phase.READING,
                f"reading listing {position}/{len(documents)} from "
                f"{document.marketplace}",
            )
            try:
                found = _comps_from_document(
                    conn, sku, document, planned, model_adapter, stage_budget, outcome
                )
            except BudgetExceeded as exc:
                # The extraction budget is gone. Everything already retrieved is
                # still evidence, and it was already paid for -- this used to
                # propagate out of the round and discard all of it, which is how
                # three rounds of real Canon listings became zero comps. The stage
                # ends here and the round finishes with what it has.
                outcome.notes.append(f"extraction stopped: {exc}")
                outcome.stopped_early = True
                break
            recorded.extend(found)

        # Observations the adapter read straight from the search index, for hosts
        # we are not permitted to fetch. No page was loaded and no extraction call
        # was made, so they cost nothing here and arrive already shaped -- asking,
        # condition unstated. Adapters without this method simply have none.
        direct = getattr(research_adapter, "take_direct_comps", None)
        from_index = list(direct()) if direct else []
        if from_index:
            recorded.extend(from_index)
            outcome.notes.append(
                f"{len(from_index)} asking price(s) taken from the search index for "
                f"{planned.query!r}: condition unstated, never sold prices"
            )

        # Pages the adapter could not read -- timed out, refused, empty -- with the
        # URL. Without this they were appended to the adapter's own `notes` and
        # never looked at again, so a host that reliably stalls was invisible.
        _drain_adapter_notes(research_adapter, outcome)

        gateway.record_lookup(
            sku, provider=research_adapter.provider, query=planned.query,
            motivation=planned.motivation, evidence_ids=list(planned.evidence_ids),
            result_count=len(documents) + len(from_index), scope="pricing",
            cost_micros=research_adapter.cost_micros_per_lookup(),
        )
        outcome.performed.append(planned.query)
        if outcome.stopped_early:
            # No further query can extract anything, so searching again would
            # spend lookups to produce pages nothing can read.
            outcome.notes.append(
                "stopped searching: the extraction budget for this item is spent"
            )
            break

    outcome.listings_found = len(recorded)
    if not recorded:
        outcome.stopped = "searched_not_found"
        outcome.stop_reason = (
            f"{len(outcome.performed)} search(es) returned no usable listing"
        )
        return outcome

    stored = []
    for obs in recorded:
        try:
            sp.record_comp_observation(conn, obs)
        except sqlite3.IntegrityError:
            # Same marketplace, same listing id, same instant: this *is* the row
            # already there, not a second sighting. The table is right to refuse
            # the insert -- but the comp is still a comp for this item, so it
            # stays in the round.
            #
            # Dropping it was worse than the crash it replaced. A listing found in
            # an earlier round could never be claimed in a later one: MP-000022
            # re-found the two real Bowflex pairs at $250 and $499.99, skipped
            # both as duplicates, judged only the parts listings that happened to
            # be new, and priced a pair of dumbbells at $50.
            outcome.notes.append(f"already recorded: {obs.comp_id}")
            stored.append(obs)
            outcome.kinds[str(obs.price_kind)] = (
                outcome.kinds.get(str(obs.price_kind), 0) + 1
            )
            continue
        stored.append(obs)
        outcome.kinds[str(obs.price_kind)] = (
            outcome.kinds.get(str(obs.price_kind), 0) + 1
        )
    recorded = stored
    outcome.comps_recorded = len(recorded)

    # --- judge -----------------------------------------------------------------
    #
    # Only comps their source's licence permits into a prompt. A source recorded
    # as `derived_only` may still price the item -- `estimate.py` is arithmetic and
    # never sees a prompt -- but its rows must not appear in a model's context, and
    # the judge is the one stage in this loop that would put them there.
    #
    # Withheld comps are not silently dropped: they are reported, and they are
    # judged by whoever can legitimately look at them, which is the operator via
    # `price claim` or the deterministic matcher an official adapter supplies.
    promptable = [
        obs for obs in recorded
        if obs.model_visibility is ModelVisibility.FULL
    ]
    outcome.withheld_from_model = [
        obs.comp_id for obs in recorded if obs.model_visibility is not ModelVisibility.FULL
    ]
    if outcome.withheld_from_model:
        outcome.notes.append(
            f"{len(outcome.withheld_from_model)} comp(s) withheld from the judging "
            f"prompt by their source's licence; offered for your judgement instead"
        )
    withheld = [
        obs for obs in recorded if obs.model_visibility is not ModelVisibility.FULL
    ]
    if not promptable:
        offered = _offer_withheld(conn, sku, withheld, ceiling, outcome, propose_only)
        outcome.stopped = "nothing_promptable"
        outcome.stop_reason = (
            f"{len(recorded)} comp(s) recorded, none of which their licence allows "
            f"into a model prompt. "
            + (f"{offered} offered for your judgement."
               if offered else
               "They are stored and priceable; comparability is yours to record "
               "with `price claim`.")
        )
        return outcome

    progress.report(
        progress.Phase.JUDGING,
        f"weighing {len(promptable)} listing(s) against this item",
    )
    judge_request = comp_judging_stage(
        identification=render_identification(conn, sku),
        observations=render_observations(observations),
        comps=render_retrieved(promptable),
        identity_ceiling=str(ceiling),
        max_output_tokens=stage_budget.max_output_tokens,
    )
    judge_result = _run_stage(
        conn, sku, model_adapter, judge_request, purpose="comp_judge",
        budget=stage_budget, spent=_stage_spend(conn, sku, "comp_judge"),
    )
    judged = parse_comp_judge_tool_input(
        judge_result.tool_input,
        valid_item_evidence={row["id"] for row in observations},
        valid_comp_ids={obs.comp_id for obs in promptable},
    )
    outcome.notes.extend(f"judge: {note}" for note in judged.malformed)

    for judgement in judged.judgements:
        if propose_only:
            # Excluded judgements are still decided by the agent: ruling out a
            # bundle or a parts unit is not a question worth putting to a person.
            if judgement.comparability == "excluded":
                pass
            else:
                sp.record_comp_candidate(
                    conn, sku=sku, comp_id=judgement.comp_id,
                    proposed_comparability=judgement.comparability,
                    item_citations=tuple(str(i) for i in judgement.item_evidence_ids),
                    comp_citations=judgement.comp_fields,
                    rationale=judgement.rationale,
                )
                outcome.candidates_offered += 1
                outcome.ladder[judgement.comparability] = (
                    outcome.ladder.get(judgement.comparability, 0) + 1
                )
                continue

        claim = CompClaim(
            claim_id=_uid("claim"),
            sku=sku,
            comp_id=judgement.comp_id,
            comparability=Comparability(judgement.comparability),
            # Evidence ids as strings: `comp_claim` stores citation labels rather
            # than a foreign key, and an integer id renders as one either way.
            item_citations=tuple(str(i) for i in judgement.item_evidence_ids),
            comp_citations=judgement.comp_fields,
            rationale=judgement.rationale,
            excluded_reason=judgement.excluded_reason,
        )
        try:
            sp.record_comp_claim(conn, claim, identity_resolution=resolution)
        except ValueError as exc:
            # The ladder ceiling, refused where it is enforced. Recorded rather
            # than retried at a lower rung: silently demoting a claim would make
            # the judgement look considered when it was salvaged.
            outcome.refused.append(f"{judgement.comp_id}: {exc}")
            continue
        outcome.claims_recorded += 1
        outcome.ladder[judgement.comparability] = (
            outcome.ladder.get(judgement.comparability, 0) + 1
        )

    _offer_withheld(conn, sku, withheld, ceiling, outcome, propose_only)

    log_event(
        conn, "comp_research.round_complete",
        {"lookups": outcome.performed, "comps": outcome.comps_recorded,
         "claims": outcome.claims_recorded,
         "candidates": outcome.candidates_offered, "ladder": outcome.ladder,
         "kinds": outcome.kinds, "refused": len(outcome.refused)},
        item_id=sku,
    )
    return outcome


def _offer_withheld(conn, sku, withheld, ceiling, outcome, propose_only: bool) -> int:
    """Put licence-withheld comps in front of the operator instead of nowhere.

    `derived_only` means the rows must not enter a model prompt. It was never
    meant to mean invisible -- the note beside the judge says as much: such comps
    "are judged by whoever can legitimately look at them, which is the operator".
    Nothing ever showed them, so they were recorded, priced at nothing and lost.

    MP-000013 is the case. Its one genuinely comparable listing -- a $399.99 pair
    of the right dumbbells -- came from an unregistered shop, was withheld, and
    never became a candidate. What did reach the judge was eBay's replacement
    weight plates, which the judge correctly excluded. The item then had no comps
    at all, from a round that had found the right one.

    The agent proposes no comparability here because it has not been allowed to
    look. It offers the listing at the identity ceiling and says why it is
    unassessed, and the operator -- who may read that page perfectly legitimately
    -- decides. The licence rule is untouched: these rows still never go in a
    prompt.
    """
    if not (withheld and propose_only):
        return 0
    offered = 0
    for obs in withheld:
        sp.record_comp_candidate(
            conn, sku=sku, comp_id=obs.comp_id,
            proposed_comparability=str(ceiling),
            comp_citations=("title", "price"),
            rationale=(
                f"not assessed by the agent: {obs.marketplace} is not a registered "
                f"source, so its listings may not enter a model prompt. Offered at "
                f"the identity ceiling for you to judge."
            ),
        )
        offered += 1
    outcome.candidates_offered += offered
    outcome.notes.append(
        f"{offered} comp(s) offered for your judgement instead of the agent's, "
        f"because their source's licence keeps them out of a prompt"
    )
    return offered


def _identity_resolution(conn: sqlite3.Connection, sku: str) -> str:
    """The stored resolution, which caps the ladder. Never inferred here."""
    from resell.reasoning.research_loop import identity_resolution

    return str(identity_resolution(conn, sku))


def _comps_from_document(
    conn, sku, document, planned, model_adapter, stage_budget, outcome
) -> list[CompObservation]:
    """Extract listings from one page and turn them into comp observations.

    The extraction is a model call; everything it produces then passes through
    `comp_reading`, which decides what a price *is*. A `sold` reading the quoted
    text does not support becomes `asking` and the downgrade is reported -- that is
    the single most consequential correction this loop makes.
    """
    from resell.reasoning.stages import comp_extraction_stage, page_body_for_extraction
    from resell.reasoning.tools import parse_comp_extract_tool_input

    page_text = getattr(document, "page_text", "") or ""
    if not page_text.strip():
        outcome.notes.append(f"{document.url}: no readable text")
        return []

    request = comp_extraction_stage(
        page_text=page_text, url=document.url, query=planned.query,
        seeking=planned.seeking, max_output_tokens=stage_budget.max_output_tokens,
    )
    result = _run_stage(
        conn, sku, model_adapter, request, purpose="comp_extract",
        budget=stage_budget, spent=_stage_spend(conn, sku, "comp_extract"),
    )
    extracted = parse_comp_extract_tool_input(
        result.tool_input, page_text=page_body_for_extraction(page_text)
    )
    outcome.notes.extend(f"extract: {note}" for note in extracted.malformed)

    ceiling = ceiling_for_identity(_identity_resolution(conn, sku))
    exact = ceiling is Comparability.SAME_PRODUCT
    now = datetime.now(UTC)

    comps: list[CompObservation] = []
    for listing in extracted.listings:
        kind, why = read_price_kind(
            listing.price_state, listing.excerpt, bool(listing.sale_date)
        )
        if listing.price_state == "sold" and str(kind) != "realized":
            outcome.downgraded.append(f"{listing.title[:50]}: {why}")
        # A price on the brand's own site is what the thing costs new. Left as an
        # asking comp it competes with second-hand listings in the same sample.
        kind, retail_kind, retail_why = retail_from_source(
            kind, document.marketplace, _brand_of(conn, sku), document.authority,
        )
        if retail_kind is not None:
            outcome.notes.append(f"retail context: {retail_why}")

        band, band_why = band_for_declared_condition(listing.condition_text)
        if listing.condition_text and str(band) == "unknown":
            outcome.notes.append(f"condition: {band_why}")

        comps.append(CompObservation(
            comp_id=_uid("comp"),
            marketplace=document.marketplace,
            external_id=listing.external_id or _fingerprint(document.url, listing),
            price_kind=kind,
            basis=basis_for(kind, exact),
            retail_kind=retail_kind,
            price_cents=listing.price_cents,
            observed_at=now,
            condition_band=band,
            condition_declared_raw=listing.condition_text or None,
            condition_source=ConditionSource.SELLER_DECLARED,
            shipping_cents=listing.shipping_cents,
            url=listing.url or document.url,
            title=listing.title,
            source_authority=str(document.authority),
            retrieval_method=RetrievalMethod.AUTOMATED_FETCH,
            adapter=document.adapter,
            query_text=planned.query,
            raw_payload_hash=hashlib.sha256(listing.excerpt.encode()).hexdigest()[:32],
            source_excerpt=listing.excerpt,
            # From the source's recorded policy, not from the caller. An
            # unregistered source resolves to derived_only, which is the
            # conservative direction and costs only the model's view of the rows.
            model_visibility=sp.visibility_for_source(conn, document.marketplace),
        ))
    return comps


def _fingerprint(url: str, listing) -> str:
    """A stable id for a listing the page never gave one.

    `comp_observation` is unique on (marketplace, external_id, observed_at), so a
    missing id would otherwise collide across listings on the same page. Derived
    from the URL, title and price, which is what distinguishes them.
    """
    seed = f"{url}|{listing.title}|{listing.price_cents}"
    return "anon-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def _brand_of(conn, sku: str) -> str | None:
    """The item's brand, from the column or the aspect that holds it.

    Both are consulted because they disagree: the column is filled by the mapping
    stage's carry-forward and the aspect by the mapper itself, and an item mapped
    before that carry-forward existed has one and not the other.
    """
    row = conn.execute(
        "SELECT brand, aspects FROM identification "
        "WHERE sku = ? AND superseded_at IS NULL", (sku,),
    ).fetchone()
    if row is None:
        return None
    if row["brand"]:
        return str(row["brand"])
    try:
        values = (json.loads(row["aspects"] or "{}") or {}).get("Brand") or []
    except (TypeError, ValueError):
        return None
    return str(values[0]) if values else None


def _drain_adapter_notes(adapter, outcome) -> None:
    """Move the adapter's per-URL notes onto the round's outcome, once each.

    Drained rather than copied so a note is reported against the query that
    produced it, and so the same unreadable page is not listed again on every
    subsequent lookup.
    """
    notes = getattr(adapter, "notes", None)
    if not notes:
        return
    outcome.notes.extend(f"page: {note}" for note in notes)
    notes.clear()
