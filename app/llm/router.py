from __future__ import annotations

import time
from typing import Any

import structlog

from app.core.errors import BudgetExceededError, LLMUnavailableError
from app.llm.budget import BudgetTracker
from app.llm.cache import ResponseCache
from app.llm.config import RouterConfig
from app.llm.errors import (
    ProviderUnavailable,
    RateLimited,
    SchemaViolation,
)
from app.llm.log_repo import LLMLogRepo
from app.llm.providers.base import LLMProvider
from app.llm.types import LLMResponse, Message, SamplingParams, TaskTier

log = structlog.get_logger(__name__)


class LLMRouter:
    def __init__(
        self,
        config: RouterConfig,
        providers: dict[str, LLMProvider],
        cache: ResponseCache,
        budget: BudgetTracker,
        log_repo: LLMLogRepo,
    ):
        self.config = config
        self.providers = providers
        self.cache = cache
        self.budget = budget
        self.log_repo = log_repo

    def _tier(self, task: TaskTier):
        if task not in self.config.tiers:
            raise LLMUnavailableError(f"No tier configured for task={task}")
        return self.config.tiers[task]

    async def generate(
        self,
        messages: list[Message],
        *,
        task: TaskTier,
        schema: dict[str, Any] | None = None,
        sampling: SamplingParams | None = None,
        model_override: str | None = None,
        cache: bool = True,
        trace_id: str | None = None,
    ) -> LLMResponse:
        sampling = sampling or SamplingParams()
        tier = self._tier(task)
        provider_names = [model_override] if model_override else list(tier.providers)
        if not provider_names:
            raise LLMUnavailableError(f"Empty tier providers for task={task}")

        # Cache lookup uses the first provider's model id as a stable key.
        first_provider = self.providers.get(provider_names[0])
        if first_provider is None:
            raise LLMUnavailableError(f"Unknown provider {provider_names[0]!r}")
        cache_key = self.cache.build_key(
            f"{first_provider.name}:{first_provider.model}",
            messages,
            schema,
            sampling,
        )
        if cache and self.cache.enabled:
            hit = await self.cache.get(cache_key)
            if hit is not None:
                self.log_repo.record(
                    trace_id=trace_id,
                    tier=task,
                    provider=hit.provider,
                    model=hit.model_used,
                    tokens_in=hit.tokens_in,
                    tokens_out=hit.tokens_out,
                    cost_usd=0.0,
                    latency_ms=0,
                    status="ok",
                    cache_hit=True,
                    cache_key=cache_key,
                )
                return hit

        last_error: Exception | None = None
        for provider_name in provider_names:
            provider = self.providers.get(provider_name)
            if provider is None:
                last_error = LLMUnavailableError(f"Unknown provider {provider_name!r}")
                continue

            estimate = provider.cost_estimate(
                tokens_in=sampling.max_tokens, tokens_out=sampling.max_tokens
            )
            if not tier.bypass_budget and estimate > 0 and not self.budget.check(estimate):
                self.log_repo.record(
                    trace_id=trace_id,
                    tier=task,
                    provider=provider_name,
                    model=provider.model,
                    status="budget_blocked",
                    error_code="BUDGET_EXCEEDED",
                    cache_key=cache_key,
                )
                raise BudgetExceededError(
                    f"Hourly LLM budget exhausted (spent ${self.budget.current_spend():.4f} "
                    f"of ${self.budget.hourly_usd:.2f}); blocked tier={task} provider={provider_name}"
                )

            attempts = 2 if (schema is not None and tier.schema_retry) else 1
            for attempt in range(attempts):
                t0 = time.monotonic()
                try:
                    response = await provider.generate(
                        messages, schema=schema, sampling=sampling
                    )
                    response.cached_hit = False
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    response.latency_ms = response.latency_ms or latency_ms
                    if estimate > 0:
                        await self.budget.add(response.cost_usd or estimate)
                    if cache and self.cache.enabled:
                        await self.cache.put(cache_key, response)
                    self.log_repo.record(
                        trace_id=trace_id,
                        tier=task,
                        provider=provider_name,
                        model=response.model_used,
                        tokens_in=response.tokens_in,
                        tokens_out=response.tokens_out,
                        cost_usd=response.cost_usd,
                        latency_ms=response.latency_ms,
                        status="ok",
                        cache_hit=False,
                        cache_key=cache_key,
                    )
                    return response
                except SchemaViolation as e:
                    last_error = e
                    if attempt + 1 < attempts:
                        log.info(
                            "llm.schema_retry",
                            provider=provider_name,
                            task=task,
                            error=str(e),
                        )
                        continue
                    self.log_repo.record(
                        trace_id=trace_id,
                        tier=task,
                        provider=provider_name,
                        model=provider.model,
                        status="error",
                        error_code="SCHEMA_VIOLATION",
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        cache_key=cache_key,
                    )
                    break
                except (ProviderUnavailable, RateLimited) as e:
                    last_error = e
                    self.log_repo.record(
                        trace_id=trace_id,
                        tier=task,
                        provider=provider_name,
                        model=provider.model,
                        status="error",
                        error_code=type(e).__name__,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        cache_key=cache_key,
                    )
                    break  # fail over to next provider

        raise LLMUnavailableError(
            f"All providers failed for tier={task}: {last_error!r}"
        )
