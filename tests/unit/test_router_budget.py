import pytest

from app.core.errors import BudgetExceededError
from app.llm.budget import BudgetTracker
from app.llm.cache import ResponseCache
from app.llm.config import RouterConfig, TierConfig
from app.llm.log_repo import LLMLogRepo
from app.llm.providers.mock import MockProvider
from app.llm.router import LLMRouter
from app.llm.types import Message


@pytest.mark.asyncio
async def test_budget_blocks_expensive_hosted_tier():
    p = MockProvider(name="hosted", input_cost_per_1k=10.0, output_cost_per_1k=10.0)
    cfg = RouterConfig(tiers={"generation": TierConfig(providers=["hosted"], bypass_budget=False)})
    r = LLMRouter(
        cfg,
        {"hosted": p},
        ResponseCache(session_factory=None, enabled=False),
        BudgetTracker(hourly_usd=0.01),
        LLMLogRepo(session_factory=None),
    )
    with pytest.raises(BudgetExceededError):
        await r.generate([Message(role="user", content="hi")], task="generation")
    assert p.call_count == 0


@pytest.mark.asyncio
async def test_local_zero_cost_tier_passes_through_when_budget_empty():
    """Local providers have cost_estimate==0, so they bypass the budget gate
    even without bypass_budget set."""
    p = MockProvider(name="local", input_cost_per_1k=0.0, output_cost_per_1k=0.0, default_text="ok")
    cfg = RouterConfig(tiers={"validation": TierConfig(providers=["local"], bypass_budget=False)})
    r = LLMRouter(
        cfg,
        {"local": p},
        ResponseCache(session_factory=None, enabled=False),
        BudgetTracker(hourly_usd=0.0),
        LLMLogRepo(session_factory=None),
    )
    resp = await r.generate([Message(role="user", content="hi")], task="validation")
    assert resp.text == "ok"


@pytest.mark.asyncio
async def test_bypass_budget_flag_lets_call_through_even_with_cost():
    p = MockProvider(name="x", input_cost_per_1k=10.0, output_cost_per_1k=10.0, default_text="ok")
    cfg = RouterConfig(tiers={"extraction": TierConfig(providers=["x"], bypass_budget=True)})
    r = LLMRouter(
        cfg,
        {"x": p},
        ResponseCache(session_factory=None, enabled=False),
        BudgetTracker(hourly_usd=0.0),
        LLMLogRepo(session_factory=None),
    )
    resp = await r.generate([Message(role="user", content="hi")], task="extraction")
    assert resp.text == "ok"
