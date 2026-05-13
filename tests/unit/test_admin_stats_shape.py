import pytest

from app.api.deps import get_llm_router, reset_router_for_tests
from app.llm.budget import BudgetTracker
from app.llm.cache import ResponseCache
from app.llm.config import RouterConfig, TierConfig
from app.llm.log_repo import LLMLogRepo
from app.llm.providers.mock import MockProvider
from app.llm.router import LLMRouter
from app.main import app


@pytest.fixture
def fake_router():
    cfg = RouterConfig(tiers={"extraction": TierConfig(providers=["m"])})
    r = LLMRouter(
        cfg,
        {"m": MockProvider(name="m")},
        ResponseCache(session_factory=None, enabled=False),
        BudgetTracker(hourly_usd=5.0),
        LLMLogRepo(session_factory=None),
    )
    app.dependency_overrides[get_llm_router] = lambda: r
    yield r
    app.dependency_overrides.pop(get_llm_router, None)
    reset_router_for_tests()


@pytest.mark.asyncio
async def test_admin_stats_returns_expected_shape(client, fake_router):
    res = await client.get("/admin/llm-stats")
    assert res.status_code == 200
    body = res.json()
    assert body["window"] == "1h"
    assert set(body["totals"].keys()) >= {
        "calls",
        "cache_hits",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "cache_hit_rate",
    }
    assert isinstance(body["by_tier"], list)
    assert isinstance(body["by_provider"], list)
    assert body["budget"]["hourly_usd"] == 5.0
    assert body["budget"]["remaining_usd"] >= 0
