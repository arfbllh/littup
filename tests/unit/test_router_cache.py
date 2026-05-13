import pytest

from app.llm.budget import BudgetTracker
from app.llm.cache import ResponseCache
from app.llm.config import RouterConfig, TierConfig
from app.llm.log_repo import LLMLogRepo
from app.llm.providers.mock import MockProvider
from app.llm.router import LLMRouter
from app.llm.types import Message


@pytest.mark.asyncio
async def test_second_call_hits_cache():
    p = MockProvider(name="p", default_text="canned")
    cfg = RouterConfig(tiers={"extraction": TierConfig(providers=["p"], bypass_budget=True)})
    r = LLMRouter(
        cfg,
        {"p": p},
        ResponseCache(session_factory=None, enabled=True),
        BudgetTracker(hourly_usd=10.0),
        LLMLogRepo(session_factory=None),
    )

    msgs = [Message(role="user", content="hi")]
    r1 = await r.generate(msgs, task="extraction")
    r2 = await r.generate(msgs, task="extraction")

    assert p.call_count == 1
    assert r1.cached_hit is False
    assert r2.cached_hit is True
    assert r2.text == "canned"
