from __future__ import annotations

import json
from typing import Literal

import anthropic as anthropic_sdk
from pydantic import BaseModel

from app.llm.types import (
    ContentPart,
    LLMResponse,
    Message,
    ProviderUnavailable,
    RateLimited,
    SamplingParams,
    SchemaViolation,
)

# USD per 1M tokens: (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}
_DEFAULT_PRICING = (3.00, 15.00)

_DEFAULT = SamplingParams()


class AnthropicProvider:
    """Anthropic Claude via official SDK. Uses tool-use for structured output."""

    def __init__(self, name: str, model: str, api_key: str | None = None) -> None:
        self.name = name
        self.capabilities: set[Literal["text", "json", "tools", "vision", "streaming"]] = {
            "text",
            "json",
            "tools",
            "vision",
        }
        self._model = model
        self._client = anthropic_sdk.AsyncAnthropic(api_key=api_key or "")

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        sampling: SamplingParams = _DEFAULT,
    ) -> LLMResponse:
        system, api_messages = self._split(messages)

        kwargs: dict = {
            "model": self._model,
            "max_tokens": sampling.max_tokens,
            "temperature": sampling.temperature,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system
        if sampling.stop:
            kwargs["stop_sequences"] = sampling.stop

        if schema is not None:
            kwargs["tools"] = [
                {
                    "name": "return_output",
                    "description": "Return structured output matching the schema exactly.",
                    "input_schema": schema.model_json_schema(),
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": "return_output"}

        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic_sdk.RateLimitError as e:
            raise RateLimited(str(e)) from e
        except anthropic_sdk.APIConnectionError as e:
            raise ProviderUnavailable(str(e)) from e
        except anthropic_sdk.APIStatusError as e:
            raise ProviderUnavailable(f"Anthropic {e.status_code}: {e.message}") from e

        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens

        text_content = ""
        structured = None

        if schema is not None:
            for block in resp.content:
                if block.type == "tool_use" and block.name == "return_output":
                    structured = block.input
                    text_content = json.dumps(structured)
                    break
            if structured is None:
                raise SchemaViolation("Anthropic did not invoke return_output tool")
        else:
            for block in resp.content:
                if block.type == "text":
                    text_content = block.text
                    break

        return LLMResponse(
            text=text_content,
            structured=structured,
            model_used=resp.model,
            provider=self.name,
            tokens_in=in_tok,
            tokens_out=out_tok,
            cost_usd=self._cost(in_tok, out_tok),
            finish_reason=resp.stop_reason or "stop",
        )

    @staticmethod
    def _split(messages: list[Message]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        api: list[dict] = []
        for m in messages:
            if m.role == "system":
                text = m.content if isinstance(m.content, str) else _parts_to_text(m.content)
                system_parts.append(text)
            else:
                api.append({"role": m.role, "content": _format_content(m.content)})
        return "\n\n".join(system_parts), api

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


def _parts_to_text(parts: list[ContentPart]) -> str:
    return " ".join(p.text or "" for p in parts if p.type == "text")


def _format_content(content: str | list[ContentPart]) -> str | list[dict]:
    if isinstance(content, str):
        return content
    result: list[dict] = []
    for p in content:
        if p.type == "text":
            result.append({"type": "text", "text": p.text or ""})
        elif p.type == "image" and p.source:
            result.append({"type": "image", "source": p.source})
    return result
