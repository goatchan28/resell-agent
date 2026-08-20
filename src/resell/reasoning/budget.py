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
        raw_input = os.environ.get("RESELL_RATE_INPUT_MICROS_PER_1K")
        raw_output = os.environ.get("RESELL_RATE_OUTPUT_MICROS_PER_1K")
        if raw_input and raw_output:
            return cls(
                input_micros_per_1k=int(raw_input),
                output_micros_per_1k=int(raw_output),
                basis=RateBasis.CONFIGURED,
                source=f"environment, for {provider}/{model}",
            )
        return cls()

    def cost_micros(self, input_tokens: int, output_tokens: int) -> int:
        return round(
            input_tokens * self.input_micros_per_1k / 1000
            + output_tokens * self.output_micros_per_1k / 1000
        )


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
