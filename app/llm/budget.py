from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.llm_log import LLMRequest


class BudgetTracker:
    """Sliding 1-hour USD budget. Protects against runaway hosted spend (NN-6)."""

    def __init__(self, hourly_usd: float, session_factory: async_sessionmaker | None = None):
        self.hourly_usd = hourly_usd
        self._sf = session_factory
        self._events: deque[tuple[datetime, float]] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=1)
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def current_spend(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        self._prune(now)
        return sum(c for _, c in self._events)

    def check(self, estimate_usd: float, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.current_spend(now) + estimate_usd <= self.hourly_usd

    async def add(self, cost_usd: float, ts: datetime | None = None) -> None:
        async with self._lock:
            self._events.append((ts or datetime.now(timezone.utc), cost_usd))
            self._prune(datetime.now(timezone.utc))

    async def prime(self) -> None:
        """Reload the last hour of spend from llm_log.llm_requests at startup."""
        if self._sf is None:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(LLMRequest.created_at, LLMRequest.cost_usd).where(
                        LLMRequest.created_at >= cutoff
                    )
                )
            ).all()
        async with self._lock:
            self._events.clear()
            for ts, cost in rows:
                if cost is None:
                    continue
                self._events.append((ts, float(cost)))
