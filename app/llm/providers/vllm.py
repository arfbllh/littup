from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.llm.errors import ProviderUnavailable, RateLimited, SchemaViolation
from app.llm.providers.base import LLMProvider, cost_from_pricing, now_ms
from app.llm.types import LLMResponse, Message, ProviderCapability, SamplingParams


class VLLMProvider(LLMProvider):
    """Talks to a vLLM-compatible OpenAI HTTP endpoint (local vLLM or hosted, e.g. RunPod)."""

    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        model: str,
        timeout_s: int = 60,
        input_cost_per_1k: float = 0.0,
        output_cost_per_1k: float = 0.0,
        api_key_env: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.input_cost_per_1k = input_cost_per_1k
        self.output_cost_per_1k = output_cost_per_1k
        self.capabilities: set[ProviderCapability] = {"text", "json", "streaming"}
        # Hosted vLLM endpoints (RunPod, etc.) typically require a Bearer token.
        # A local vLLM with no auth leaves api_key_env unset → no header sent.
        self._api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        self._client = client or httpx.AsyncClient(timeout=timeout_s, headers=headers)

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        sampling: SamplingParams | None = None,
    ) -> LLMResponse:
        sampling = sampling or SamplingParams()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": m.role, "content": m.content if isinstance(m.content, str) else [p.model_dump() for p in m.content]}
                for m in messages
            ],
            "max_tokens": sampling.max_tokens,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
        }
        if sampling.stop:
            payload["stop"] = sampling.stop
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}

        t0 = now_ms()
        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.NetworkError) as e:
            raise ProviderUnavailable(f"vllm:{self.name}: {e}") from e

        if resp.status_code == 429:
            raise RateLimited(f"vllm:{self.name}: 429")
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"vllm:{self.name}: {resp.status_code}")
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"vllm:{self.name}: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        usage = data.get("usage", {})
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)

        structured = None
        if schema is not None:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as e:
                raise SchemaViolation(f"vllm:{self.name}: non-JSON output") from e

        return LLMResponse(
            text=text,
            structured=structured,
            model_used=self.model,
            provider=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self.cost_estimate(tokens_in, tokens_out),
            latency_ms=now_ms() - t0,
            finish_reason=choice.get("finish_reason", "stop"),
        )

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/models", timeout=5.0)
            return r.status_code < 500
        except Exception:
            return False

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return cost_from_pricing(tokens_in, tokens_out, self.input_cost_per_1k, self.output_cost_per_1k)
