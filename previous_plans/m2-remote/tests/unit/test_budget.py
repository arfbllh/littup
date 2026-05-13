from datetime import UTC, datetime, timedelta

import pytest

from app.llm.budget import BudgetTracker


@pytest.mark.asyncio
async def test_zero_estimate_always_passes():
    b = BudgetTracker(hourly_limit_usd=0.0)
    assert await b.check(0.0) is True


@pytest.mark.asyncio
async def test_under_budget_passes():
    b = BudgetTracker(hourly_limit_usd=1.00)
    assert await b.check(0.50) is True


@pytest.mark.asyncio
async def test_over_budget_blocks():
    b = BudgetTracker(hourly_limit_usd=0.01)
    await b.add("p", 0.005)
    assert await b.check(0.01) is False


@pytest.mark.asyncio
async def test_at_threshold_passes():
    b = BudgetTracker(hourly_limit_usd=1.0)
    await b.add("p", 0.5)
    assert await b.check(0.5) is True
    assert await b.check(0.51) is False


@pytest.mark.asyncio
async def test_sliding_window_expires_old_entries():
    b = BudgetTracker(hourly_limit_usd=1.0, window_seconds=3600)
    old = datetime.now(UTC) - timedelta(hours=2)
    await b.add("p", 0.99, ts=old)
    assert await b.current_spend() == 0.0
    assert await b.check(0.5) is True


@pytest.mark.asyncio
async def test_local_zero_cost_calls_never_consume_budget():
    """NN-6: local-tier ($0) calls bypass budget."""
    b = BudgetTracker(hourly_limit_usd=0.0)
    await b.add("vllm", 0.0)
    assert await b.current_spend() == 0.0
    assert await b.check(0.0) is True


@pytest.mark.asyncio
async def test_remaining_clamped_at_zero():
    b = BudgetTracker(hourly_limit_usd=1.0)
    await b.add("p", 5.0)
    assert await b.remaining() == 0.0
