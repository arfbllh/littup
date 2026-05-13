from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from app.llm.types import LLMResponse, Message, SamplingParams


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    capabilities: set[Literal["text", "json", "tools", "vision", "streaming"]]

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        sampling: SamplingParams = ...,
    ) -> LLMResponse: ...

    async def health(self) -> bool: ...

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float: ...
