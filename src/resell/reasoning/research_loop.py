"""The identification research loop.

One round: plan, allocate budget, retrieve, judge, select. Two model calls at the
ends and deterministic machinery in between.

The rule this module exists to enforce: **match confidence is not donation
authority.** A model can produce a fluent, confident rationale for any pairing;
that is the cheapest thing it makes. What a candidate is permitted to contribute is
computed from three things it does not control -- whether the identifier carries a
check digit, where the document came from, and whether both sides of the claim cite
real evidence. The rationale is recorded for a human to read and feeds nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from resell.db import log_event
from resell.reasoning.adapters import AdapterError, ModelAdapter, get_adapter
from resell.reasoning.adapters.research import (
    ResearchAdapter,
    ResearchError,
    ResearchQuery,
    get_research_adapter,
)
from resell.reasoning.budget import (
    LookupBudget,
    LookupRates,
    LookupSpend,
    StageBudget,
    StageSpend,
    check,
    check_lookup_plan,
    estimate_cost,
)
from resell.reasoning.ledger import CallStatus, begin_call, finalize_call
from resell.reasoning.research import (
    MatchStrength,
    ResearchState,
    Selection,
    SourceAuthority,
    donation_scope,
    select_candidate,
    should_stop,
)
from resell.reasoning.stages import (
    matching_stage,
    planning_stage,
    render_observations,
)
from resell.reasoning.tools import (
    ResearchPlan,
    parse_match_tool_input,
    parse_plan_tool_input,
)


class ResearchLoopError(RuntimeError):
    pass


@dataclass
class ModeDecision:
    proposed: str
    accepted: str
    supported: bool
    reason: str


@dataclass
class RoundOutcome:
    plan: ResearchPlan | None = None
    mode: ModeDecision | None = None
    performed: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    deferral_reason: str = ""
    candidates_found: int = 0
    selection: Selection | None = None
    stopped: str | None = None
    stop_reason: str = ""
    notes: list[str] = field(default_factory=list)


# --- helpers -----------------------------------------------------------------


def _run_stage(conn, sku, adapter: ModelAdapter, request, *, purpose: str,
               budget: StageBudget, spent: StageSpend):
    """A ledgered, budgeted model call. Same shape as observation and mapping."""
    rates = adapter.rates()
    estimate = estimate_cost(adapter.estimate_input_tokens(request), budget, rates)
    check(budget, spent, estimate)

    call_id = begin_call(
        conn, sku, purpose=purpose, provider=adapter.provider, model=adapter.model,
        estimated_cost_micros=estimate.worst_case_micros, rate_basis=str(rates.basis),
        request_key=request.replay_key(),
    )
    try:
        result = adapter.run(request)
    except AdapterError as exc:
        finalize_call(conn, call_id, status=CallStatus.PROVIDER_ERROR, error=str(exc)[:2000])
        raise ResearchLoopError(str(exc)) from exc

    finalize_call(
        conn, call_id, status=CallStatus.COMPLETED,
        input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
        cost_micros=rates.cost_micros(result.usage.input_tokens, result.usage.output_tokens),
        latency_ms=result.latency_ms, response=result.raw_response,
        raw_usage=result.usage.raw,
    )
    return result, call_id


def _render_identifiers(rows) -> str:
    import json as _json

    lines = []
    for row in rows:
        if row["kind"] != "identifier_observation":
            continue
        payload = _json.loads(row["payload"])
        lines.append(
            f"[{row['id']}] {payload.get('scheme')}: {payload.get('normalized')} "
            f"({payload.get('check_explanation', '')[:60]})"
        )
    return "\n".join(lines)


def _render_candidates(conn, sku) -> str:
    import json as _json

    from resell.gateway import candidate_evidence

    grouped: dict[str, list] = {}
    for row in candidate_evidence(conn, sku):
        grouped.setdefault(row["candidate_ref"], []).append(row)

    lines = []
    for ref, rows in grouped.items():
        first = rows[0]
        provenance = (
            " (operator-transcribed; the system did not fetch this page)"
            if first["retrieval_method"] == "operator_transcribed" else ""
        )
        lines.append(
            f"Candidate {ref} — {first['source_url']} "
            f"[{first['source_authority']}]{provenance}"
        )
        for row in rows:
            payload = _json.loads(row["payload"])
            domain = row["fact_domain"] or "identity"
            lines.append(f"  [{row['id']}] ({domain}) {payload.get('claim', '')}")
    return "\n".join(lines)


def _prior_lookups(conn, sku, scope: str = "identity") -> list[str]:
    return [
        row["query"] for row in conn.execute(
            "SELECT query FROM research_lookup WHERE sku = ? AND scope = ?", (sku, scope)
        )
    ]


def _authority_by_candidate(conn, sku) -> dict[str, SourceAuthority]:
    """Authority is a property of where the document came from, read from storage."""
    mapping = {}
    for row in conn.execute(
        "SELECT DISTINCT candidate_ref, source_authority FROM evidence "
        "WHERE sku = ? AND subject = 'candidate_product'", (sku,)
    ):
        try:
            mapping[row["candidate_ref"]] = SourceAuthority(row["source_authority"])
        except (ValueError, TypeError):
            mapping[row["candidate_ref"]] = SourceAuthority.UNKNOWN
    return mapping


# --- the round ---------------------------------------------------------------


def run_round(
    conn: sqlite3.Connection,
    gateway,
    sku: str,
    *,
    unresolved: str = "",
    model_adapter: ModelAdapter | None = None,
    research_adapter: ResearchAdapter | None = None,
    provider: str | None = None,
    research_provider: str | None = None,
    stage_budget: StageBudget | None = None,
    lookup_budget: LookupBudget | None = None,
    lookup_rates: LookupRates | None = None,
    dry_run: bool = False,
) -> RoundOutcome:
    """One planning-and-matching round. Nothing is fetched before a validated plan."""
    from resell.gateway import candidate_evidence, observations_in_scope

    outcome = RoundOutcome()
    observations = observations_in_scope(conn, sku)
    if not observations:
        raise ResearchLoopError(f"{sku} has no observations; run: resell item observe {sku}")

    item = conn.execute(
        "SELECT identification_effort FROM item WHERE sku = ?", (sku,)
    ).fetchone()
    identification = conn.execute(
        "SELECT mode FROM identification WHERE sku = ? ORDER BY version DESC LIMIT 1", (sku,)
    ).fetchone()

    model_adapter = model_adapter or get_adapter(provider)
    research_adapter = research_adapter or get_research_adapter(research_provider)
    stage_budget = stage_budget or StageBudget.from_env("research")
    lookup_budget = lookup_budget or LookupBudget.from_env("identity")
    lookup_rates = lookup_rates or LookupRates.from_env(research_adapter.provider)

    # --- stop before planning, where the answer is already known -------------
    spend_row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(result_count), 0) c FROM research_lookup "
        "WHERE sku = ? AND scope = 'identity'", (sku,)
    ).fetchone()
    best = conn.execute(
        "SELECT strength FROM product_match WHERE sku = ? AND is_match = 1", (sku,)
    ).fetchall()
    best_strength = None
    if best:
        best_strength = max(
            (MatchStrength(row["strength"]) for row in best),
            key=lambda s: {"identifier_verified": 4, "identifier_asserted": 3,
                           "attribute_convergence": 2, "similarity": 1}[str(s)],
        )
    has_identifiers = any(row["kind"] == "identifier_observation" for row in observations)

    stop, reason, why = should_stop(
        ResearchState(
            lookups_performed=spend_row["n"],
            new_candidates_last_round=1,
            best_strength=best_strength,
            has_identifiers=has_identifiers,
            negative_finding_sufficient=False,
            budget_calls_remaining=lookup_budget.max_lookups - spend_row["n"],
        ),
        max_lookups=lookup_budget.max_lookups,
    )
    if stop:
        outcome.stopped, outcome.stop_reason = str(reason), why
        return outcome

    # --- R1: plan -------------------------------------------------------------
    request = planning_stage(
        observations=render_observations(observations),
        identifiers=_render_identifiers(observations),
        unresolved=unresolved,
        prior_lookups="\n".join(_prior_lookups(conn, sku)),
        current_mode=(identification["mode"] if identification else "unresolved"),
        effort=item["identification_effort"],
        max_output_tokens=stage_budget.max_output_tokens,
    )
    result, _ = _run_stage(
        conn, sku, model_adapter, request, purpose="research_plan",
        budget=stage_budget,
        spent=_stage_spend(conn, sku, "research_plan"),
    )
    plan = parse_plan_tool_input(
        result.tool_input,
        valid_evidence_ids={row["id"] for row in observations},
        already_searched=set(_prior_lookups(conn, sku)),
    )
    outcome.plan = plan
    # Notes from two different model calls used to arrive unlabelled, so an
    # "assessment missing" gave no clue which stage produced it.
    outcome.notes.extend(f"plan: {note}" for note in plan.malformed)

    # A response nothing could be read out of is not a decision about this item.
    # Recording it as one wrote "research not pursued" into evidence, which is the
    # audit trail for a deliberate choice not to search. Nothing reads those rows
    # back today, so the immediate cost is a log that misdescribes what happened --
    # but it is append-only, and `identity_resolution` derives the same fact from
    # `research_lookup` instead, so the two would simply disagree with each other
    # for the life of the item.
    #
    # Nothing is written, no mode is declared, and the call is already in the ledger
    # with its reasons, so re-running is the whole remedy.
    if not plan.usable:
        outcome.stopped = "plan_unusable"
        outcome.stop_reason = (
            "the planner's arguments could not be read, so there is no assessment "
            "and no lookups. Nothing was recorded and no lookup was spent; re-run "
            "to try again."
        )
        return outcome

    if plan.sufficient or not plan.lookups:
        outcome.stopped = "sufficient"
        outcome.stop_reason = plan.rationale or "the planner proposed no lookups"
        if not dry_run:
            gateway.record_research_negative(
                sku, summary=f"research not pursued: {outcome.stop_reason[:200]}",
                detail={"proposed_mode": plan.proposed_mode, "lookups_planned": 0},
            )
            outcome.mode = declare_mode(
                conn, gateway, sku, plan.proposed_mode, plan.rationale
            )
        return outcome

    # --- budget allocation, with the deferrals recorded ----------------------
    spent = LookupSpend(lookups=spend_row["n"], cost_micros=0)
    allocation = check_lookup_plan(lookup_budget, spent, len(plan.lookups), lookup_rates)
    outcome.deferral_reason = allocation.reason
    outcome.deferred = [plan.lookups[i].query for i in allocation.deferred]

    if allocation.trimmed and not dry_run:
        # Trimming silently would make the plan a fiction. What the agent judged
        # worth doing, and why it did not happen, both belong in the record.
        log_event(
            conn, "research.lookups_deferred",
            {"reason": allocation.reason,
             "deferred": [
                 {"query": plan.lookups[i].query,
                  "motivation": plan.lookups[i].motivation,
                  "cites": list(plan.lookups[i].evidence_ids)}
                 for i in allocation.deferred
             ]},
            item_id=sku,
        )

    if dry_run:
        outcome.performed = [l.query for l in plan.lookups[: allocation.allowed]]
        return outcome

    # --- R2: retrieve ---------------------------------------------------------
    for planned in plan.lookups[: allocation.allowed]:
        try:
            documents = research_adapter.search(
                ResearchQuery(planned.query, planned.source_kind, planned.motivation)
            )
        except ResearchError as exc:
            outcome.notes.append(f"lookup failed ({planned.query}): {exc}")
            continue

        recorded: list[int] = []
        for document in documents:
            recorded.extend(
                gateway.record_candidate_facts(
                    sku, candidate_ref=document.candidate_ref,
                    source_url=document.url, authority=str(document.authority),
                    facts=[
                        (fact.claim, str(fact.domain), fact.excerpt)
                        for fact in document.facts
                    ],
                    restriction=document.restriction, title=document.title,
                    retrieval_method=str(document.retrieval_method),
                )
            )
        gateway.record_lookup(
            sku, provider=research_adapter.provider, query=planned.query,
            motivation=planned.motivation, evidence_ids=list(planned.evidence_ids),
            result_count=len(documents),
        )
        outcome.performed.append(planned.query)
        outcome.candidates_found += len(documents)

    candidates = candidate_evidence(conn, sku)
    if not candidates:
        outcome.stopped = "searched_not_found"
        outcome.stop_reason = f"{len(outcome.performed)} lookup(s) returned no candidates"
        gateway.record_research_negative(
            sku, summary=outcome.stop_reason,
            detail={"queries": outcome.performed},
        )
        return outcome

    # --- R3: judge ------------------------------------------------------------
    match_request = matching_stage(
        observations=render_observations(observations),
        candidates=_render_candidates(conn, sku),
        max_output_tokens=stage_budget.max_output_tokens,
    )
    match_result, _ = _run_stage(
        conn, sku, model_adapter, match_request, purpose="research_match",
        budget=stage_budget, spent=_stage_spend(conn, sku, "research_match"),
    )
    proposal = parse_match_tool_input(
        match_result.tool_input,
        valid_item_evidence={row["id"] for row in observations},
        valid_candidate_evidence={row["id"] for row in candidates},
    )
    outcome.notes.extend(f"match: {note}" for note in proposal.malformed)

    if not proposal.claims:
        # Retrieval succeeded and judging produced nothing usable. Without saying
        # what came back, this is indistinguishable from finding no candidates --
        # and the lookups have already been paid for.
        shape = (
            sorted(match_result.tool_input)
            if isinstance(match_result.tool_input, dict)
            else type(match_result.tool_input).__name__
        )
        outcome.notes.append(
            f"match: no usable claims from {len(candidates)} candidate fact(s) across "
            f"{len({row['candidate_ref'] for row in candidates})} document(s); "
            f"tool input keys: {shape}"
        )

    # --- selection and donation, both deterministic ---------------------------
    authorities = _authority_by_candidate(conn, sku)
    selection = select_candidate(proposal.claims, authorities)
    outcome.selection = selection

    for claim in proposal.claims:
        authority = authorities.get(claim.candidate_ref, SourceAuthority.UNKNOWN)
        # Donation is computed from strength and authority. The claim's rationale --
        # however persuasive -- is stored and consulted by nobody.
        scope, _ = donation_scope(claim.strength, authority) if claim.is_match else (
            __import__("resell.reasoning.research", fromlist=["DonationScope"]).DonationScope.NONE,
            "",
        )
        gateway.record_product_match(
            sku, claim, authority=str(authority), donation_scope=str(scope),
        )

    if not selection.selected:
        gateway.record_research_negative(
            sku, summary=selection.reason,
            detail={"considered": selection.considered, "ruled_out": selection.ruled_out},
        )
    if plan.proposed_mode:
        outcome.mode = declare_mode(
            conn, gateway, sku, plan.proposed_mode, plan.rationale
        )
    return outcome


def identity_resolution(conn, sku: str):
    """Whether the identifiers were ever resolved, computed from the record."""
    from resell.reasoning.schema import IdentityResolution

    qualifying = conn.execute(
        "SELECT COUNT(*) FROM product_match WHERE sku = ? AND is_match = 1 "
        "AND donation_scope IS NOT NULL AND donation_scope != 'none' "
        "AND strength IN ('identifier_verified', 'identifier_asserted')",
        (sku,),
    ).fetchone()[0]
    if qualifying:
        return IdentityResolution.RESOLVED

    attempted = conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'identity'",
        (sku,),
    ).fetchone()[0]
    return (
        IdentityResolution.SEARCHED_NOT_FOUND if attempted
        else IdentityResolution.UNATTEMPTED
    )


def mode_evidence(conn, sku: str) -> dict:
    """Assemble what the mode gate needs, from stored facts only."""
    from resell.reasoning.schema import Basis, EvidenceRef, IdentificationEffort

    def cited(pattern: str) -> tuple:
        return tuple(
            EvidenceRef(row["id"], Basis(row["basis"] or "inference"))
            for row in conn.execute(
                "SELECT id, basis FROM evidence WHERE sku = ? AND subject = 'this_item' "
                "AND lower(payload) LIKE ?", (sku, pattern),
            )
        )

    finding = None
    stored = db_kv_negative(conn, sku)
    if stored:
        from resell.reasoning.schema import NegativeFinding

        finding = NegativeFinding(
            surfaces_examined=tuple(stored.get("surfaces_examined") or ()),
            photos_reviewed=int(stored.get("photos_reviewed", 0)),
            note=str(stored.get("note", "")),
        )

    # A product line, a manufacturer style code or an MPN all establish a family.
    line = cited("%line%") + cited("%style code%") + cited("%model%") + tuple(
        EvidenceRef(row["id"], Basis.TEXT_READ)
        for row in conn.execute(
            "SELECT id FROM evidence WHERE sku = ? AND kind = 'identifier_observation'",
            (sku,),
        )
    )
    return {
        "effort": IdentificationEffort(
            conn.execute(
                "SELECT identification_effort FROM item WHERE sku = ?", (sku,)
            ).fetchone()[0]
        ),
        "negative_finding": finding,
        "brand_support": cited("%brand%"),
        "line_support": line,
        "qualifying_match": bool(
            conn.execute(
                "SELECT COUNT(*) FROM product_match WHERE sku = ? AND is_match = 1 "
                "AND donation_scope IS NOT NULL AND donation_scope != 'none' "
                "AND strength IN ('identifier_verified', 'identifier_asserted')",
                (sku,),
            ).fetchone()[0]
        ),
    }


def declare_mode(conn, gateway, sku: str, proposed: str, rationale: str) -> ModeDecision:
    """Record the identification mode, if the evidence earns it.

    An unsupported proposal is refused rather than downgraded to whatever would have
    passed -- silently accepting a lesser mode would make the declaration look
    considered when it was salvaged. The modes that ARE supported are named, so the
    refusal is actionable.
    """
    import json as _json

    from resell.gateway import current_identification
    from resell.reasoning.gaps import mode_is_supported, supported_modes
    from resell.reasoning.schema import IdentificationMode

    try:
        mode = IdentificationMode(proposed)
    except ValueError:
        return ModeDecision(proposed, str(IdentificationMode.UNRESOLVED), False,
                            f"unknown mode {proposed!r}")

    evidence = mode_evidence(conn, sku)
    supported, why = mode_is_supported(mode, **evidence)
    available = [str(m) for m in supported_modes(**evidence)]

    if not supported:
        why = (
            f"{why} Supported by the current evidence: "
            f"{', '.join(available) if available else 'nothing beyond unresolved'}."
        )

    accepted = mode if supported else IdentificationMode.UNRESOLVED
    resolution = identity_resolution(conn, sku)

    identification = current_identification(conn, sku)
    fields = {
        column: (identification[column] if identification else None)
        for column in ("brand", "model", "variant", "title", "description",
                       "condition_id", "category_id", "reasoning")
    }
    aspects = (
        _json.loads(identification["aspects"])
        if identification and identification["aspects"] else None
    )
    gateway.propose_identification(sku, aspects=aspects, **fields)
    conn.execute(
        "UPDATE identification SET mode = ?, mode_rationale = ?, identity_resolution = ? "
        "WHERE sku = ? AND superseded_at IS NULL",
        (str(accepted), f"{rationale[:1500]}\n\n[gate] {why}", str(resolution), sku),
    )
    log_event(
        conn, "identification.mode_declared",
        {"proposed": str(mode), "accepted": str(accepted), "supported": supported,
         "resolution": str(resolution), "supported_modes": available, "why": why},
        item_id=sku,
    )
    return ModeDecision(str(mode), str(accepted), supported, why)


def db_kv_negative(conn, sku) -> dict | None:
    """The identity-search summary recorded by the observation stage, if any."""
    import json as _json

    from resell.db import kv_get

    raw = kv_get(conn, f"identity_search:{sku}")
    if not raw:
        return None
    try:
        return _json.loads(raw)
    except ValueError:
        return None


def rejudge(
    conn: sqlite3.Connection,
    gateway,
    sku: str,
    *,
    model_adapter: ModelAdapter | None = None,
    provider: str | None = None,
    stage_budget: StageBudget | None = None,
) -> RoundOutcome:
    """Judge the candidates already retrieved, without spending a lookup.

    Retrieval and judging fail independently. When the matcher returns nothing
    usable the documents are still there and already paid for, and making the
    operator re-run the searches to try again would charge twice for one mistake.
    """
    from resell.gateway import candidate_evidence, observations_in_scope

    outcome = RoundOutcome()
    observations = observations_in_scope(conn, sku)
    candidates = candidate_evidence(conn, sku)
    if not candidates:
        outcome.stopped = "no_candidates"
        outcome.stop_reason = "nothing has been retrieved for this item yet"
        return outcome

    model_adapter = model_adapter or get_adapter(provider)
    stage_budget = stage_budget or StageBudget.from_env("research")

    request = matching_stage(
        observations=render_observations(observations),
        candidates=_render_candidates(conn, sku),
        max_output_tokens=stage_budget.max_output_tokens,
    )
    result, _ = _run_stage(
        conn, sku, model_adapter, request, purpose="research_match",
        budget=stage_budget, spent=_stage_spend(conn, sku, "research_match"),
    )
    proposal = parse_match_tool_input(
        result.tool_input,
        valid_item_evidence={row["id"] for row in observations},
        valid_candidate_evidence={row["id"] for row in candidates},
    )
    outcome.notes.extend(f"match: {note}" for note in proposal.malformed)
    outcome.candidates_found = len({row["candidate_ref"] for row in candidates})

    authorities = _authority_by_candidate(conn, sku)
    selection = select_candidate(proposal.claims, authorities)
    outcome.selection = selection
    for claim in proposal.claims:
        authority = authorities.get(claim.candidate_ref, SourceAuthority.UNKNOWN)
        from resell.reasoning.research import DonationScope

        scope = (
            donation_scope(claim.strength, authority)[0]
            if claim.is_match else DonationScope.NONE
        )
        gateway.record_product_match(
            sku, claim, authority=str(authority), donation_scope=str(scope)
        )
    if not selection.selected:
        gateway.record_research_negative(
            sku, summary=selection.reason,
            detail={"considered": selection.considered, "ruled_out": selection.ruled_out},
        )
    return outcome


def _stage_spend(conn, sku, purpose) -> StageSpend:
    from resell.reasoning.vision import spend_so_far

    return spend_so_far(conn, sku, purpose)
