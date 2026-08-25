"""A `ModelAdapter` for a model running on this Mac, for benchmarking only.

**Deliberately outside `src/resell/`.** The proposal put this next to the
Anthropic adapter; it lives here instead so it cannot be added to `ADAPTERS` by
accident and become live. Nothing in the package imports it, and it satisfies the
`ModelAdapter` Protocol structurally rather than by registration.

Talks to LM Studio's OpenAI-compatible server. The differences from Anthropic
that actually cost time, since the base module asks for them to be written down:

  - Tool arguments arrive as a JSON *string* in
    `choices[0].message.tool_calls[0].function.arguments`, not as a dict.
  - `tool_choice` takes only the strings none/auto/required -- not OpenAI's
    object form and not Anthropic's. One tool is declared, so "required" names it.
  - Token fields are `prompt_tokens` / `completion_tokens`.
  - `finish_reason` is `stop` / `length` / `tool_calls`, where Anthropic says
    `end_turn` / `max_tokens` / `tool_use`. Normalised here so the truncation
    guard upstream keeps working unchanged.
  - **Thinking cannot be turned off, and it spends the answer's budget.**
    `chat_template_kwargs.enable_thinking`, `reasoning_effort` and
    `reasoning.enabled` are all accepted and all ignored -- the model emits
    roughly the same reasoning either way. Those tokens come out of `max_tokens`,
    so a ceiling sized for the answer truncates before the answer starts. The
    first benchmark run lost 48 of 83 calls that way and measured nothing but
    this. `HEADROOM` adds room for it here, in the adapter, so `StageRequest`
    keeps meaning what it means everywhere else: the size of the answer.

Two ways to ask for structure, because local servers vary in which they honour:
`tools` + `tool_choice` (default), or `response_format: {type: "json_schema"}`.
Whichever is used, the adapter returns the same `StageResult`, so the caller
never learns which.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from resell.reasoning.adapters.base import AdapterError
from resell.reasoning.budget import ModelRates
from resell.reasoning.stages import StageRequest, StageResult, Usage

def headroom(answer_tokens: int) -> int:
    """A ceiling that fits the reasoning as well as the answer.

    Measured rather than guessed: on the calls that did fit, this model spent
    54--99% of a ceiling sized for the answer alone, most of it reasoning. Four
    times the answer plus a fixed floor covers every one of those with room, and
    the cap bounds the two largest judging batches, which would otherwise run for
    minutes on a machine that has a beta to get back to.
    """
    return min(4 * answer_tokens + 2000, 40_000)


PROVIDER = "lmstudio"
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "qwen3.6-35b-a3b-mlx"

# Anthropic's vocabulary, which the rest of the system already speaks. The
# truncation guard in `comp_loop` tests for "max_tokens" by name, so a local
# `length` has to arrive under that name or a cut-off answer would be read as a
# complete one -- the exact failure the guard exists to prevent.
FINISH_REASON = {
    "length": "max_tokens",
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
}


class LocalAdapter:
    """One method, same as every other adapter."""

    provider = PROVIDER

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        mode: str = "tools",
        timeout_s: float = 600.0,
        temperature: float = 0.0,
    ):
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._mode = mode
        self._timeout_s = timeout_s
        self._temperature = temperature

    # --- budget shims --------------------------------------------------------
    #
    # The benchmark does not meter a local model -- the marginal cost is zero and
    # that is the point of the exercise -- but `StageRequest` callers may consult
    # these, so they answer honestly rather than raising.

    CHARS_PER_TOKEN = 3.5

    def estimate_input_tokens(self, request: StageRequest) -> int:
        text = request.system_prompt + request.instruction
        return int(len(text) / self.CHARS_PER_TOKEN) + 512

    def rates(self) -> ModelRates:
        return ModelRates(input_micros_per_1k=0, output_micros_per_1k=0)

    # --- the call ------------------------------------------------------------

    def run(self, request: StageRequest) -> StageResult:
        if request.images:
            raise AdapterError(
                PROVIDER,
                f"{self.model} is a text model; {len(request.images)} image(s) "
                f"were supplied. Vision stages are out of scope for this benchmark.",
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.instruction},
            ],
            "max_tokens": headroom(request.max_tokens),
            "temperature": self._temperature,
        }
        if self._mode == "tools":
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": request.tool.name,
                    "description": request.tool.description,
                    "parameters": request.tool.json_schema,
                },
            }]
            # LM Studio rejects Anthropic's/OpenAI's object form outright:
            # "Invalid tool_choice type: 'object'. Supported string values: none,
            # auto, required". Only one tool is ever declared, so "required" forces
            # the same call by construction.
            payload["tool_choice"] = "required"
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.tool.name,
                    "strict": True,
                    "schema": request.tool.json_schema,
                },
            }

        started = time.monotonic()
        try:
            response = httpx.post(f"{self._base_url}/chat/completions",
                                  json=payload, timeout=self._timeout_s)
        except httpx.HTTPError as exc:
            raise AdapterError(PROVIDER, f"{type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise AdapterError(PROVIDER, response.text[:400],
                               status_code=response.status_code)
        body = response.json()
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish = FINISH_REASON.get(choice.get("finish_reason"),
                                   choice.get("finish_reason"))

        tool_input = self._arguments(message, request, finish)
        usage = body.get("usage") or {}
        return StageResult(
            tool_input=tool_input,
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                raw=usage,
            ),
            latency_ms=latency_ms,
            provider=PROVIDER,
            model=self.model,
            stop_reason=finish,
            raw_response=body,
        )

    def _arguments(self, message: dict, request: StageRequest, finish) -> dict:
        """The tool arguments, however this server chose to express them."""
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != request.tool.name:
                continue
            raw = function.get("arguments")
            if isinstance(raw, dict):
                return raw
            try:
                return json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise AdapterError(
                    PROVIDER,
                    f"{request.tool.name} arguments were not valid JSON "
                    f"({exc}); first 200 chars: {str(raw)[:200]}",
                ) from exc

        # `response_format` mode, or a server that answered in prose.
        content = (message.get("content") or "").strip()
        if content:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                pass
        raise AdapterError(
            PROVIDER,
            f"model did not call {request.tool.name} "
            f"(finish_reason={finish}): {content[:300]}",
        )
