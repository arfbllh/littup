from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

TaskTier = Literal["extraction", "generation", "validation", "vision", "analysis"]
ProviderCapability = Literal["text", "json", "tools", "vision", "streaming"]


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    source: dict[str, Any]


ContentPart = TextPart | ImagePart


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart]


class SamplingParams(BaseModel):
    max_tokens: int = 1024
    temperature: float = 0.2
    top_p: float = 1.0
    stop: list[str] | None = None


class LLMResponse(BaseModel):
    text: str = ""
    structured: dict[str, Any] | None = None
    model_used: str
    provider: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    finish_reason: str = "stop"
    cached_hit: bool = False
    raw: dict[str, Any] | None = Field(default=None, exclude=True)
