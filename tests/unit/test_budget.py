from datetime import datetime, timedelta, timezone

import pytest

from app.llm.budget import BudgetTracker


@pytest.mark.asyncio
async def test_check_blocks_above_threshold():
    b = BudgetTracker(hourly_usd=1.0)
    await b.add(0.5)
    assert b.check(0.4) is True
    assert b.check(0.6) is False


@pytest.mark.asyncio
async def test_sliding_window_expires_old_events():
    b = BudgetTracker(hourly_usd=1.0)
    now = datetime.now(timezone.utc)
    # Push two events: one ~70 minutes old, one fresh.
    b._events.append((now - timedelta(minutes=70), 0.9))
    b._events.append((now, 0.05))
    assert b.current_spend(now) == pytest.approx(0.05)
    assert b.check(0.9, now) is True  # because old event was pruned


@pytest.mark.asyncio
async def test_zero_budget_blocks_any_positive_estimate():
    b = BudgetTracker(hourly_usd=0.0)
    assert b.check(0.0) is True
    assert b.check(0.0001) is False
