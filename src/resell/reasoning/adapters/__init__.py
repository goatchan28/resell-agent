"""Adapter registry.

Only Anthropic is implemented. The registry exists so that adding another is a
new module plus one entry, and so nothing above this layer names a provider.
"""

from __future__ import annotations

import os

from resell.reasoning.adapters.anthropic import AnthropicAdapter
from resell.reasoning.adapters.base import AdapterError, ModelAdapter

ADAPTERS: dict[str, type] = {
    "anthropic": AnthropicAdapter,
    # "openai": OpenAIAdapter,
    # "gemini": GeminiAdapter,
    # "local": LocalAdapter,
}

DEFAULT_PROVIDER = os.environ.get("RESELL_VISION_PROVIDER", "anthropic")


def get_adapter(provider: str | None = None, **kwargs) -> ModelAdapter:
    name = (provider or DEFAULT_PROVIDER).lower()
    if name not in ADAPTERS:
        raise AdapterError(
            name,
            f"no adapter registered. Available: {', '.join(sorted(ADAPTERS))}",
        )
    return ADAPTERS[name](**kwargs)


__all__ = ["ADAPTERS", "AdapterError", "ModelAdapter", "get_adapter"]
