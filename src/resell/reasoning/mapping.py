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
    render_donated_facts,
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


def load_operator_candidates(
    conn: sqlite3.Connection, sku: str
) -> dict[str, list[Candidate]]:
    """Stored candidates whose support is an operator statement.

    Spans identification versions on purpose. An operator answering "40R" was
    describing the object, not the model's current guess about it, so the answer
    survives re-identification. Bases come from the evidence row, never from
    anything a caller asserted.

    Assumes `identification.sku`; if that column is named otherwise, this join is
    the only thing to change.
    """
    rows = conn.execute(
        """
        SELECT ac.aspect_name AS aspect_name, ac.value AS value,
               e.id AS evidence_id, e.basis AS basis
        FROM aspect_candidate ac
        JOIN identification i ON i.id = ac.identification_id
        JOIN aspect_candidate_evidence ace ON ace.candidate_id = ac.id
        JOIN evidence e ON e.id = ace.evidence_id
        WHERE i.sku = ? AND e.basis = ?
        """,
        (sku, str(Basis.OPERATOR)),
    ).fetchall()

    support: dict[tuple[str, str], list[EvidenceRef]] = {}
    for row in rows:
        key = (row["aspect_name"], row["value"])
        support.setdefault(key, []).append(
            EvidenceRef(row["evidence_id"], Basis(row["basis"]))
        )

    out: dict[str, list[Candidate]] = {}
    for (aspect_name, value), refs in support.items():
        out.setdefault(aspect_name, []).append(
            Candidate(value=value, support=tuple(refs))
        )
    return out


def _merge_operator_candidates(
    conn: sqlite3.Connection,
    sku: str,
    resolved: dict[str, list[Candidate]],
    known_aspects: set[str],
) -> dict[str, list[Candidate]]:
    """Fold operator statements into the model's candidates before resolution.

    Same value as one the model proposed: the support is unioned, so that
    candidate gains an adjudicating basis and wins rather than competing with
    itself. Different value: it joins as a competing candidate and wins on
    adjudication. Two operator statements naming different values: both are
    adjudicating, `analyse` finds more than one, and it falls through to
    contradicted -- which is correct, because the operator has contradicted
    themselves and inventing a winner would hide that.

    Aspects outside the category's form are dropped; `analyse` is given a
    cardinality map keyed on the form, and an aspect missing from it has no
    defined behaviour.
    """
    stored = load_operator_candidates(conn, sku)
    if not stored:
        return resolved

    merged = {name: list(candidates) for name, candidates in resolved.items()}
    for aspect_name, operator_candidates in stored.items():
        if aspect_name not in known_aspects:
            continue
        existing = merged.setdefault(aspect_name, [])
        for candidate in operator_candidates:
            match = next(
                (c for c in existing if c.value == candidate.value), None
            )
            if match is None:
                existing.append(candidate)
                continue
            union = {(ref.evidence_id, ref.basis): ref for ref in match.support}
            union.update(
                {(ref.evidence_id, ref.basis): ref for ref in candidate.support}
            )
            existing[existing.index(match)] = Candidate(
                value=match.value, support=tuple(union.values())
            )
    return merged


