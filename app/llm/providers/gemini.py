from __future__ import annotations

import json
import os
from typing import Any

from app.llm.errors import ProviderUnavailable, RateLimited, SchemaViolation
from app.llm.providers.base import LLMProvider, cost_from_pricing, now_ms
from app.llm.types import LLMResponse, Message, ProviderCapability, SamplingParams


class GeminiProvider(LLMProvider):
    def __init__(
        self,
        name: str,
        *,
        model: str,
        api_key_env: str = "GEMINI_API_KEY",
        timeout_s: int = 60,
        input_cost_per_1k: float = 0.00125,
        output_cost_per_1k: float = 0.005,
        client: Any | None = None,
    ):
        self.name = name
        self.model = model
        self.timeout_s = timeout_s
        self.input_cost_per_1k = input_cost_per_1k
        self.output_cost_per_1k = output_cost_per_1k
        self.capabilities: set[ProviderCapability] = {"text", "json", "vision"}
        self._api_key = os.environ.get(api_key_env, "")
        self._client = client
        self._available = bool(self._api_key) or client is not None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ProviderUnavailable(f"gemini:{self.name}: api key missing")
        try:
            from google import genai
        except ImportError as e:
            raise ProviderUnavailable("google-genai SDK not installed") from e
        self._client = genai.Client(api_key=self._api_key)
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

        # Concatenate system + user messages; gemini handles `system_instruction` separately.
        system_text = "\n\n".join(
            m.content for m in messages if m.role == "system" and isinstance(m.content, str)
        )
        contents: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            parts = (
                [{"text": m.content}]
                if isinstance(m.content, str)
                else [p.model_dump() for p in m.content]
            )
            contents.append({"role": "user" if m.role == "user" else "model", "parts": parts})

        gen_cfg: dict[str, Any] = {
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "max_output_tokens": sampling.max_tokens,
        }
        if sampling.stop:
            gen_cfg["stop_sequences"] = sampling.stop
        if system_text:
            gen_cfg["system_instruction"] = system_text
        if schema is not None:
            gen_cfg["response_mime_type"] = "application/json"
            gen_cfg["response_schema"] = schema

        t0 = now_ms()
        try:
            resp = await client.aio.models.generate_content(
                model=self.model, contents=contents, config=gen_cfg
            )
        except Exception as e:
            klass = type(e).__name__
            if "RateLimit" in klass or "ResourceExhausted" in klass:
                raise RateLimited(str(e)) from e
            raise ProviderUnavailable(f"gemini:{self.name}: {e}") from e

        text = getattr(resp, "text", "") or ""
        structured: dict[str, Any] | None = None
        if schema is not None:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as e:
                raise SchemaViolation(f"gemini:{self.name}: non-JSON output") from e

        usage = getattr(resp, "usage_metadata", None)
        tokens_in = getattr(usage, "prompt_token_count", 0) if usage else 0
        tokens_out = getattr(usage, "candidates_token_count", 0) if usage else 0

        return LLMResponse(
            text=text,
            structured=structured,
            model_used=self.model,
            provider=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self.cost_estimate(tokens_in, tokens_out),
            latency_ms=now_ms() - t0,
            finish_reason="stop",
        )

    async def health(self) -> bool:
        return self._available

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return cost_from_pricing(tokens_in, tokens_out, self.input_cost_per_1k, self.output_cost_per_1k)
