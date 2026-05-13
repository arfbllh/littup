"""SSE helpers: minimal framing without external deps (we ship sse-starlette
but want full control over Last-Event-ID + keepalive semantics)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ingest.events import DocumentEventBus
from app.settings import settings

logger = structlog.get_logger(__name__)


@dataclass
class _Frame:
    seq: int | None
    event: str | None
    data: str  # already JSON-encoded payload, or "" for keepalive


def parse_last_event_id(value: str | None) -> int:
    if not value:
        return 0
    try:
        n = int(value)
        return max(0, n)
    except (TypeError, ValueError):
        return 0


def _format(frame: _Frame) -> bytes:
    if frame.event is None and frame.seq is None and frame.data == "":
        return b": keepalive\n\n"
    lines: list[str] = []
    if frame.seq is not None:
        lines.append(f"id: {frame.seq}")
    if frame.event is not None:
        lines.append(f"event: {frame.event}")
    for data_line in (frame.data or "").splitlines() or [""]:
        lines.append(f"data: {data_line}")
    lines.append("")
    lines.append("")
    return ("\n".join(lines)).encode("utf-8")


async def stream_document_events(
    request: Request,
    document_id: str,
    last_event_id: int,
    *,
    session_factory: async_sessionmaker | None = None,
    keepalive: float | None = None,
    poll_interval: float | None = None,
    max_seconds: float | None = None,
) -> AsyncIterator[bytes]:
    """Yield SSE byte frames for a document's event stream.

    Replays everything with seq > last_event_id, then polls the
    document_events table on a short interval (pgbouncer transaction
    pooling makes LISTEN/NOTIFY unreliable, so polling is the
    pragmatic choice).
    """
    if session_factory is None:
        # Resolve lazily so monkeypatching app.db.session.async_session_factory
        # in tests is honored.
        from app.db import session as _session_mod
        session_factory = _session_mod.async_session_factory
    keepalive = keepalive if keepalive is not None else settings.SSE_KEEPALIVE_SECONDS
    poll_interval = (
        poll_interval if poll_interval is not None else settings.SSE_POLL_INTERVAL_SECONDS
    )
    max_seconds = max_seconds if max_seconds is not None else settings.SSE_MAX_STREAM_SECONDS

    cursor = last_event_id
    started = time.monotonic()
    last_keepalive = started

    # Initial replay
    async with session_factory() as session:
        rows = await DocumentEventBus.replay(session, document_id, cursor)
    for row in rows:
        cursor = row.seq
        payload: dict[str, Any] = {"seq": row.seq, "type": row.type, "payload": row.payload}
        yield _format(_Frame(seq=row.seq, event=row.type, data=json.dumps(payload)))

    while True:
        if await request.is_disconnected():
            break
        if time.monotonic() - started > max_seconds:
            break

        await asyncio.sleep(poll_interval)

        try:
            async with session_factory() as session:
                rows = await DocumentEventBus.replay(session, document_id, cursor)
        except Exception as exc:
            logger.warning("sse_poll_error", document_id=document_id, error=str(exc))
            rows = []

        for row in rows:
            cursor = row.seq
            payload = {"seq": row.seq, "type": row.type, "payload": row.payload}
            yield _format(_Frame(seq=row.seq, event=row.type, data=json.dumps(payload)))
            last_keepalive = time.monotonic()

        if time.monotonic() - last_keepalive >= keepalive:
            yield _format(_Frame(seq=None, event=None, data=""))
            last_keepalive = time.monotonic()
