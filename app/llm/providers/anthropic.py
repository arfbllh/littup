from __future__ import annotations

import os
from typing import Any

from app.llm.errors import ProviderUnavailable, RateLimited, SchemaViolation
from app.llm.providers.base import LLMProvider, cost_from_pricing, now_ms
from app.llm.types import LLMResponse, Message, ProviderCapability, SamplingParams


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider. Uses tool-use for JSON-schema structured output."""

    def __init__(
        self,
        name: str,
        *,
        model: str,
        api_key_env: str = "ANTHROPIC_API_KEY",
        timeout_s: int = 60,
        input_cost_per_1k: float = 0.0008,
        output_cost_per_1k: float = 0.004,
        client: Any | None = None,
    ):
        self.name = name
        self.model = model
        self.timeout_s = timeout_s
        self.input_cost_per_1k = input_cost_per_1k
        self.output_cost_per_1k = output_cost_per_1k
        self.capabilities: set[ProviderCapability] = {"text", "json", "tools", "vision"}
        self._api_key = os.environ.get(api_key_env, "")
        self._client = client
        self._available = bool(self._api_key) or client is not None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ProviderUnavailable(f"anthropic:{self.name}: api key missing")
        try:
            import anthropic
        except ImportError as e:
            raise ProviderUnavailable("anthropic SDK not installed") from e
        self._client = anthropic.AsyncAnthropic(api_key=self._api_key, timeout=self.timeout_s)
        return self._client

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        sampling: SamplingParams | None = None,
    ) -> LLMResponse:
        sampling = sampling or SamplingParams()
        client = self._ensure_client()

        system_blocks: list[str] = []
        rest: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                if isinstance(m.content, str):
                    system_blocks.append(m.content)
            else:
                content = m.content if isinstance(m.content, str) else [p.model_dump() for p in m.content]
                rest.append({"role": m.role, "content": content})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": sampling.max_tokens,
            "temperature": sampling.temperature,
            "messages": rest,
        }
        if system_blocks:
            kwargs["system"] = "\n\n".join(system_blocks)
        if sampling.stop:
            kwargs["stop_sequences"] = sampling.stop
        if schema is not None:
            kwargs["tools"] = [
                {"name": "return", "description": "Return the answer.", "input_schema": schema}
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": "return"}

        t0 = now_ms()
        try:
            resp = await client.messages.create(**kwargs)
        except Exception as e:
            klass = type(e).__name__
            if "RateLimit" in klass:
                raise RateLimited(str(e)) from e
            raise ProviderUnavailable(f"anthropic:{self.name}: {e}") from e

        text = ""
        structured: dict[str, Any] | None = None
        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text += getattr(block, "text", "") or ""
            elif btype == "tool_use" and getattr(block, "name", "") == "return":
                structured = getattr(block, "input", None) or {}

        if schema is not None and structured is None:
            raise SchemaViolation(f"anthropic:{self.name}: model did not call return tool")

        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "output_tokens", 0) if usage else 0

        return LLMResponse(
            text=text,
            structured=structured,
            model_used=self.model,
            provider=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self.cost_estimate(tokens_in, tokens_out),
            latency_ms=now_ms() - t0,
            finish_reason=getattr(resp, "stop_reason", "stop") or "stop",
        )

    async def health(self) -> bool:
        return self._available

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return cost_from_pricing(tokens_in, tokens_out, self.input_cost_per_1k, self.output_cost_per_1k)
