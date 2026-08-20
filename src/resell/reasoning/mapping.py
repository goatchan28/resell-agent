"""The aspect mapping stage: observations in, cited candidate sets out.

Provider-neutral, like the observation stage. Persists nothing itself -- the caller
offers the resolved outcomes to the gateway.

Two deterministic checks bracket the model:

  before  the aspect form comes from Taxonomy, not from the model
  after   every citation is verified against evidence actually in scope, and the
          candidate sets are resolved arithmetically into resolved / unsupported /
          ambiguous / contradicted

The model chooses values and explains why. It does not decide whether its answer is
good enough.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from resell.reasoning.adapters import AdapterError, ModelAdapter, get_adapter
from resell.reasoning.budget import (
    CostEstimate,
    ModelRates,
    StageBudget,
    StageSpend,
    check,
    estimate_cost,
)
from resell.reasoning.gaps import AspectOutcome, Candidate, Gap, analyse, gap_for
from resell.reasoning.ledger import CallStatus, begin_call, finalize_call
from resell.reasoning.schema import Basis, EvidenceRef
from resell.reasoning.stages import (
    StageRequest,
    StageResult,
    mapping_stage,
    render_aspect_form,
    render_observations,
)
from resell.reasoning.tools import MappingProposal, parse_map_tool_input


class MappingError(RuntimeError):
    pass


@dataclass
class MappingResult:
    proposal: MappingProposal
    outcomes: list[AspectOutcome]
    gaps: list[Gap]
    request: StageRequest
    result: StageResult
    estimate: CostEstimate
    rates: ModelRates
    call_id: int


def _rehydrate_basis(
    conn: sqlite3.Connection, candidates: list[Candidate]
) -> list[Candidate]:
    """Replace placeholder bases with the ones actually recorded.

    Resolution treats an operator statement as adjudicating, so the basis must come
    from storage rather than from whatever the model asserted -- otherwise a model
    could claim operator support for its own guess.
    """
    rehydrated = []
    for candidate in candidates:
        refs = []
        for ref in candidate.support:
            row = conn.execute(
                "SELECT basis FROM evidence WHERE id = ?", (ref.evidence_id,)
            ).fetchone()
            basis = Basis(row["basis"]) if row and row["basis"] else Basis.INFERENCE
            refs.append(EvidenceRef(ref.evidence_id, basis))
        rehydrated.append(Candidate(value=candidate.value, support=tuple(refs)))
    return rehydrated


def map_aspects(
    conn: sqlite3.Connection,
    sku: str,
    *,
    specs,
    observations: list[sqlite3.Row],
    adapter: ModelAdapter | None = None,
    provider: str | None = None,
    model: str | None = None,
    budget: StageBudget | None = None,
    spent: StageSpend | None = None,
    purpose: str = "map_aspects",
) -> MappingResult:
    """Run the mapping stage with the call durably recorded around the request."""
    if not observations:
        raise MappingError(
            f"{sku} has no observations in scope; run: resell item observe {sku}"
        )
    if not specs:
        raise MappingError("the aspect form is empty; check the category")

    if adapter is None:
        adapter = get_adapter(provider, **({"model": model} if model else {}))
    for required in ("run", "rates", "estimate_input_tokens"):
        if not callable(getattr(adapter, required, None)):
            raise MappingError(
                f"adapter {getattr(adapter, 'provider', type(adapter).__name__)!r} does "
                f"not implement {required}()"
            )

    budget = budget or StageBudget.from_env(purpose)
    spent = spent or StageSpend()
    rates = adapter.rates()

    request = mapping_stage(
        render_aspect_form(specs),
        render_observations(observations),
        max_output_tokens=budget.max_output_tokens,
    )
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
        raise MappingError(str(exc)) from exc

    cost = rates.cost_micros(result.usage.input_tokens, result.usage.output_tokens)
    in_scope = {row["id"] for row in observations}
    proposal = parse_map_tool_input(result.tool_input, valid_evidence_ids=in_scope)

    resolved = {
        name: _rehydrate_basis(conn, candidates)
        for name, candidates in proposal.candidates_by_aspect.items()
    }
    required_names = [spec.name for spec in specs if spec.required]
    cardinality = {spec.name: spec.cardinality for spec in specs}
    outcomes, gaps = analyse(required_names, resolved, cardinality)

    # Optional aspects are resolved too, but a gap on one does not block.
    for spec in specs:
        if spec.required or spec.name not in resolved:
            continue
        from resell.reasoning.gaps import resolve_aspect

        outcome = resolve_aspect(
            spec.name, resolved[spec.name], cardinality=spec.cardinality
        )
        outcomes.append(outcome)
        optional_gap = gap_for(outcome)
        if optional_gap:
            gaps.append(
                Gap(
                    optional_gap.aspect_name, optional_gap.resolution,
                    optional_gap.action, optional_gap.question, blocking=False,
                )
            )

    status = (
        CallStatus.PARSE_FAILED
        if not proposal.candidates_by_aspect and proposal.malformed
        else CallStatus.COMPLETED
    )
    finalize_call(
        conn, call_id, status=status,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens, cost_micros=cost,
        latency_ms=result.latency_ms, response=result.raw_response,
        raw_usage=result.usage.raw,
        error="; ".join(proposal.malformed)[:2000] if proposal.malformed else None,
    )

    return MappingResult(
        proposal=proposal, outcomes=outcomes, gaps=gaps, request=request,
        result=result, estimate=estimate, rates=rates, call_id=call_id,
    )
