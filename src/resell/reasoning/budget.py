"""Inference budget guard. Deliberately small.

Three limits per item per stage: how many calls, how many output tokens per call,
and how much estimated spend. A call whose worst case would exceed what remains is
refused before it is attempted, rather than discovered afterwards.

Not a billing system. There is no invoicing, no reconciliation, no cross-provider
rate negotiation. It exists so a loop that decides it needs "one more look" cannot
quietly run up a bill.

Costs are derived from a configured rate table, not reported by the provider --
providers report token usage, not money. The basis travels with every figure so a
number computed from placeholder rates can never be mistaken for an invoice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class RateBasis(StrEnum):
    """Where a price came from. Same discipline as the eBay fee basis."""

    PROVISIONAL_ESTIMATE = "provisional_estimate"  # placeholder; verify before trusting
    CONFIGURED = "configured"                      # operator supplied it
    PUBLISHED = "published"                        # the provider's own list price


class BudgetExceeded(RuntimeError):
    """Refused before the call was attempted."""


@dataclass(frozen=True)
class ModelRates:
    """Micros (millionths of a currency unit) per 1000 tokens.

    The defaults are placeholders chosen to be non-trivial rather than accurate:
    a budget guard that under-estimates is worse than useless. Set
    RESELL_RATE_INPUT_MICROS_PER_1K and RESELL_RATE_OUTPUT_MICROS_PER_1K from the
    provider's current price list to move the basis to `configured`.
    """

    input_micros_per_1k: int = 3000
    output_micros_per_1k: int = 15000
    basis: RateBasis = RateBasis.PROVISIONAL_ESTIMATE
    source: str = "placeholder defaults; not verified against any price list"

    @classmethod
    def from_env(cls, provider: str, model: str) -> ModelRates:
        """Rates for this model: configured, else published, else the placeholder.

        The order matters. An operator who set the environment variables has said
        something specific and it wins; otherwise a model we have a list price for
        is charged at that price; and only a model we know nothing about falls back
        to the placeholder, which is deliberately not cheap.

        Cost is computed and stored per call, so a rate that changes later does not
        rewrite what an item already cost. That is the reason this can be a plain
        table rather than a dated one.
        """
        raw_input = os.environ.get("RESELL_RATE_INPUT_MICROS_PER_1K")
        raw_output = os.environ.get("RESELL_RATE_OUTPUT_MICROS_PER_1K")
        if raw_input and raw_output:
            return cls(
                input_micros_per_1k=int(raw_input),
                output_micros_per_1k=int(raw_output),
                basis=RateBasis.CONFIGURED,
                source=f"environment, for {provider}/{model}",
            )
        published = PUBLISHED_RATES.get(model)
        if published is not None:
            return published
        return cls()

    def cost_micros(self, input_tokens: int, output_tokens: int) -> int:
        return round(
            input_tokens * self.input_micros_per_1k / 1000
            + output_tokens * self.output_micros_per_1k / 1000
        )


# Anthropic's published list prices, as micros per 1000 tokens -- so $3.00 per
# million input tokens is 3000 micros per 1000 tokens.
#
# Image tokens are not a separate line. The API counts them into
# `usage.input_tokens`, which is what the ledger records, so vision cost is
# already in the input figure rather than being missed or double-counted.
#
# Checked 2026-08-22. A stored cost is computed at call time and never revisited,
# so an out-of-date entry here misprices future calls only -- but it does misprice
# them silently, which is why the basis travels with every row.
PUBLISHED_RATES: dict[str, ModelRates] = {
    "claude-opus-5": ModelRates(
        input_micros_per_1k=5000, output_micros_per_1k=25000,
        basis=RateBasis.PUBLISHED,
        source="Anthropic list price, $5.00/$25.00 per 1M, checked 2026-08-22",
    ),
    "claude-sonnet-5": ModelRates(
        # Introductory pricing, $2.00/$10.00 per 1M, runs to 2026-08-31. Recorded
        # at the standard rate instead: over-stating a cost is the safe direction
        # for a budget guard, and the discount ends within days of this being set.
        input_micros_per_1k=3000, output_micros_per_1k=15000,
        basis=RateBasis.PUBLISHED,
        source="Anthropic list price, $3.00/$15.00 per 1M, checked 2026-08-22 "
               "(intro pricing of $2.00/$10.00 runs to 2026-08-31)",
    ),
    "claude-haiku-4-5": ModelRates(
        input_micros_per_1k=1000, output_micros_per_1k=5000,
        basis=RateBasis.PUBLISHED,
        source="Anthropic list price, $1.00/$5.00 per 1M, checked 2026-08-22",
    ),
}


@dataclass(frozen=True)
class StageBudget:
    """Limits for one stage on one item."""

    max_calls: int = 3
    max_output_tokens: int = 4000
    max_cost_micros: int = 250_000  # 0.25 currency units

    @classmethod
    def from_env(cls, stage: str) -> StageBudget:
        prefix = f"RESELL_BUDGET_{stage.upper()}"
        return cls(
            max_calls=int(os.environ.get(f"{prefix}_MAX_CALLS", cls.max_calls)),
            max_output_tokens=int(
                os.environ.get(f"{prefix}_MAX_OUTPUT_TOKENS", cls.max_output_tokens)
            ),
            max_cost_micros=int(
                os.environ.get(f"{prefix}_MAX_COST_MICROS", cls.max_cost_micros)
            ),
        )


@dataclass(frozen=True)
class StageSpend:
    """What this item has already used on this stage."""

    calls: int = 0
    cost_micros: int = 0


@dataclass(frozen=True)
class CostEstimate:
    estimated_input_tokens: int
    max_output_tokens: int
    worst_case_micros: int
    rates: ModelRates

    def describe(self) -> str:
        qualifier = (
            "estimated" if self.rates.basis is RateBasis.PROVISIONAL_ESTIMATE else "computed"
        )
        return (
            f"{qualifier} worst case {_money(self.worst_case_micros)} "
            f"({self.estimated_input_tokens} in + up to {self.max_output_tokens} out; "
            f"rate basis: {self.rates.basis})"
        )


def estimate_cost(
    estimated_input_tokens: int, budget: StageBudget, rates: ModelRates
) -> CostEstimate:
    """Worst case, not expected case.

    The output length is unknown until the call returns, so the guard assumes the
    cap. A guard that budgets for the average would let the expensive call through.
    """
    return CostEstimate(
        estimated_input_tokens=estimated_input_tokens,
        max_output_tokens=budget.max_output_tokens,
        worst_case_micros=rates.cost_micros(estimated_input_tokens, budget.max_output_tokens),
        rates=rates,
    )


def check(budget: StageBudget, spent: StageSpend, estimate: CostEstimate) -> None:
    """Refuse before attempting. Raises BudgetExceeded with the arithmetic shown."""
    if spent.calls >= budget.max_calls:
        raise BudgetExceeded(
            f"call limit reached: {spent.calls} of {budget.max_calls} already made "
            f"for this stage on this item"
        )

    remaining = budget.max_cost_micros - spent.cost_micros
    if estimate.worst_case_micros > remaining:
        raise BudgetExceeded(
            f"{estimate.describe()} exceeds the {_money(remaining)} remaining of a "
            f"{_money(budget.max_cost_micros)} budget "
            f"({_money(spent.cost_micros)} already spent across {spent.calls} call(s))"
        )


def _money(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"


# --- retrieval budget --------------------------------------------------------
#
# Research has two cost dimensions, and conflating them hides one of them. The
# planning and matching calls are inference and use StageBudget like any other
# stage. The lookups themselves are retrieval: separate provider, separate price,
# separate limit. An agent that plans cheaply and then fetches forty pages has
# stayed inside its inference budget and spent real money.


@dataclass(frozen=True)
class LookupRates:
    """Cost per retrieval, in micros. Same provenance discipline as token rates."""

    micros_per_lookup: int = 5000
    basis: RateBasis = RateBasis.PROVISIONAL_ESTIMATE
    source: str = "placeholder; set RESELL_RATE_LOOKUP_MICROS from your provider"

    @classmethod
    def from_env(cls, provider: str) -> LookupRates:
        raw = os.environ.get("RESELL_RATE_LOOKUP_MICROS")
        if raw:
            return cls(
                micros_per_lookup=int(raw),
                basis=RateBasis.CONFIGURED,
                source=f"environment, for {provider}",
            )
        return cls()


@dataclass(frozen=True)
class LookupBudget:
    """Retrieval limits for one research scope on one item.

    Scoped like identification effort is: identity research and pricing research get
    separate allowances, so a hard-to-identify item cannot quietly consume the comp
    budget before pricing has started.
    """

    scope: str = "identity"
    max_lookups: int = 6
    max_cost_micros: int = 60_000

    @classmethod
    def from_env(cls, scope: str = "identity") -> LookupBudget:
        prefix = f"RESELL_LOOKUP_{scope.upper()}"
        return cls(
            scope=scope,
            max_lookups=int(os.environ.get(f"{prefix}_MAX", cls.max_lookups)),
            max_cost_micros=int(
                os.environ.get(f"{prefix}_MAX_COST_MICROS", cls.max_cost_micros)
            ),
        )


@dataclass(frozen=True)
class LookupSpend:
    lookups: int = 0
    cost_micros: int = 0


@dataclass(frozen=True)
class LookupAllocation:
    """What the budget permits of a plan, and what it withheld.

    `deferred` exists because trimming a plan silently makes the plan a fiction. The
    agent proposed six lookups and two ran; the other four were a judgment about
    what would help, and discarding them without record loses both the judgment and
    the reason it was overruled. They are re-plannable later when budget allows.
    """

    allowed: int
    deferred: tuple[int, ...]          # indices into the original plan
    reason: str
    trimmed: bool = False


def check_lookup_plan(
    budget: LookupBudget, spent: LookupSpend, planned: int, rates: LookupRates
) -> LookupAllocation:
    """How many of the planned lookups may proceed, and which were withheld.

    Trims rather than refuses. A plan of six lookups with room for two should run the
    two most valuable, not be rejected wholesale -- the planner orders them, so the
    prefix is the useful part. What is withheld is recorded, not dropped.
    """
    remaining_calls = budget.max_lookups - spent.lookups
    if remaining_calls <= 0:
        return LookupAllocation(
            0, tuple(range(planned)),
            f"lookup limit reached: {spent.lookups} of {budget.max_lookups} performed "
            f"for {budget.scope} research on this item",
            trimmed=planned > 0,
        )

    remaining_micros = budget.max_cost_micros - spent.cost_micros
    affordable = remaining_micros // max(rates.micros_per_lookup, 1)
    allowed = max(0, min(planned, remaining_calls, affordable))

    if allowed == 0:
        return LookupAllocation(
            0, tuple(range(planned)),
            f"{_money(remaining_micros)} remaining will not cover a lookup at "
            f"{_money(rates.micros_per_lookup)} each (rate basis: {rates.basis})",
            trimmed=planned > 0,
        )
    if allowed < planned:
        return LookupAllocation(
            allowed, tuple(range(allowed, planned)),
            f"{planned} lookups planned, {allowed} affordable: "
            f"{remaining_calls} call(s) and {_money(remaining_micros)} remaining",
            trimmed=True,
        )
    return LookupAllocation(
        allowed, (), f"all {planned} planned lookups are within budget"
    )
