from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.llm.errors import ProviderUnavailable
from app.llm.providers.base import LLMProvider
from app.llm.types import LLMResponse, Message, ProviderCapability, SamplingParams


class MockProvider(LLMProvider):
    """Test/eval provider. Returns canned responses keyed by a substring match.

    Registration order matters: first matching rule wins.
    """

    def __init__(
        self,
        name: str = "mock",
        *,
        model: str = "mock-1",
        default_text: str = "ok",
        capabilities: set[ProviderCapability] | None = None,
        input_cost_per_1k: float = 0.0,
        output_cost_per_1k: float = 0.0,
        healthy: bool = True,
    ):
        self.name = name
        self.model = model
        self.capabilities = capabilities or {"text", "json"}
        self._rules: list[tuple[Callable[[str], bool], Any]] = []
        self._default = default_text
        self.call_count = 0
        self.input_cost_per_1k = input_cost_per_1k
        self.output_cost_per_1k = output_cost_per_1k
        self._healthy = healthy

    def register(
        self,
        substring: str | Callable[[str], bool],
        response: Any,
    ) -> None:
        predicate = substring if callable(substring) else (lambda s, sub=substring: sub in s)
        self._rules.append((predicate, response))

    def set_default(self, text: str) -> None:
        self._default = text

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        sampling: SamplingParams | None = None,
    ) -> LLMResponse:
        self.call_count += 1
        flat = "\n".join(
            m.content if isinstance(m.content, str) else "" for m in messages
        )
        for pred, resp in self._rules:
            if pred(flat):
                if isinstance(resp, Exception):
                    raise resp
                if isinstance(resp, LLMResponse):
                    return resp
                if isinstance(resp, dict):
                    return LLMResponse(
                        text=resp.get("text", ""),
                        structured=resp.get("structured"),
                        model_used=self.model,
                        provider=self.name,
                        tokens_in=resp.get("tokens_in", 1),
                        tokens_out=resp.get("tokens_out", 1),
                        cost_usd=resp.get("cost_usd", 0.0),
                        latency_ms=resp.get("latency_ms", 1),
                    )
        return LLMResponse(
            text=self._default,
            model_used=self.model,
            provider=self.name,
            tokens_in=1,
            tokens_out=1,
        )

    async def health(self) -> bool:
        if isinstance(self._healthy, Exception):
            raise ProviderUnavailable(str(self._healthy))
        return self._healthy

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in / 1000.0) * self.input_cost_per_1k + (
            tokens_out / 1000.0
        ) * self.output_cost_per_1k
