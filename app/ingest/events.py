"""Document event bus — increments app.documents.last_event_seq atomically
with a row in app.document_events for SSE replay (NN-10)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class DocumentEventRow:
    seq: int
    type: str
    payload: dict[str, Any]
    ts: datetime


class DocumentEventBus:
    """All writes happen inside the caller's transaction; commit is the caller's job."""

    @staticmethod
    async def emit(
        session: AsyncSession,
        document_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        result = await session.execute(
            text(
                """
                UPDATE app.documents
                SET last_event_seq = last_event_seq + 1,
                    updated_at     = NOW()
                WHERE id = :doc_id
                RETURNING last_event_seq
                """
            ),
            {"doc_id": document_id},
        )
        row = result.fetchone()
        if row is None:
            raise LookupError(f"document {document_id} not found")
        seq = int(row.last_event_seq)
        await session.execute(
            text(
                """
                INSERT INTO app.document_events (document_id, seq, type, payload)
                VALUES (:doc_id, :seq, :type, CAST(:payload AS jsonb))
                """
            ),
            {
                "doc_id": document_id,
                "seq": seq,
                "type": event_type,
                "payload": json.dumps(payload or {}),
            },
        )
        return seq

    @staticmethod
    async def replay(
        session: AsyncSession,
        document_id: str,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[DocumentEventRow]:
        result = await session.execute(
            text(
                """
                SELECT seq, type, payload, ts
                FROM app.document_events
                WHERE document_id = :doc_id AND seq > :after
                ORDER BY seq ASC
                LIMIT :limit
                """
            ),
            {"doc_id": document_id, "after": after_seq, "limit": limit},
        )
        rows = result.fetchall()
        return [
            DocumentEventRow(seq=int(r.seq), type=r.type, payload=r.payload or {}, ts=r.ts)
            for r in rows
        ]
