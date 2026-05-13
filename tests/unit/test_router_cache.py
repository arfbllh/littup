import pytest

from app.llm.providers.mock import MockProvider
from app.llm.types import LLMResponse, Message
from tests.unit._router_helpers import make_router


@pytest.mark.asyncio
async def test_second_identical_call_hits_cache():
    provider = MockProvider(name="p", cost_per_call=0.0)
    provider.register(
        "hello",
        LLMResponse(text="world", model_used="p", provider="p", cost_usd=0.0),
    )

    router, cache, _, _ = make_router(
        providers={"p": provider},
        tiers={"extraction": ["p"]},
    )

    messages = [Message(role="user", content="hello")]

    r1 = await router.generate(messages, task="extraction")
    assert r1.text == "world"
    assert r1.cache_hit is False
    assert provider.call_count == 1

    r2 = await router.generate(messages, task="extraction")
    assert r2.text == "world"
    assert r2.cache_hit is True
    assert provider.call_count == 1  # cache hit — provider not called again
    assert len(cache.store) == 1


@pytest.mark.asyncio
async def test_cache_disabled_skips_cache():
    provider = MockProvider(name="p", cost_per_call=0.0)
    provider.register(
        "hello",
        LLMResponse(text="world", model_used="p", provider="p", cost_usd=0.0),
    )

    router, _, _, _ = make_router(
        providers={"p": provider},
        tiers={"extraction": ["p"]},
        cache_enabled=False,
    )
    messages = [Message(role="user", content="hello")]

    await router.generate(messages, task="extraction")
    await router.generate(messages, task="extraction")
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_cache_hit_logged_with_cache_hit_true():
    provider = MockProvider(name="p", cost_per_call=0.0)
    provider.register(
        "hello",
        LLMResponse(text="world", model_used="p", provider="p", cost_usd=0.0),
    )
    router, _, log_repo, _ = make_router(
        providers={"p": provider},
        tiers={"extraction": ["p"]},
    )
    messages = [Message(role="user", content="hello")]
    await router.generate(messages, task="extraction")
    await router.generate(messages, task="extraction")

    assert len(log_repo.records) == 2
    assert log_repo.records[0]["cache_hit"] is False
    assert log_repo.records[1]["cache_hit"] is True
