from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import BaseModel

from app.llm.types import LLMResponse, Message, ProviderUnavailable, SamplingParams, SchemaViolation

_DEFAULT = SamplingParams()


class VLLMProvider:
    """OpenAI-compatible client for local vLLM. cost_estimate always returns 0."""

    def __init__(self, name: str, base_url: str, model: str, timeout_s: int = 60) -> None:
        self.name = name
        self.capabilities: set[Literal["text", "json", "tools", "vision", "streaming"]] = {"text", "json"}
        self._model = model
        self._timeout = timeout_s
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        sampling: SamplingParams = _DEFAULT,
    ) -> LLMResponse:
        payload: dict = {
            "model": self._model,
            "messages": [self._fmt(m) for m in messages],
            "max_tokens": sampling.max_tokens,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
        }
        if sampling.stop:
            payload["stop"] = sampling.stop
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema.model_json_schema()},
            }

        try:
            resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
        except httpx.TimeoutException as e:
            raise ProviderUnavailable(f"vLLM timeout: {e}") from e
        except httpx.HTTPStatusError as e:
            raise ProviderUnavailable(f"vLLM HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise ProviderUnavailable(f"vLLM connection error: {e}") from e

        data = resp.json()
        choice = data["choices"][0]
        content: str = choice["message"]["content"] or ""
        usage = data.get("usage", {})

        structured = None
        if schema is not None:
            try:
                structured = json.loads(content)
            except json.JSONDecodeError as e:
                raise SchemaViolation(f"vLLM returned invalid JSON: {e}") from e

        return LLMResponse(
            text=content,
            structured=structured,
            model_used=data.get("model", self._model),
            provider=self.name,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            cost_usd=0.0,
            finish_reason=choice.get("finish_reason", "stop"),
        )

    @staticmethod
    def _fmt(m: Message) -> dict:
        if isinstance(m.content, str):
            return {"role": m.role, "content": m.content}
        return {
            "role": m.role,
            "content": [
                {"type": p.type, "text": p.text}
                if p.type == "text"
                else {"type": "image_url", "image_url": p.source}
                for p in m.content
            ],
        }

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return 0.0
