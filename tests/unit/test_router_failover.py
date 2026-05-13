import pytest

from app.core.errors import LLMUnavailableError
from app.llm.providers.mock import MockProvider
from app.llm.types import LLMResponse, Message
from tests.unit._router_helpers import make_router


@pytest.mark.asyncio
async def test_primary_fails_secondary_succeeds():
    primary = MockProvider(name="primary", fail=True)
    secondary = MockProvider(name="secondary", cost_per_call=0.0)
    secondary.register(
        "claim",
        LLMResponse(text="ok", model_used="secondary", provider="secondary", cost_usd=0.0),
    )

    router, _, _, _ = make_router(
        providers={"primary": primary, "secondary": secondary},
        tiers={"extraction": ["primary", "secondary"]},
    )

    response = await router.generate(
        [Message(role="user", content="check this claim")],
        task="extraction",
    )

    assert response.provider == "secondary"
    assert response.text == "ok"
    assert primary.call_count == 0  # primary raised before incrementing
    assert secondary.call_count == 1


@pytest.mark.asyncio
async def test_all_providers_fail_raises_llm_unavailable():
    a = MockProvider(name="a", fail=True)
    b = MockProvider(name="b", fail=True)

    router, _, _, _ = make_router(
        providers={"a": a, "b": b},
        tiers={"extraction": ["a", "b"]},
    )

    with pytest.raises(LLMUnavailableError):
        await router.generate([Message(role="user", content="x")], task="extraction")


@pytest.mark.asyncio
async def test_unknown_tier_raises_llm_unavailable():
    router, _, _, _ = make_router(
        providers={"p": MockProvider(name="p")},
        tiers={"extraction": ["p"]},
    )
    with pytest.raises(LLMUnavailableError):
        await router.generate([Message(role="user", content="x")], task="generation")
