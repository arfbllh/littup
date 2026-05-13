import pytest

from app.core.errors import LLMUnavailableError
from app.llm.budget import BudgetTracker
from app.llm.cache import ResponseCache
from app.llm.config import RouterConfig, TierConfig
from app.llm.errors import ProviderUnavailable
from app.llm.log_repo import LLMLogRepo
from app.llm.providers.mock import MockProvider
from app.llm.router import LLMRouter
from app.llm.types import Message


def _router(providers, tier_providers, bypass_budget=True):
    cfg = RouterConfig(tiers={"extraction": TierConfig(providers=tier_providers, bypass_budget=bypass_budget)})
    return LLMRouter(
        cfg,
        providers,
        ResponseCache(session_factory=None, enabled=False),
        BudgetTracker(hourly_usd=100.0),
        LLMLogRepo(session_factory=None),
    )


@pytest.mark.asyncio
async def test_failover_to_secondary_on_provider_unavailable():
    primary = MockProvider(name="primary")
    primary.register("", ProviderUnavailable("boom"))
    secondary = MockProvider(name="secondary", default_text="from-secondary")

    r = _router({"primary": primary, "secondary": secondary}, ["primary", "secondary"])
    resp = await r.generate([Message(role="user", content="hi")], task="extraction")
    assert resp.provider == "secondary"
    assert resp.text == "from-secondary"
    assert primary.call_count == 1
    assert secondary.call_count == 1


@pytest.mark.asyncio
async def test_all_providers_fail_raises_llm_unavailable():
    p = MockProvider(name="p")
    p.register("", ProviderUnavailable("down"))
    q = MockProvider(name="q")
    q.register("", ProviderUnavailable("down"))

    r = _router({"p": p, "q": q}, ["p", "q"])
    with pytest.raises(LLMUnavailableError):
        await r.generate([Message(role="user", content="hi")], task="extraction")
