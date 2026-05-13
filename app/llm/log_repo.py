from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import text

from app.core.ids import new_uuid7

log = structlog.get_logger(__name__)


@dataclass
class _LogEntry:
    tier: str
    provider: str
    model: str
    status: str
    trace_id: str | None = None
    prompt_fingerprint: str | None = None
    cache_key: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error_code: str | None = None
    cache_hit: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


_INSERT = text(
    """
    INSERT INTO llm_log.llm_requests
      (id, trace_id, tier, provider, model, prompt_fingerprint, cache_key,
       tokens_in, tokens_out, cost_usd, latency_ms, status, error_code, cache_hit, created_at)
    VALUES
      (:id, :trace_id, :tier, :provider, :model, :prompt_fingerprint, :cache_key,
       :tokens_in, :tokens_out, :cost_usd, :latency_ms, :status, :error_code, :cache_hit, :created_at)
    """
)


class LLMLogRepo:
    """NN-12: fire-and-forget async queue. record() never blocks the caller."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self._queue: asyncio.Queue[_LogEntry] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain(), name="llm-log-drain")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def record(
        self,
        *,
        tier: str,
        provider: str,
        model: str,
        status: str = "ok",
        trace_id: str | None = None,
        prompt_fingerprint: str | None = None,
        cache_key: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        error_code: str | None = None,
        cache_hit: bool = False,
    ) -> None:
        entry = _LogEntry(
            trace_id=trace_id,
            tier=tier,
            provider=provider,
            model=model,
            prompt_fingerprint=prompt_fingerprint,
            cache_key=cache_key,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            error_code=error_code,
            cache_hit=cache_hit,
        )
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            log.warning("llm_log.queue_full", dropped=True)

    async def _drain(self) -> None:
        while True:
            entry = await self._queue.get()
            try:
                await self._write(entry)
            except Exception as exc:
                log.error("llm_log.write_failed", error=str(exc))
            finally:
                self._queue.task_done()

    async def _write(self, e: _LogEntry) -> None:
        async with self._session_factory() as session:
            await session.execute(
                _INSERT,
                {
                    "id": new_uuid7(),
                    "trace_id": e.trace_id,
                    "tier": e.tier,
                    "provider": e.provider,
                    "model": e.model,
                    "prompt_fingerprint": e.prompt_fingerprint,
                    "cache_key": e.cache_key,
                    "tokens_in": e.tokens_in,
                    "tokens_out": e.tokens_out,
                    "cost_usd": e.cost_usd,
                    "latency_ms": e.latency_ms,
                    "status": e.status,
                    "error_code": e.error_code,
                    "cache_hit": e.cache_hit,
                    "created_at": e.created_at,
                },
            )
            await session.commit()
