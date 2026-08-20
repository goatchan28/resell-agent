"""The listing drafting stage.

Provider-neutral, ledgered and budgeted like every other stage. Persists nothing;
the caller offers the draft to the gateway after the deterministic review.

Two outputs from one call, kept apart on purpose: factual claims with citations, and
marketing copy without. The citations exist so a claim can be traced back to the
observation behind it months later; the buyer never sees them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from resell.reasoning.adapters import AdapterError, ModelAdapter, get_adapter
from resell.reasoning.budget import ModelRates, StageBudget, StageSpend, check, estimate_cost
from resell.reasoning.ledger import CallStatus, begin_call, finalize_call
from resell.reasoning.listing import DraftReview, ListingDraft, review_draft, support_kinds
from resell.reasoning.stages import StageRequest, StageResult, drafting_stage, render_observations
from resell.reasoning.tools import parse_draft_tool_input


class DraftingError(RuntimeError):
    pass


@dataclass
class DraftOutcome:
    draft: ListingDraft
    review: DraftReview
    request: StageRequest
    result: StageResult
    call_id: int
    unresolved: tuple[str, ...] = ()


def _render_aspects(aspects: dict[str, Any]) -> str:
    return "\n".join(f"- {name}: {' + '.join(map(str, values))}"
                     for name, values in sorted(aspects.items()))


def draft_listing(
    conn: sqlite3.Connection,
    sku: str,
    *,
    aspects: dict[str, Any],
    condition_id: str | None,
    unresolved: tuple[str, ...] = (),
    adapter: ModelAdapter | None = None,
    provider: str | None = None,
    model: str | None = None,
    budget: StageBudget | None = None,
    spent: StageSpend | None = None,
) -> DraftOutcome:
    """Write a listing from the record, then check it against the record."""
    from resell.gateway import observations_in_scope

    observations = observations_in_scope(conn, sku)
    if not observations:
        raise DraftingError(f"{sku} has no observations; run: resell item observe {sku}")

    if adapter is None:
        adapter = get_adapter(provider, **({"model": model} if model else {}))
    for required in ("run", "rates", "estimate_input_tokens"):
        if not callable(getattr(adapter, required, None)):
            raise DraftingError(
                f"adapter {getattr(adapter, 'provider', type(adapter).__name__)!r} does "
                f"not implement {required}()"
            )

    budget = budget or StageBudget.from_env("draft")
    spent = spent or StageSpend()
    rates = adapter.rates()

    observation_text = render_observations(observations)
    instruction_aspects = _render_aspects(aspects)
    if unresolved:
        # Naming the gaps is what stops them being filled. A model shown a form with
        # a blank Size will reach for one; a model told Size is unresolved and must
        # stay unstated has been given the correct behaviour explicitly.
        instruction_aspects += (
            f"\n\nThese aspects have NO value and must not be stated or implied "
            f"anywhere in the listing: {', '.join(unresolved)}."
        )

    request = drafting_stage(
        observations=observation_text,
        aspects=instruction_aspects,
        condition=condition_id or "",
        max_output_tokens=budget.max_output_tokens,
    )
    estimate = estimate_cost(adapter.estimate_input_tokens(request), budget, rates)
    check(budget, spent, estimate)

    call_id = begin_call(
        conn, sku, purpose="draft", provider=adapter.provider, model=adapter.model,
        estimated_cost_micros=estimate.worst_case_micros, rate_basis=str(rates.basis),
        request_key=request.replay_key(),
    )
    try:
        result = adapter.run(request)
    except AdapterError as exc:
        finalize_call(conn, call_id, status=CallStatus.PROVIDER_ERROR, error=str(exc)[:2000])
        raise DraftingError(str(exc)) from exc

    valid = {row["id"] for row in observations}
    draft = parse_draft_tool_input(result.tool_input, valid_evidence_ids=valid)

    supported_text = " ".join(
        [observation_text, " ".join(str(v) for values in aspects.values() for v in values)]
    )
    review = review_draft(
        draft,
        supported_text=supported_text,
        valid_evidence_ids=valid,
        available_support=support_kinds(
            condition_id=condition_id,
            aspect_names=set(aspects),
            evidence_kinds={row["kind"] for row in observations},
            observation_text=observation_text,
        ),
    )

    finalize_call(
        conn, call_id,
        status=CallStatus.PARSE_FAILED if draft.malformed and not draft.title
        else CallStatus.COMPLETED,
        input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
        cost_micros=rates.cost_micros(result.usage.input_tokens, result.usage.output_tokens),
        latency_ms=result.latency_ms, response=result.raw_response,
        raw_usage=result.usage.raw,
        error="; ".join(draft.malformed + review.problems)[:2000] or None,
    )
    return DraftOutcome(
        draft=draft, review=review, request=request, result=result,
        call_id=call_id, unresolved=unresolved,
    )


def store_draft(conn: sqlite3.Connection, gateway, sku: str, outcome: DraftOutcome) -> int:
    """Record the copy on the identification, with its citations kept separately.

    The claims are stored so a sentence can be traced back to the observation behind
    it later -- the point of the whole apparatus is that "why does it say that?" has
    an answer. They are not part of what a buyer reads.
    """
    from resell.cli_item import merged_identification
    from resell.gateway import current_identification

    fields, _ = merged_identification(
        conn, sku, title=outcome.draft.title, description=outcome.draft.description
    )
    gateway.propose_identification(sku, **fields)
    conn.execute(
        "UPDATE identification SET draft_claims = ? WHERE sku = ? AND superseded_at IS NULL",
        (
            json.dumps({
                "claims": [
                    {"text": claim.text, "evidence_ids": list(claim.evidence_ids)}
                    for claim in outcome.draft.claims
                ],
                "marketing_copy": outcome.draft.marketing_copy,
                "model": f"{outcome.result.provider}/{outcome.result.model}",
                "call_id": outcome.call_id,
                "warnings": outcome.review.warnings,
            }),
            sku,
        ),
    )
    return current_identification(conn, sku)["version"]
