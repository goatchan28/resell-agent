"""Anthropic adapter. The first implementation, not a privileged one.

Everything here is shaped by Anthropic's API and belongs nowhere else: base64
image blocks with a media type, `tools` plus `tool_choice`, a `content` array of
typed blocks, and `usage.input_tokens` / `usage.output_tokens`.

Notes for whoever writes the next adapter, since these are the differences that
actually cost time:

  - Tool arguments arrive here as a dict. OpenAI returns them as a JSON *string*
    that must be parsed; Gemini nests them under functionCall.args.
  - Images are base64 with a separate media_type field. OpenAI takes a data URL;
    Gemini takes inlineData with mimeType.
  - Token fields are input_tokens/output_tokens. OpenAI says prompt_tokens/
    completion_tokens; Gemini says promptTokenCount/candidatesTokenCount.
  - This schema is passed through unchanged. OpenAI strict mode additionally
    requires additionalProperties: false and every property listed in required.

Normalising all of that into StageResult is this layer's whole job.
"""

from __future__ import annotations

import base64
import os
import time

from resell.reasoning.adapters.base import AdapterError
from resell.reasoning.budget import ModelRates
from resell.reasoning.stages import StageRequest, StageResult, Usage

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
PROVIDER = "anthropic"


class AnthropicAdapter:
    provider = PROVIDER

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        transport=None,
        timeout_s: float = 180.0,
    ):
        self.model = model or os.environ.get("RESELL_VISION_MODEL", DEFAULT_MODEL)
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._transport = transport
        self._timeout_s = timeout_s

    # --- provider-specific estimation ----------------------------------------

    # Anthropic bills images by area, at roughly width*height/750 tokens. Other
    # providers use fixed tiles or entirely different accounting, which is why this
    # is not in the neutral budget code.
    IMAGE_TOKENS_PER_PIXEL = 1 / 750
    CHARS_PER_TOKEN = 3.5  # deliberately low: over-estimating is the safe direction

    def estimate_input_tokens(self, request: StageRequest) -> int:
        from resell.images import inspect

        total = 0
        for image in request.images:
            facts = inspect(image.path)
            if facts.width and facts.height:
                total += int(facts.width * facts.height * self.IMAGE_TOKENS_PER_PIXEL)
            else:
                # Dimensions unreadable: assume the largest image we would ever send
                # rather than assume it is small.
                total += int(1568 * 1568 * self.IMAGE_TOKENS_PER_PIXEL)
        text = request.system_prompt + request.instruction + repr(request.tool.json_schema)
        total += int(len(text) / self.CHARS_PER_TOKEN)
        return total

    def rates(self) -> ModelRates:
        return ModelRates.from_env(self.provider, self.model)

    # --- provider-specific encoding -----------------------------------------

    def _content_blocks(self, request: StageRequest) -> list[dict]:
        blocks: list[dict] = []
        for image in request.images:
            blocks.append({"type": "text", "text": f"Photo position {image.position}:"})
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(image.path.read_bytes()).decode(),
                    },
                }
            )
        blocks.append({"type": "text", "text": request.instruction})
        return blocks

    def _payload(self, request: StageRequest) -> dict:
        payload = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": request.system_prompt,
            "tools": [
                {
                    "name": request.tool.name,
                    "description": request.tool.description,
                    "input_schema": request.tool.json_schema,
                }
            ],
            "messages": [{"role": "user", "content": self._content_blocks(request)}],
        }
        if request.require_tool:
            payload["tool_choice"] = {"type": "tool", "name": request.tool.name}
        return payload

    # --- the call ------------------------------------------------------------

    def run(self, request: StageRequest) -> StageResult:
        if not self._api_key and self._transport is None:
            raise AdapterError(
                PROVIDER, "ANTHROPIC_API_KEY is not set; add it to .env"
            )

        payload = self._payload(request)
        started = time.monotonic()
        if self._transport is not None:
            body = self._transport(payload)
        else:
            import httpx

            response = httpx.post(
                API_URL,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
                json=payload,
                timeout=httpx.Timeout(self._timeout_s),
            )
            if response.status_code >= 400:
                raise AdapterError(
                    PROVIDER,
                    f"HTTP {response.status_code}: {response.text[:400]}",
                    status_code=response.status_code,
                )
            body = response.json()
        latency_ms = int((time.monotonic() - started) * 1000)

        blocks = body.get("content") or []
        tool_uses = [
            block for block in blocks
            if block.get("type") == "tool_use" and block.get("name") == request.tool.name
        ]
        if not tool_uses:
            spoken = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            raise AdapterError(
                PROVIDER,
                f"model did not call {request.tool.name} "
                f"(stop_reason={body.get('stop_reason')}): {spoken[:300]}",
            )

        raw_usage = body.get("usage") or {}
        return StageResult(
            tool_input=tool_uses[0].get("input") or {},
            usage=Usage(
                input_tokens=int(raw_usage.get("input_tokens", 0)),
                output_tokens=int(raw_usage.get("output_tokens", 0)),
                raw=dict(raw_usage),
            ),
            latency_ms=latency_ms,
            provider=PROVIDER,
            model=body.get("model", self.model),
            stop_reason=body.get("stop_reason"),
            raw_response=body,
        )
