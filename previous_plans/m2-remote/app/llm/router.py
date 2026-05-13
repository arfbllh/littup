from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from app.core.errors import BudgetExceededError, LLMUnavailableError
from app.llm.cache import ResponseCache, build_key
from app.llm.config import RouterConfig
from app.llm.types import (
    LLMResponse,
    Message,
    ProviderUnavailable,
    RateLimited,
    SamplingParams,
    SchemaViolation,
    TaskTier,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from app.llm.budget import BudgetTracker
    from app.llm.log_repo import LLMLogRepo
    from app.llm.providers.base import LLMProvider

log = structlog.get_logger(__name__)

_SCHEMA_RETRY_INSTRUCTION = (
    "\n\nIMPORTANT: Your previous response did not match the required JSON schema. "
    "Return ONLY valid JSON that exactly satisfies the schema — no prose, no markdown."
)


class LLMRouter:
    def __init__(
        self,
        config: RouterConfig,
        providers: dict[str, LLMProvider],
        cache: ResponseCache,
        budget: BudgetTracker,
        log_repo: LLMLogRepo,
    ) -> None:
        self._config = config
        self._providers = providers
        self._cache = cache
        self._budget = budget
        self._log_repo = log_repo

    async def generate(
        self,
        messages: list[Message],
        *,
        task: TaskTier,
        schema: type[BaseModel] | None = None,
        sampling: SamplingParams | None = None,
        model_override: str | None = None,
        cache: bool = True,
        trace_id: str | None = None,
    ) -> LLMResponse:
        if sampling is None:
            sampling = SamplingParams()

        cache_key = build_key(
            model_id=model_override or task,
            messages=messages,
            schema=schema,
            sampling=sampling,
        )

        if cache and self._config.cache.enabled:
            hit = await self._cache.get(cache_key)
            if hit:
                self._log_repo.record(
                    tier=task,
                    provider=hit.provider,
                    model=hit.model_used,
                    cache_key=cache_key,
                    tokens_in=hit.tokens_in,
                    tokens_out=hit.tokens_out,
                    cost_usd=hit.cost_usd,
                    latency_ms=hit.latency_ms,
                    status="ok",
                    cache_hit=True,
                    trace_id=trace_id,
                )
                return hit

        provider_names = [model_override] if model_override else self._config.tiers.get(task, [])
        if not provider_names:
            raise LLMUnavailableError(f"No providers configured for tier={task}")

        last_error: Exception | None = None

        for provider_name in provider_names:
            provider = self._providers.get(provider_name)
            if provider is None:
                log.warning("router.unknown_provider", provider=provider_name, task=task)
                continue

            est_cost = provider.cost_estimate(sampling.max_tokens, sampling.max_tokens)
            if est_cost > 0 and not await self._budget.check(est_cost):
                raise BudgetExceededError(
                    f"Hourly budget exceeded; estimated cost ${est_cost:.4f} would exceed limit"
                )

            t0 = time.monotonic()
            try:
                response = await provider.generate(messages, schema=schema, sampling=sampling)
            except SchemaViolation:
                retry_messages = list(messages) + [
                    Message(role="user", content=_SCHEMA_RETRY_INSTRUCTION)
                ]
                try:
                    response = await provider.generate(retry_messages, schema=schema, sampling=sampling)
                except (SchemaViolation, ProviderUnavailable, RateLimited) as e:
                    last_error = e
                    log.warning(
                        "router.schema_retry_failed",
                        provider=provider_name,
                        error=str(e),
                    )
                    continue
            except (ProviderUnavailable, RateLimited) as e:
                last_error = e
                log.warning("router.provider_unavailable", provider=provider_name, error=str(e))
                continue

            latency_ms = int((time.monotonic() - t0) * 1000)
            response.latency_ms = latency_ms

            await self._budget.add(provider_name, response.cost_usd)
            if cache and self._config.cache.enabled:
                await self._cache.put(
                    cache_key,
                    response,
                    ttl_hours=self._config.cache.ttl_hours,
                    model=response.model_used,
                )
            self._log_repo.record(
                tier=task,
                provider=response.provider,
                model=response.model_used,
                cache_key=cache_key,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=response.cost_usd,
                latency_ms=latency_ms,
                status="ok",
                cache_hit=False,
                trace_id=trace_id,
            )
            return response

        self._log_repo.record(
            tier=task,
            provider="",
            model="",
            status="failed",
            error_code="LLM_UNAVAILABLE",
            trace_id=trace_id,
        )
        raise LLMUnavailableError(f"All providers failed for tier={task}") from last_error
