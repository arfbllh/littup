"""Live smoke test against Anthropic. Skipped unless `pytest --live` is passed.

Costs ~$0.00001 per run (≤20 output tokens on claude-haiku-4-5, $4/Mtok).
Reads ANTHROPIC_API_KEY from .env via app.settings.
"""
import pytest

from app.llm.providers.anthropic import AnthropicProvider
from app.llm.types import Message, SamplingParams
from app.settings import settings


@pytest.mark.live
@pytest.mark.asyncio
async def test_anthropic_haiku_real_call():
    if not settings.ANTHROPIC_API_KEY:
        pytest.skip("ANTHROPIC_API_KEY not set in .env")

    provider = AnthropicProvider(
        name="anthropic_haiku_live",
        model="claude-haiku-4-5",
        api_key=settings.ANTHROPIC_API_KEY,
    )

    response = await provider.generate(
        [Message(role="user", content="Reply with exactly the word: pong")],
        sampling=SamplingParams(max_tokens=10, temperature=0.0),
    )

    assert response.provider == "anthropic_haiku_live"
    assert response.tokens_in > 0
    assert response.tokens_out > 0
    assert response.cost_usd > 0
    assert "claude-haiku" in response.model_used
    assert response.cost_usd < 0.001
    assert response.text  # non-empty
