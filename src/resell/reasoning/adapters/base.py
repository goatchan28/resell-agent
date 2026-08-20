"""The model adapter boundary.

Everything vendor-specific lives behind this interface: image encoding, request
assembly, tool-call extraction, token accounting, and error mapping. Everything
above it -- stages, proposals, evidence, the gateway, the database -- is neutral.

The point is an experiment that stays cheap: running one item/evidence eval set
across several providers should be implementing `run` a few times, not unpicking
vendor structure from storage.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from resell.reasoning.budget import ModelRates
from resell.reasoning.stages import StageRequest, StageResult


class AdapterError(RuntimeError):
    """A provider call failed, or returned something unusable.

    Adapters raise this rather than leaking vendor exception types, so callers can
    handle failure without knowing who they were talking to.
    """

    def __init__(self, provider: str, message: str, *, status_code: int | None = None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"[{provider}] {message}")


@runtime_checkable
class ModelAdapter(Protocol):
    """One method. Deliberately narrow.

    A wider interface would tempt callers into provider-specific branching, which
    is exactly what this exists to prevent.
    """

    provider: str
    model: str

    def run(self, request: StageRequest) -> StageResult: ...

    def estimate_input_tokens(self, request: StageRequest) -> int:
        """Rough input token count, for the pre-call budget guard.

        Provider-specific by nature -- image tokenisation in particular differs
        markedly -- so it lives behind the adapter and the guard stays neutral.
        Over-estimating is the safe direction.
        """
        ...

    def rates(self) -> ModelRates: ...
