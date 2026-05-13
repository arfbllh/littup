"""Shared test fixtures for router unit tests — in-memory fakes for cache/log_repo."""
from __future__ import annotations

from app.llm.budget import BudgetTracker
from app.llm.config import CacheConfig, ProviderConfig, RouterConfig
from app.llm.router import LLMRouter
from app.llm.types import LLMResponse


class InMemoryCache:
    def __init__(self) -> None:
        self.store: dict[str, LLMResponse] = {}

    async def get(self, key: str) -> LLMResponse | None:
        hit = self.store.get(key)
        if hit is None:
            return None
        # Return a copy so we don't accidentally mutate the stored one
        copy = hit.model_copy()
        copy.cache_hit = True
        return copy

    async def put(self, key: str, response: LLMResponse, *, ttl_hours: int, model: str = "") -> None:
        self.store[key] = response


class InMemoryLogRepo:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def record(self, **kwargs) -> None:
        self.records.append(kwargs)


def make_router(
    providers: dict,
    tiers: dict[str, list[str]],
    *,
    hourly_limit_usd: float = 100.0,
    cache_enabled: bool = True,
) -> tuple[LLMRouter, InMemoryCache, InMemoryLogRepo, BudgetTracker]:
    config = RouterConfig(
        default_locale="local",
        tiers=tiers,
        providers={
            name: ProviderConfig(type="mock", model=name) for name in providers
        },
        cache=CacheConfig(enabled=cache_enabled, ttl_hours=24),
    )
    cache = InMemoryCache()
    log_repo = InMemoryLogRepo()
    budget = BudgetTracker(hourly_limit_usd=hourly_limit_usd)
    router = LLMRouter(
        config=config,
        providers=providers,
        cache=cache,  # type: ignore[arg-type]
        budget=budget,
        log_repo=log_repo,  # type: ignore[arg-type]
    )
    return router, cache, log_repo, budget
