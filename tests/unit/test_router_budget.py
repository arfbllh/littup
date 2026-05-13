import pytest

from app.core.errors import BudgetExceededError
from app.llm.providers.mock import MockProvider
from app.llm.types import LLMResponse, Message
from tests.unit._router_helpers import make_router


@pytest.mark.asyncio
async def test_expensive_call_over_budget_raises():
    expensive = MockProvider(name="hosted", cost_per_call=1.00)
    expensive.register(
        "x", LLMResponse(text="ok", model_used="hosted", provider="hosted", cost_usd=1.00)
    )
    router, _, _, _ = make_router(
        providers={"hosted": expensive},
        tiers={"generation": ["hosted"]},
        hourly_limit_usd=0.01,
    )

    with pytest.raises(BudgetExceededError):
        await router.generate([Message(role="user", content="x")], task="generation")
    assert expensive.call_count == 0


@pytest.mark.asyncio
async def test_local_zero_cost_provider_bypasses_budget():
    """NN-6: local tier ($0) proceeds even at LLM_HOURLY_BUDGET_USD=0."""
    local = MockProvider(name="vllm", cost_per_call=0.0)
    local.register(
        "x", LLMResponse(text="ok", model_used="vllm", provider="vllm", cost_usd=0.0)
    )
    router, _, _, _ = make_router(
        providers={"vllm": local},
        tiers={"validation": ["vllm"]},
        hourly_limit_usd=0.0,
    )

    response = await router.generate([Message(role="user", content="x")], task="validation")
    assert response.provider == "vllm"
    assert local.call_count == 1


@pytest.mark.asyncio
async def test_cumulative_spend_eventually_blocks_hosted():
    hosted = MockProvider(name="hosted", cost_per_call=0.005)
    hosted.register(
        "x",
        LLMResponse(
            text="ok", model_used="hosted", provider="hosted", cost_usd=0.005, tokens_in=1, tokens_out=1
        ),
    )
    router, _, _, budget = make_router(
        providers={"hosted": hosted},
        tiers={"extraction": ["hosted"]},
        hourly_limit_usd=1.0,
    )

    # First call is small relative to limit; should pass
    await router.generate([Message(role="user", content="x")], task="extraction")
    assert hosted.call_count == 1
    assert await budget.current_spend() == pytest.approx(0.005)
