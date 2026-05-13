import asyncio

import pytest
from sqlalchemy import select

from app.db.models.llm_log import LLMRequest
from app.llm.budget import BudgetTracker
from app.llm.cache import ResponseCache
from app.llm.config import RouterConfig, TierConfig
from app.llm.log_repo import LLMLogRepo
from app.llm.providers.mock import MockProvider
from app.llm.router import LLMRouter
from app.llm.types import Message


@pytest.mark.asyncio
async def test_router_call_writes_llm_log_row(db_engine, db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sf = async_sessionmaker(db_engine, expire_on_commit=False)
    log_repo = LLMLogRepo(session_factory=sf)
    p = MockProvider(name="mockp", default_text="hi")
    cfg = RouterConfig(tiers={"extraction": TierConfig(providers=["mockp"], bypass_budget=True)})
    r = LLMRouter(
        cfg,
        {"mockp": p},
        ResponseCache(session_factory=None, enabled=False),
        BudgetTracker(hourly_usd=10.0),
        log_repo,
    )

    trace_id = "trace-llm-log-test"
    await r.generate([Message(role="user", content="hello")], task="extraction", trace_id=trace_id)
    await log_repo.drain()

    # Allow Postgres to flush.
    await asyncio.sleep(0.05)

    rows = (
        await db_session.execute(
            select(LLMRequest).where(LLMRequest.trace_id == trace_id)
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.tier == "extraction"
    assert row.provider == "mockp"
    assert row.tokens_in == 1
    assert row.tokens_out == 1
    assert row.cache_hit is False
    assert row.status == "ok"
