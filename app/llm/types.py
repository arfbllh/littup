from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

TaskTier = Literal["extraction", "generation", "validation", "vision", "analysis"]


class ContentPart(BaseModel):
    type: Literal["text", "image"]
    text: str | None = None
    source: dict | None = None


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart]


class SamplingParams(BaseModel):
    max_tokens: int = 1024
    temperature: float = 0.2
    top_p: float = 1.0
    stop: list[str] | None = None


class LLMResponse(BaseModel):
    text: str
    structured: dict | None = None
    model_used: str
    provider: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    finish_reason: str = "stop"
    cache_hit: bool = False


# Internal signals — caught only within the router, never exposed as HTTP errors
class ProviderUnavailable(Exception):
    pass


class SchemaViolation(Exception):
    pass


class RateLimited(Exception):
    pass
