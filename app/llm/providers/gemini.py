from __future__ import annotations

import json
from typing import Literal

from google import genai
from google.genai import types as genai_types
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
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
}
_DEFAULT_PRICING = (1.25, 10.00)

_DEFAULT = SamplingParams()


class GeminiProvider:
    """Google Gemini via google-genai SDK."""

    def __init__(self, name: str, model: str, api_key: str | None = None) -> None:
        self.name = name
        self.capabilities: set[Literal["text", "json", "tools", "vision", "streaming"]] = {
            "text",
            "json",
            "tools",
            "vision",
        }
        self._model = model
        self._client = genai.Client(api_key=api_key or "")

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        sampling: SamplingParams = _DEFAULT,
    ) -> LLMResponse:
        system_parts, contents = self._split(messages)

        config_kwargs: dict = {
            "max_output_tokens": sampling.max_tokens,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
        }
        if sampling.stop:
            config_kwargs["stop_sequences"] = sampling.stop
        if schema is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = schema

        gen_config = genai_types.GenerateContentConfig(**config_kwargs)
        if system_parts:
            gen_config.system_instruction = "\n\n".join(system_parts)

        try:
            resp = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=gen_config,
            )
        except Exception as e:
            msg = str(e).lower()
            if "rate" in msg or "quota" in msg or "429" in msg:
                raise RateLimited(str(e)) from e
            raise ProviderUnavailable(str(e)) from e

        text = resp.text or ""

        structured = None
        if schema is not None:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as e:
                raise SchemaViolation(f"Gemini returned invalid JSON: {e}") from e

        usage = resp.usage_metadata
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0

        return LLMResponse(
            text=text,
            structured=structured,
            model_used=self._model,
            provider=self.name,
            tokens_in=in_tok,
            tokens_out=out_tok,
            cost_usd=self._cost(in_tok, out_tok),
            finish_reason="stop",
        )

    @staticmethod
    def _split(messages: list[Message]) -> tuple[list[str], list[genai_types.Content]]:
        system_parts: list[str] = []
        contents: list[genai_types.Content] = []
        for m in messages:
            if m.role == "system":
                text = (
                    m.content
                    if isinstance(m.content, str)
                    else " ".join(p.text or "" for p in m.content if p.type == "text")
                )
                system_parts.append(text)
            else:
                role = "user" if m.role == "user" else "model"
                if isinstance(m.content, str):
                    parts = [genai_types.Part(text=m.content)]
                else:
                    parts = [genai_types.Part(text=p.text or "") for p in m.content if p.type == "text"]
                contents.append(genai_types.Content(role=role, parts=parts))
        return system_parts, contents

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        in_p, out_p = _PRICING.get(self._model, _DEFAULT_PRICING)
        return (tokens_in * in_p + tokens_out * out_p) / 1_000_000

    async def health(self) -> bool:
        try:
            async for _ in self._client.aio.models.list():
                return True
            return True
        except Exception:
            return False

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return self._cost(tokens_in, tokens_out)
