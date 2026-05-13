from datetime import datetime, timedelta, timezone

import pytest

from app.llm.budget import BudgetTracker


@pytest.mark.asyncio
async def test_check_and_reserve_blocks_above_threshold():
    b = BudgetTracker(hourly_usd=1.0)
    await b.add(0.5)
    assert await b.check_and_reserve(0.4) is True   # 0.5 + 0.4 = 0.9 ≤ 1.0
    # Now at 0.9; next reservation of 0.2 would push to 1.1 — blocked.
    assert await b.check_and_reserve(0.2) is False


@pytest.mark.asyncio
async def test_sliding_window_expires_old_events():
    b = BudgetTracker(hourly_usd=1.0)
    now = datetime.now(timezone.utc)
    # Push two events: one ~70 minutes old (expired), one fresh.
    b._events.append((now - timedelta(minutes=70), 0.9))
    b._events.append((now, 0.05))
    assert b.current_spend(now) == pytest.approx(0.05)
    # Old event pruned → 0.05 + 0.9 = 0.95 ≤ 1.0, should pass.
    assert await b.check_and_reserve(0.9, now) is True


@pytest.mark.asyncio
async def test_zero_budget_blocks_any_positive_estimate():
    b = BudgetTracker(hourly_usd=0.0)
    assert await b.check_and_reserve(0.0) is True
    assert await b.check_and_reserve(0.0001) is False


@pytest.mark.asyncio
async def test_concurrent_reservations_are_atomic():
    """Two concurrent check_and_reserve calls must not both succeed when combined they exceed budget."""
    import asyncio

    b = BudgetTracker(hourly_usd=1.0)
    results = await asyncio.gather(
        b.check_and_reserve(0.7),
        b.check_and_reserve(0.7),
    )
    # Only one should succeed; combined would be 1.4 > 1.0.
    assert results.count(True) == 1
    assert results.count(False) == 1


@pytest.mark.asyncio
async def test_replace_reservation_swaps_estimate_with_actual():
    b = BudgetTracker(hourly_usd=5.0)
    await b.check_and_reserve(1.0)
    spend_after_reserve = b.current_spend()
    assert spend_after_reserve == pytest.approx(1.0)

    await b.replace_reservation(1.0, 0.3)
    assert b.current_spend() == pytest.approx(0.3)
