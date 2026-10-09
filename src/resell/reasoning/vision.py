"""The observation stage, orchestrated. Provider-neutral.

Prepares images, builds the stage request, hands it to whichever adapter is
configured, and parses the returned tool input into typed proposals. Persists
nothing: the caller offers the proposals to the gateway, which validates them on
the same terms as operator input.

This module names no vendor. Everything provider-specific is behind the adapter.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from resell.db import now_iso
from resell.derivatives import for_model
from resell.reasoning.adapters import AdapterError, ModelAdapter, get_adapter
from resell.reasoning.ledger import (
    BILLABLE_STATUSES,
    CallStatus,
    begin_call,
    completion_status,
    finalize_call,
)
from resell.reasoning.budget import (
    CostEstimate,
    ModelRates,
    StageBudget,
    StageSpend,
    check,
    estimate_cost,
)
from resell.reasoning.stages import ImageRef, StageRequest, StageResult, observation_stage
from resell.reasoning.tools import ObservationProposal, parse_observe_tool_input


class VisionError(RuntimeError):
    pass


@dataclass
class VisionResult:
    proposal: ObservationProposal
    request: StageRequest
    result: StageResult
    estimate: CostEstimate | None = None
    rates: ModelRates | None = None

    @property
    def actual_cost_micros(self) -> int | None:
        if self.rates is None:
            return None
        return self.rates.cost_micros(
            self.result.usage.input_tokens, self.result.usage.output_tokens
        )

    @property
    def provider(self) -> str:
        return self.result.provider

    @property
    def model(self) -> str:
        return self.result.model


def prepare_images(paths: list[Path], cache_dir: Path) -> tuple[ImageRef, ...]:
    """Convert and downscale each photo, keeping its position and content hash.

    Positions are the item's photo positions, so a citation of "photo 3" means the
    same thing to the model, the operator, and `item show`.
    """
    images = []
    for position, path in enumerate(paths, start=1):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        images.append(
            ImageRef(
                path=for_model(path, cache_dir, digest=digest),
                position=position,
                content_sha256=digest,
            )
        )
    return tuple(images)


def observe(
    photo_paths: list[Path],
    *,
    cache_dir: Path,
    adapter: ModelAdapter | None = None,
    provider: str | None = None,
    model: str | None = None,
    note: str | None = None,
    budget: StageBudget | None = None,
    spent: StageSpend | None = None,
    **adapter_kwargs,
) -> VisionResult:
    """Run the observation stage. Returns proposals; writes nothing.

    The budget is checked before the adapter is called, so a request that could
    exceed what remains is refused rather than attempted and regretted.
    """
    if not photo_paths:
        raise VisionError("no photos to observe")

    if adapter is None:
        kwargs = dict(adapter_kwargs)
        if model:
            kwargs["model"] = model
        adapter = get_adapter(provider, **kwargs)

    budget = budget or StageBudget.from_env("observe")
    # A partially implemented adapter should say so plainly rather than fail with an
    # AttributeError three frames deep. Both methods are required because the budget
    # guard is only as good as the estimate behind it.
    for required in ("run", "rates", "estimate_input_tokens"):
        if not callable(getattr(adapter, required, None)):
            raise VisionError(
                f"adapter {getattr(adapter, 'provider', type(adapter).__name__)!r} does "
                f"not implement {required}(); see reasoning/adapters/base.py"
            )
    rates = adapter.rates()
    images = prepare_images(photo_paths, cache_dir)
    request = observation_stage(images, note=note)
    request = replace(request, max_tokens=budget.max_output_tokens)

    estimate = estimate_cost(adapter.estimate_input_tokens(request), budget, rates)
    check(budget, spent or StageSpend(), estimate)

    try:
        result = adapter.run(request)
    except AdapterError as exc:
        raise VisionError(str(exc)) from exc

    return VisionResult(
        proposal=parse_observe_tool_input(result.tool_input),
        request=request,
        result=result,
        estimate=estimate,
        rates=rates,
    )


def spend_so_far(conn: sqlite3.Connection, sku: str, purpose: str = "observe") -> StageSpend:
    """What this item has already used on this stage.

    Counts every call the provider may have billed for, including ones that failed
    to parse and ones left `attempted` by a crash. Where the actual cost is unknown
    the pre-call estimate is charged instead -- not knowing whether we were billed
    is not a reason to assume we were not.
    """
    placeholders = ",".join("?" * len(BILLABLE_STATUSES))
    row = conn.execute(
        f"SELECT COUNT(*) AS calls, "
        f"COALESCE(SUM(COALESCE(cost_micros, estimated_cost_micros, 0)), 0) AS cost "
        f"FROM model_call WHERE sku = ? AND purpose = ? AND status IN ({placeholders})",
        (sku, purpose, *[str(s) for s in BILLABLE_STATUSES]),
    ).fetchone()
    return StageSpend(calls=row["calls"], cost_micros=row["cost"])


def observe_and_record(
    conn: sqlite3.Connection,
    sku: str,
    photo_paths: list[Path],
    *,
    cache_dir: Path,
    purpose: str = "observe",
    **kwargs,
) -> tuple[VisionResult, int]:
    """Observe with the call durably recorded around the provider request.

    The ledger row is written before the provider is contacted and finalised after,
    so a failure anywhere in between leaves an auditable, budget-counted record
    rather than a call that silently never happened.
    """
    adapter = kwargs.pop("adapter", None)
    provider = kwargs.pop("provider", None)
    model = kwargs.pop("model", None)
    if adapter is None:
        adapter_kwargs = {"model": model} if model else {}
        adapter = get_adapter(provider, **adapter_kwargs)

    budget = kwargs.pop("budget", None) or StageBudget.from_env(purpose)
    spent = kwargs.pop("spent", None) or spend_so_far(conn, sku, purpose)
    note = kwargs.pop("note", None)

    for required in ("run", "rates", "estimate_input_tokens"):
        if not callable(getattr(adapter, required, None)):
            raise VisionError(
                f"adapter {getattr(adapter, 'provider', type(adapter).__name__)!r} does "
                f"not implement {required}(); see reasoning/adapters/base.py"
            )

    rates = adapter.rates()
    images = prepare_images(photo_paths, cache_dir)
    request = replace(
        observation_stage(images, note=note), max_tokens=budget.max_output_tokens
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
        finalize_call(
            conn, call_id, status=CallStatus.PROVIDER_ERROR, error=str(exc)[:2000]
        )
        raise VisionError(str(exc)) from exc

    cost = rates.cost_micros(result.usage.input_tokens, result.usage.output_tokens)
    try:
        proposal = parse_observe_tool_input(result.tool_input)
        status = completion_status(
            bool(proposal.observations or proposal.identifiers)
            or not proposal.malformed
        )
        error = "; ".join(proposal.malformed)[:2000] if proposal.malformed else None
    except Exception as exc:  # noqa: BLE001 -- parsing must never lose a paid call
        finalize_call(
            conn, call_id, status=CallStatus.PARSE_FAILED,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens, cost_micros=cost,
            latency_ms=result.latency_ms, response=result.raw_response,
            raw_usage=result.usage.raw, error=f"{type(exc).__name__}: {exc}"[:2000],
        )
        raise VisionError(f"could not parse the response (call {call_id} recorded): {exc}") from exc

    finalize_call(
        conn, call_id, status=status,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens, cost_micros=cost,
        latency_ms=result.latency_ms, response=result.raw_response,
        raw_usage=result.usage.raw, error=error,
    )
    record_negative_finding(conn, sku, proposal)
    return VisionResult(
        proposal=proposal, request=request, result=result, estimate=estimate, rates=rates
    ), call_id


def record_negative_finding(conn: sqlite3.Connection, sku: str, proposal) -> bool:
    """Persist how hard the observation stage looked for an identity.

    Here rather than in a caller, because it was in a caller and only one of them
    had it. `cli_item` stored the finding; the orchestrator -- the path every
    phone-processed item takes -- parsed it, validated it and dropped it. The cost
    was invisible and total: `branded_generic` and `described_object` both *require*
    a cited negative finding, so for every item created through the web UI the two
    commonest honest outcomes were unreachable, and each one fell back to
    `unresolved`.

    The finding is a real one. MP-000056's names four surfaces across three photos
    and explains what the embossed text on the staple channel was. Nothing was
    missing except the write.
    """
    import json as _json

    from resell.db import kv_set

    finding = getattr(proposal, "negative_finding", None)
    if not finding:
        return False
    kv_set(conn, f"identity_search:{sku}", _json.dumps({
        "surfaces_examined": list(finding.surfaces_examined),
        "photos_reviewed": finding.photos_reviewed,
        "note": finding.note,
    }))
    return True


def record_trace(
    conn: sqlite3.Connection, sku: str, outcome: VisionResult, *, purpose: str = "observe"
) -> int:
    """Retain the call in full, with enough to replay it elsewhere.

    The stored request is the neutral replay key -- prompt and schema digests plus
    image content hashes -- not the provider's wire format. That is what makes
    running the same input against another provider a comparison rather than a
    reconstruction.

    Token counts are provider-reported; cost is derived from the configured rate
    table, so rate_basis records whether the figure rests on verified prices or on
    placeholders. The pre-call estimate is kept alongside the outcome so the guard
    can be checked against reality rather than trusted.
    """
    rates = outcome.rates
    cursor = conn.execute(
        "INSERT INTO model_call (sku, purpose, provider, model, input_tokens, "
        "output_tokens, cost_micros, latency_ms, called_at, request, response, "
        "raw_usage, estimated_cost_micros, rate_basis) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sku,
            purpose,
            outcome.result.provider,
            outcome.result.model,
            outcome.result.usage.input_tokens,
            outcome.result.usage.output_tokens,
            outcome.actual_cost_micros,
            outcome.result.latency_ms,
            now_iso(),
            json.dumps(outcome.request.replay_key()),
            json.dumps(outcome.result.raw_response),
            json.dumps(outcome.result.usage.raw),
            outcome.estimate.worst_case_micros if outcome.estimate else None,
            str(rates.basis) if rates else None,
        ),
    )
    return cursor.lastrowid
