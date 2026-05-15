from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.llm.errors import ProviderUnavailable, RateLimited, SchemaViolation
from app.llm.providers.base import LLMProvider, cost_from_pricing, now_ms
from app.llm.types import LLMResponse, Message, ProviderCapability, SamplingParams


class OllamaProvider(LLMProvider):
    """Talks to a local Ollama daemon via the native /api/generate endpoint.

    Also works against any Ollama-compatible server — just point base_url at it.
    base_url should be the daemon root (e.g. http://localhost:11434), not the
    /v1 shim path.
    """

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
        # Strip /v1 suffix if someone migrated from the old OpenAI-compat config.
        base = base_url.rstrip("/")
        self.base_url = base[:-3] if base.endswith("/v1") else base
        self.timeout_s = timeout_s
        self.input_cost_per_1k = input_cost_per_1k
        self.output_cost_per_1k = output_cost_per_1k
        self.capabilities: set[ProviderCapability] = {"text", "json", "streaming"}
        self._api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        # Tight connect timeout so a not-running daemon fails over to the
        # hosted fallback in ~5 s instead of waiting the full read timeout.
        timeout = httpx.Timeout(connect=5.0, read=timeout_s, write=10.0, pool=5.0)
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=headers)

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        sampling: SamplingParams | None = None,
    ) -> LLMResponse:
        sampling = sampling or SamplingParams()

        ollama_messages: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m.content, str):
                ollama_messages.append({"role": m.role, "content": m.content})
            else:
                text_parts: list[str] = []
                images: list[str] = []
                for part in m.content:
                    t = getattr(part, "type", None)
                    if t == "text":
                        text_parts.append(getattr(part, "text", ""))
                    elif t == "image":
                        src = getattr(part, "source", None)
                        if src and getattr(src, "data", None):
                            images.append(src.data)
                msg: dict[str, Any] = {"role": m.role, "content": "\n".join(text_parts)}
                if images:
                    msg["images"] = images
                ollama_messages.append(msg)

        options: dict[str, Any] = {
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "num_predict": sampling.max_tokens,
        }
        if sampling.stop:
            options["stop"] = sampling.stop

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": ollama_messages,
            "stream": False,
            "options": options,
        }
        if schema is not None:
            payload["format"] = schema

        t0 = now_ms()
        try:
            resp = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.NetworkError) as e:
            raise ProviderUnavailable(f"ollama:{self.name}: {e}") from e

        if resp.status_code == 429:
            raise RateLimited(f"ollama:{self.name}: 429")
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"ollama:{self.name}: {resp.status_code}")
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"ollama:{self.name}: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        message = data.get("message", {})
        text = message.get("content") or ""
        tokens_in = data.get("prompt_eval_count", 0)
        tokens_out = data.get("eval_count", 0)
        finish_reason = data.get("done_reason", "stop")

        structured = None
        if schema is not None:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as e:
                raise SchemaViolation(f"ollama:{self.name}: non-JSON output") from e

        return LLMResponse(
            text=text,
            structured=structured,
            model_used=self.model,
            provider=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self.cost_estimate(tokens_in, tokens_out),
            latency_ms=now_ms() - t0,
            finish_reason=finish_reason,
        )

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            return r.status_code < 500
        except Exception:
            return False

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return cost_from_pricing(tokens_in, tokens_out, self.input_cost_per_1k, self.output_cost_per_1k)
