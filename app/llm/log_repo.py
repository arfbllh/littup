from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker
from uuid_extensions import uuid7

from app.db.models.llm_log import LLMRequest

log = structlog.get_logger(__name__)


class LLMLogRepo:
    """Fire-and-forget logger for llm_log.llm_requests.

    `record()` returns immediately; the DB write happens on the event loop.
    Failures are logged but not raised — logging must never break a call.
    """

    def __init__(self, session_factory: async_sessionmaker | None):
        self._sf = session_factory
        self._tasks: set[asyncio.Task] = set()

    def record(
        self,
        *,
        trace_id: str | None,
        tier: str | None,
        provider: str | None,
        model: str | None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        status: str = "ok",
        error_code: str | None = None,
        cache_hit: bool = False,
        cache_key: str | None = None,
        prompt_fingerprint: str | None = None,
    ) -> None:
        if self._sf is None:
            return
        task = asyncio.create_task(
            self._write(
                trace_id=trace_id,
                tier=tier,
                provider=provider,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                status=status,
                error_code=error_code,
                cache_hit=cache_hit,
                cache_key=cache_key,
                prompt_fingerprint=prompt_fingerprint,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, **kwargs) -> None:
        try:
            async with self._sf() as session:
                row = LLMRequest(
                    id=str(uuid7()),
                    created_at=datetime.now(timezone.utc),
                    **kwargs,
                )
                session.add(row)
                await session.commit()
        except Exception as e:
            log.warning("llm_log.write_failed", error=str(e))

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
