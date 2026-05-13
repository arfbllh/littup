from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from app.llm.types import LLMResponse, Message, ProviderCapability, SamplingParams


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str
    capabilities: set[ProviderCapability]

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        sampling: SamplingParams | None = None,
    ) -> LLMResponse: ...

    async def health(self) -> bool: ...

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float: ...


def now_ms() -> int:
    return int(time.monotonic() * 1000)


def cost_from_pricing(
    tokens_in: int,
    tokens_out: int,
    input_cost_per_1k: float,
    output_cost_per_1k: float,
) -> float:
    return (tokens_in / 1000.0) * input_cost_per_1k + (tokens_out / 1000.0) * output_cost_per_1k
