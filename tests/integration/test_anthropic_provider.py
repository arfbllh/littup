"""
Real Anthropic API integration tests.
Requires ANTHROPIC_API_KEY in environment or .env.
Skipped automatically if key is absent.
"""
import os

import pytest
from dotenv import load_dotenv

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)


from app.llm.providers.anthropic import AnthropicProvider
from app.llm.types import Message, SamplingParams


@pytest.fixture
def provider():
    return AnthropicProvider(
        name="test-anthropic",
        model="claude-haiku-4-5-20251001",
        input_cost_per_1k=0.0008,
        output_cost_per_1k=0.004,
    )


@pytest.mark.asyncio
async def test_plain_text_response(provider):
    resp = await provider.generate(
        [Message(role="user", content="Reply with exactly: hello")],
        sampling=SamplingParams(max_tokens=10, temperature=0.0),
    )
    assert resp.provider == "test-anthropic"
    assert resp.model_used == "claude-haiku-4-5-20251001"
    assert "hello" in resp.text.lower()
    assert resp.tokens_in > 0
    assert resp.tokens_out > 0
    assert resp.cost_usd > 0.0
    assert resp.latency_ms > 0
    assert resp.finish_reason == "end_turn"


@pytest.mark.asyncio
async def test_system_prompt_is_respected(provider):
    resp = await provider.generate(
        [
            Message(role="system", content="You only respond in ALL CAPS."),
            Message(role="user", content="say yes"),
        ],
        sampling=SamplingParams(max_tokens=10, temperature=0.0),
    )
    assert resp.text == resp.text.upper()


@pytest.mark.asyncio
async def test_structured_output_via_tool_use(provider):
    schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "country": {"type": "string"},
        },
        "required": ["city", "country"],
    }
    resp = await provider.generate(
        [Message(role="user", content="What city is the Eiffel Tower in?")],
        schema=schema,
        sampling=SamplingParams(max_tokens=64, temperature=0.0),
    )
    assert resp.structured is not None
    assert resp.structured.get("city", "").lower() == "paris"
    assert resp.structured.get("country", "").lower() in ("france", "fr")


@pytest.mark.asyncio
async def test_health_returns_true_when_key_present(provider):
    assert await provider.health() is True


@pytest.mark.asyncio
async def test_health_returns_false_when_key_missing():
    p = AnthropicProvider(name="no-key", model="claude-haiku-4-5-20251001", api_key_env="__MISSING_KEY__")
    assert await p.health() is False
