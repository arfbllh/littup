from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.llm.types import LLMResponse, Message, ProviderUnavailable, SamplingParams

_DEFAULT = SamplingParams()


class MockProvider:
    """Test double. Register canned responses by prompt substring; tracks call_count."""

    def __init__(
        self,
        name: str = "mock",
        *,
        fail: bool = False,
        cost_per_call: float = 0.01,
    ) -> None:
        self.name = name
        self.capabilities: set[Literal["text", "json", "tools", "vision", "streaming"]] = {"text", "json"}
        self._fail = fail
        self._cost = cost_per_call
        self._registrations: list[tuple[str, LLMResponse]] = []
        self.call_count = 0

    def register(self, prompt_substring: str, response: LLMResponse) -> None:
        self._registrations.append((prompt_substring, response))

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        sampling: SamplingParams = _DEFAULT,
    ) -> LLMResponse:
        if self._fail:
            raise ProviderUnavailable(f"MockProvider '{self.name}' configured to fail")

        self.call_count += 1

        full_text = " ".join(
            m.content
            if isinstance(m.content, str)
            else " ".join(p.text or "" for p in m.content)
            for m in messages
        )
        for substring, response in self._registrations:
            if substring in full_text:
                return response

        return LLMResponse(
            text="mock response",
            model_used=self.name,
            provider=self.name,
            tokens_in=10,
            tokens_out=10,
            cost_usd=self._cost,
        )

    async def health(self) -> bool:
        return not self._fail

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return self._cost
