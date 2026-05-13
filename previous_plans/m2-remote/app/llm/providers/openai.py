from __future__ import annotations

import json
from typing import Literal

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai import RateLimitError as OpenAIRateLimitError
from pydantic import BaseModel

from app.llm.types import (
    LLMResponse,
    Message,
    ProviderUnavailable,
    RateLimited,
    SamplingParams,
    SchemaViolation,
)

# USD per 1M tokens: (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}
_DEFAULT_PRICING = (2.00, 8.00)

_DEFAULT = SamplingParams()


class OpenAIProvider:
    """OpenAI GPT via official SDK. Supports json_schema response_format."""

    def __init__(
        self,
        name: str,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.name = name
        self.capabilities: set[Literal["text", "json", "tools", "vision", "streaming"]] = {
            "text",
            "json",
            "tools",
            "vision",
        }
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key or "invalid", base_url=base_url)

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        sampling: SamplingParams = _DEFAULT,
    ) -> LLMResponse:
        api_messages = [self._fmt(m) for m in messages]
        kwargs: dict = {
            "model": self._model,
            "messages": api_messages,
            "max_tokens": sampling.max_tokens,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
        }
        if sampling.stop:
            kwargs["stop"] = sampling.stop
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "output",
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            }

        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except OpenAIRateLimitError as e:
            raise RateLimited(str(e)) from e
        except APIConnectionError as e:
            raise ProviderUnavailable(str(e)) from e
        except APIStatusError as e:
            raise ProviderUnavailable(f"OpenAI {e.status_code}") from e

        choice = resp.choices[0]
        content = choice.message.content or ""
        usage = resp.usage

        structured = None
        if schema is not None:
            try:
                structured = json.loads(content)
            except json.JSONDecodeError as e:
                raise SchemaViolation(f"OpenAI returned invalid JSON: {e}") from e

        in_tok = usage.prompt_tokens if usage else 0
        out_tok = usage.completion_tokens if usage else 0

        return LLMResponse(
            text=content,
            structured=structured,
            model_used=resp.model,
            provider=self.name,
            tokens_in=in_tok,
            tokens_out=out_tok,
            cost_usd=self._cost(in_tok, out_tok),
            finish_reason=choice.finish_reason or "stop",
        )

    @staticmethod
    def _fmt(m: Message) -> dict:
        if isinstance(m.content, str):
            return {"role": m.role, "content": m.content}
        parts: list[dict] = []
        for p in m.content:
            if p.type == "text":
                parts.append({"type": "text", "text": p.text or ""})
            elif p.type == "image" and p.source:
                parts.append({"type": "image_url", "image_url": {"url": p.source.get("url", "")}})
        return {"role": m.role, "content": parts}

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        in_p, out_p = _PRICING.get(self._model, _DEFAULT_PRICING)
        return (tokens_in * in_p + tokens_out * out_p) / 1_000_000

    async def health(self) -> bool:
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return self._cost(tokens_in, tokens_out)
