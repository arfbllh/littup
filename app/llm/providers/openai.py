from __future__ import annotations

import json
import os
from typing import Any

from app.llm.errors import ProviderUnavailable, RateLimited, SchemaViolation
from app.llm.providers.base import LLMProvider, cost_from_pricing, now_ms
from app.llm.types import LLMResponse, Message, ProviderCapability, SamplingParams


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        name: str,
        *,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        timeout_s: int = 60,
        input_cost_per_1k: float = 0.005,
        output_cost_per_1k: float = 0.015,
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
            raise ProviderUnavailable(f"openai:{self.name}: api key missing")
        try:
            import openai
        except ImportError as e:
            raise ProviderUnavailable("openai SDK not installed") from e
        self._client = openai.AsyncOpenAI(api_key=self._api_key, timeout=self.timeout_s)
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

        payload_messages = [
            {
                "role": m.role,
                "content": m.content if isinstance(m.content, str) else [p.model_dump() for p in m.content],
            }
            for m in messages
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
            "max_tokens": sampling.max_tokens,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
        }
        if sampling.stop:
            kwargs["stop"] = sampling.stop
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "out", "schema": schema, "strict": True},
            }

        t0 = now_ms()
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as e:
            klass = type(e).__name__
            if "RateLimit" in klass:
                raise RateLimited(str(e)) from e
            raise ProviderUnavailable(f"openai:{self.name}: {e}") from e

        choice = resp.choices[0]
        text = choice.message.content or ""
        structured: dict[str, Any] | None = None
        if schema is not None:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as e:
                raise SchemaViolation(f"openai:{self.name}: non-JSON output") from e

        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0

        return LLMResponse(
            text=text,
            structured=structured,
            model_used=self.model,
            provider=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self.cost_estimate(tokens_in, tokens_out),
            latency_ms=now_ms() - t0,
            finish_reason=choice.finish_reason or "stop",
        )

    async def health(self) -> bool:
        return self._available

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return cost_from_pricing(tokens_in, tokens_out, self.input_cost_per_1k, self.output_cost_per_1k)
