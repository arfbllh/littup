import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.llm.budget import BudgetTracker
from app.llm.config import CacheConfig, ProviderConfig, RouterConfig
from app.llm.log_repo import LLMLogRepo
from app.llm.providers.mock import MockProvider
from app.llm.router import LLMRouter
from app.llm.types import LLMResponse, Message


class _NoopCache:
    async def get(self, key):
        return None

    async def put(self, key, response, *, ttl_hours, model=""):
        return None


@pytest.mark.asyncio
async def test_router_call_writes_llm_log_row(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    provider = MockProvider(name="mock-int", cost_per_call=0.0)
    provider.register(
        "integration_test_marker",
        LLMResponse(
            text="ok",
            model_used="mock-model-x",
            provider="mock-int",
            tokens_in=42,
            tokens_out=17,
            cost_usd=0.0,
        ),
    )

    config = RouterConfig(
        default_locale="local",
        tiers={"extraction": ["mock-int"]},
        providers={"mock-int": ProviderConfig(type="mock", model="mock-model-x")},
        cache=CacheConfig(enabled=False),
    )

    log_repo = LLMLogRepo(session_factory)
    log_repo.start()
    try:
        router = LLMRouter(
            config=config,
            providers={"mock-int": provider},
            cache=_NoopCache(),  # type: ignore[arg-type]
            budget=BudgetTracker(hourly_limit_usd=10.0),
            log_repo=log_repo,
        )

        trace_id = "trace-int-llm-log-42"
        await router.generate(
            [Message(role="user", content="integration_test_marker")],
            task="extraction",
            trace_id=trace_id,
        )

        # Wait for background drain
        await asyncio.wait_for(log_repo._queue.join(), timeout=5.0)  # noqa: SLF001
    finally:
        await log_repo.stop()

    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT trace_id, tier, provider, model, tokens_in, tokens_out, cost_usd, status, cache_hit "
                "FROM llm_log.llm_requests WHERE trace_id = :tid"
            ),
            {"tid": trace_id},
        )
        row = result.mappings().one()

    assert row["trace_id"] == trace_id
    assert row["tier"] == "extraction"
    assert row["provider"] == "mock-int"
    assert row["model"] == "mock-model-x"
    assert row["tokens_in"] == 42
    assert row["tokens_out"] == 17
    assert float(row["cost_usd"]) == 0.0
    assert row["status"] == "ok"
    assert row["cache_hit"] is False
