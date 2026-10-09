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
from dataclasses import dataclass, field
from typing import Any

from resell.reasoning.adapters import AdapterError, ModelAdapter, get_adapter
from resell.reasoning.budget import ModelRates, StageBudget, StageSpend, check, estimate_cost
from resell.reasoning.ledger import (
    CallStatus,
    begin_call,
    completion_status,
    finalize_call,
)
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


def _supported_text(observation_text: str, aspects: dict[str, Any]) -> str:
    """Everything the record says, as the reviewer gets to see it.

    Aspect *names* are part of the record and were being left out, so a draft
    mentioning the "California Prop 65 warning" -- an aspect the mapping stage had
    resolved, whose value is only the word WARNING -- was refused for stating a
    figure the record did not contain. It contained it, in the name.

    Both callers use this. They previously built the string separately and drifted,
    which is a fault the reviewer cannot catch: it only ever sees the result.
    """
    return " ".join([
        observation_text,
        " ".join(aspects),
        " ".join(str(v) for values in aspects.values() for v in values),
    ])


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
    refused_terms: tuple[str, ...] = (),
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
        refused_terms=tuple(refused_terms),
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

    review = review_draft(
        draft,
        supported_text=_supported_text(observation_text, aspects),
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
        status=completion_status(bool(draft.title) or not draft.malformed),
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


@dataclass
class RepairOutcome:
    """A repair attempt, and how much of the original it kept.

    `preserved_ratio` is measured rather than trusted. The instruction is to change
    only what was named, and a model that quietly rewrote everything would produce
    a draft that passes review while discarding accurate copy -- which is the
    outcome this whole path exists to avoid. Measuring it means the operator can
    see whether the repair was a repair.
    """

    outcome: DraftOutcome
    previous: ListingDraft
    preserved_ratio: float
    repaired: tuple[str, ...] = ()
    # Measures this attempt made worse than the draft it was repairing. A repair
    # that moves a counted violation in the wrong direction has not repaired
    # anything, whatever else it changed, and the next attempt is told so.
    regressed: dict = field(default_factory=dict)

    @property
    def looks_like_a_rewrite(self) -> bool:
        return self.preserved_ratio < 0.5

    @property
    def went_backwards(self) -> bool:
        return bool(self.regressed)

    def what_went_backwards(self) -> tuple[str, ...]:
        return tuple(
            f"the last repair made this worse: {name} went from {before.value} to "
            f"{now.value} {now.unit} against a limit of {now.limit}. Cut it this "
            f"time; do not solve another problem by adding length."
            for name, (before, now) in self.regressed.items()
        )


def _sentences(text: str) -> list[str]:
    import re

    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def preserved_ratio(previous: ListingDraft, repaired: ListingDraft) -> float:
    """How much of the previous description survived, sentence for sentence.

    Sentences rather than tokens: the unit the instruction talks about is a
    sentence, and a token measure would score a wholesale rewrite that happened to
    reuse the same vocabulary as faithful.
    """
    before = _sentences(previous.description)
    if not before:
        return 1.0
    after = set(_sentences(repaired.description))
    return sum(1 for s in before if s in after) / len(before)


def repair_draft(
    conn: sqlite3.Connection,
    sku: str,
    outcome: DraftOutcome,
    *,
    aspects: dict[str, Any],
    condition_id: str | None,
    also_tell_it: tuple[str, ...] = (),
    adapter: ModelAdapter | None = None,
    provider: str | None = None,
    model: str | None = None,
    budget: StageBudget | None = None,
    spent: StageSpend | None = None,
) -> RepairOutcome:
    """Fix exactly what the review named, and leave the rest alone.

    A refused draft is not worthless: usually one phrase asserts something the
    record does not support and the surrounding copy is accurate. Regenerating from
    scratch throws that away and tends to reintroduce the same error somewhere
    else.

    The reviewer runs again, unchanged, on the result. That is the part that must
    not soften: a repair that invents a new unsupported claim is refused exactly as
    the first draft was, and an unsupported claim still never reaches a stored
    draft. What changes is only how much good copy is discarded on the way.
    """
    from resell.gateway import observations_in_scope
    from resell.reasoning.stages import repair_stage

    if outcome.review.ok:
        raise DraftingError("nothing to repair; that draft passed review")

    observations = observations_in_scope(conn, sku)
    if adapter is None:
        adapter = get_adapter(provider, **({"model": model} if model else {}))

    budget = budget or StageBudget.from_env("draft")
    spent = spent or StageSpend()
    rates = adapter.rates()

    previous = outcome.draft
    claims = "\n".join(
        f"- {claim.text}  [cites {list(claim.evidence_ids) or 'nothing'}]"
        for claim in previous.claims
    )
    observation_text = render_observations(observations)
    request = repair_stage(
        previous_title=previous.title,
        previous_description=previous.description,
        previous_claims=claims,
        previous_marketing=previous.marketing_copy,
        problems="\n".join(
            f"- {p}" for p in list(outcome.review.problems) + list(also_tell_it)
        ),
        # The sums, done. A limit stated as "over eBay's 80" leaves the model to
        # subtract, and subtracting is exactly what it got wrong: told a title was
        # 89 characters it answered with 96.
        arithmetic="\n".join(f"- {a}" for a in outcome.review.arithmetic()),
        aspects=_render_aspects(aspects),
        observations=observation_text,
        condition=condition_id or "",
        max_output_tokens=budget.max_output_tokens,
    )
    estimate = estimate_cost(adapter.estimate_input_tokens(request), budget, rates)
    check(budget, spent, estimate)

    call_id = begin_call(
        conn, sku, purpose="draft_repair", provider=adapter.provider,
        model=adapter.model, estimated_cost_micros=estimate.worst_case_micros,
        rate_basis=str(rates.basis), request_key=request.replay_key(),
    )
    try:
        result = adapter.run(request)
    except AdapterError as exc:
        finalize_call(conn, call_id, status=CallStatus.PROVIDER_ERROR, error=str(exc)[:2000])
        raise DraftingError(str(exc)) from exc

    valid = {row["id"] for row in observations}
    draft = parse_draft_tool_input(result.tool_input, valid_evidence_ids=valid)
    review = review_draft(
        draft, supported_text=_supported_text(observation_text, aspects),
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
        status=completion_status(review.ok),
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_micros=rates.cost_micros(result.usage.input_tokens, result.usage.output_tokens),
        latency_ms=result.latency_ms, response=result.raw_response,
        raw_usage=result.usage.raw,
        error="; ".join(review.problems)[:2000] if review.problems else None,
    )

    repaired = DraftOutcome(
        draft=draft, review=review, request=request, result=result, call_id=call_id,
        unresolved=outcome.unresolved,
    )
    return RepairOutcome(
        outcome=repaired,
        previous=previous,
        preserved_ratio=preserved_ratio(previous, draft),
        repaired=tuple(outcome.review.problems),
        regressed=review.regressions(outcome.review),
    )