def _donated_text(conn: sqlite3.Connection, citable: dict[int, str]) -> dict[int, str]:
    """Payload text for the donated facts an aspect is permitted to cite.

    `citable_candidate_evidence` returns ids mapped to a donation scope, not to
    text, so the text is fetched here. Kept to the permitted ids alone: a citation
    outside them is already dropped as an invented one, and reading it back would
    give the citation checks an opinion about evidence the gate has refused.
    """
    if not citable:
        return {}
    placeholders = ",".join("?" * len(citable))
    return {
        row["id"]: str(row["payload"])
        for row in conn.execute(
            f"SELECT id, payload FROM evidence WHERE id IN ({placeholders})",
            list(citable),
        )
    }


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

    from resell.gateway import citable_candidate_evidence

    citable = citable_candidate_evidence(conn, sku)
    request = mapping_stage(
        render_aspect_form(specs),
        render_observations(observations),
        max_output_tokens=budget.max_output_tokens,
        donated=render_donated_facts(conn, citable),
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
    proposal = parse_map_tool_input(
        result.tool_input, valid_evidence_ids=in_scope, citable_candidates=citable
    )

    # Before resolving, two checks on the citations themselves. Both must happen
    # here rather than at publish: by then a bad citation has been approved and
    # looks like a decision.
    from dataclasses import replace

    from resell.reasoning.gaps import (
        detect_uncited_value,
        detect_value_substitution,
        supported_generalisation,
    )

    allowed_by_aspect = {spec.name: spec.allowed_values for spec in specs}
    # Everything a candidate is allowed to cite, so a citation check can read what
    # was actually cited. Observations and donated facts both, because a candidate
    # may cite either and a half-populated map would make the second check fail
    # open on exactly the candidates that lean on external facts.
    text_by_id = {row["id"]: str(row["payload"]) for row in observations}
    text_by_id.update(_donated_text(conn, citable))

    filtered: dict[str, list] = {}
    for name, candidates in proposal.candidates_by_aspect.items():
        kept = []
        for candidate in candidates:
            cited_ids = tuple(ref.evidence_id for ref in candidate.support)
            cited_text = " ".join(
                text_by_id.get(evidence_id, "") for evidence_id in cited_ids
            )
            # Miscitation first, and the order is the finding rather than a
            # preference. Brand = "Beats by Dr. Dre" cited from a panel reading
            # "Apple Inc." trips both checks, and the substitution check's verdict
            # -- "the evidence names 'Apple', and where they differ the evidence
            # wins" -- points at the wrong fault entirely: the value was right and
            # the citation was wrong. "Some other observation names this, cite
            # that one" is the specific diagnosis, so it gets asked first.
            #
            # Only where the aspect has a single candidate, though. Two readings
            # off one hedged observation -- "the fabric could read as either shade"
            # -- is the model correctly declining to choose, and this check would
            # drop whichever reading some other observation happens to name,
            # converting an ambiguity into a confident answer. `analyse` already
            # handles competing candidates properly: it refuses to pick and asks
            # for a photo. Deciding here would be worse than not checking.
            miscited = (
                detect_uncited_value(name, candidate.value, cited_ids, text_by_id)
                if len(candidates) == 1 else None
            )
            if miscited:
                proposal.malformed.append(miscited)
                continue
            # The swap check does run against competing candidates, and must: the
            # pair ("Navy" cited from a tag reading NAVY, "Blue" cited from the
            # same tag) is a substitution rather than an ambiguity, and dropping
            # "Blue" is the right answer.
            swap = detect_value_substitution(
                name, candidate.value, cited_text, allowed_by_aspect.get(name, ())
            )
            if swap:
                # Before the refusal costs someone a question: is there a weaker
                # version of this same value that the evidence does support?
                #
                # MP-000041 proposed `100% Polyester` from a tag reading
                # "Polyester". Refusing the composition claim is right; asking a
                # person for a value already sitting in the citation is not, and
                # the person answered "recycled origin", which is worse than what
                # was refused. Narrowing is never a different value -- see
                # `supported_generalisation` -- so `Gray` still cannot become
                # `Black` this way.
                weaker = supported_generalisation(
                    candidate.value, cited_text, allowed_by_aspect.get(name, ())
                )
                if weaker:
                    proposal.malformed.append(
                        f"{name}: {candidate.value!r} narrowed to {weaker!r}, which "
                        f"is what the cited evidence supports"
                    )
                    kept.append(replace(candidate, value=weaker))
                    continue
                proposal.malformed.append(swap)
                continue
            kept.append(candidate)
        filtered[name] = kept

        # A value the evidence names under another word, that nobody proposed, is a
        # fibre or finish quietly dropped from the listing. Reported, not added.
        from resell.reasoning.gaps import missing_synonyms

        overlooked = missing_synonyms(
            " ".join(text_by_id.values()),
            allowed_by_aspect.get(name, ()),
            {c.value for c in kept},
        )
        for entry in overlooked:
            proposal.malformed.append(
                f"{name}: {entry} is an allowed value the evidence supports but "
                f"nothing proposed it"
            )

    resolved = {
        name: _rehydrate_basis(conn, candidates)
        for name, candidates in filtered.items()
    }
    required_names = [spec.name for spec in specs if spec.required]
    cardinality = {spec.name: spec.cardinality for spec in specs}
    # Operator statements are inputs to resolution, not audit records written
    # after it. Without this, answering a blocking question changed nothing.
    resolved = _merge_operator_candidates(conn, sku, resolved, set(cardinality))
    outcomes, gaps = analyse(
        required_names, resolved, cardinality, proposal.reasons_by_aspect
    )

    # Optional aspects are resolved too, but a gap on one does not block.
    for spec in specs:
        if spec.required or spec.name not in resolved:
            continue
        from resell.reasoning.gaps import resolve_aspect

        outcome = resolve_aspect(
            spec.name, resolved[spec.name], cardinality=spec.cardinality,
            unsupported_reason=proposal.reasons_by_aspect.get(spec.name),
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
