from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from app.db.models.llm_log import LLMRequest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


class BudgetTracker:
    """NN-6: sliding-window spend tracker. Thread-safe via asyncio.Lock."""

    def __init__(self, hourly_limit_usd: float, window_seconds: int = 3600) -> None:
        self._limit = hourly_limit_usd
        self._window = timedelta(seconds=window_seconds)
        self._entries: deque[tuple[float, datetime]] = deque()
        self._lock = asyncio.Lock()

    async def reload_from_db(self, session: AsyncSession) -> None:
        from sqlalchemy import select

        cutoff = datetime.now(UTC) - self._window
        result = await session.execute(
            select(LLMRequest.cost_usd, LLMRequest.created_at).where(
                LLMRequest.created_at >= cutoff,
                LLMRequest.cost_usd.isnot(None),
            )
        )
        async with self._lock:
            self._entries.clear()
            for cost, ts in result:
                if cost and float(cost) > 0:
                    aware = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
                    self._entries.append((float(cost), aware))
        log.info(
            "budget.reloaded",
            entries=len(self._entries),
            current_spend=self._spend_unsafe(),
        )

    def _prune(self) -> None:
        cutoff = datetime.now(UTC) - self._window
        while self._entries and self._entries[0][1] < cutoff:
            self._entries.popleft()

    def _spend_unsafe(self) -> float:
        self._prune()
        return sum(c for c, _ in self._entries)

    async def current_spend(self) -> float:
        async with self._lock:
            return self._spend_unsafe()

    async def remaining(self) -> float:
        async with self._lock:
            return max(0.0, self._limit - self._spend_unsafe())

    async def add(self, provider: str, cost_usd: float, ts: datetime | None = None) -> None:
        if cost_usd <= 0:
            return
        async with self._lock:
            self._entries.append((cost_usd, ts or datetime.now(UTC)))

    async def check(self, estimate_usd: float) -> bool:
        """Return True if the call is within budget. Local ($0) calls always pass."""
        if estimate_usd <= 0:
            return True
        async with self._lock:
            return self._spend_unsafe() + estimate_usd <= self._limit
