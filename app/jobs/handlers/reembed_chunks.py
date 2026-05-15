"""REEMBED_CHUNKS handler — re-embeds chunks after a block-text edit (WS-E).

The block-edit cascade (``app/ingest/block_edits.py``) NULLs ``chunks.embedding``
and enqueues this job with the affected chunk IDs. Re-embedding is local —
BGE-large stays on the local embedder; no LLM-tier spend (Inv #6).
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_embedder
from app.core.errors import IngestError
from app.ingest.events import DocumentEventBus
from app.jobs.kinds import HANDLERS, JobKind
from app.settings import settings

logger = structlog.get_logger(__name__)


async def handle_reembed_chunks(payload: dict, session: AsyncSession) -> dict:
    chunk_ids = payload.get("chunk_ids") or []
    document_id = payload.get("document_id")
    if not chunk_ids:
        return {"reembedded": 0, "skipped": True, "reason": "empty_chunk_ids"}

    embedder = await get_embedder()

    result = await session.execute(
        text(
            """
            SELECT id::text AS id, text FROM app.chunks
             WHERE id::text = ANY(:ids)
             ORDER BY id
            """
        ),
        {"ids": [str(c) for c in chunk_ids]},
    )
    rows = result.fetchall()
    if not rows:
        return {"reembedded": 0, "skipped": True, "reason": "no_matching_chunks"}

    batch_size = settings.EMBEDDING_BATCH_SIZE
    embedded = 0
    try:
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            vectors = await embedder.embed([r.text for r in batch])
            for row, vec in zip(batch, vectors, strict=True):
                vec_str = "[" + ",".join(str(f) for f in vec) + "]"
                await session.execute(
                    text(
                        "UPDATE app.chunks SET embedding = CAST(:v AS vector) "
                        "WHERE id = :id"
                    ),
                    {"v": vec_str, "id": row.id},
                )
            await session.commit()
            embedded += len(batch)
    except Exception as exc:
        # WS-E.3: emit a `reembed_failed` event so the UI can surface a banner
        # instead of leaving the operator wondering why citations are forever
        # stale. The job runner will retry up to its bounded budget; this event
        # is informational on this particular attempt.
        logger.warning(
            "reembed_chunks_failed",
            document_id=document_id,
            chunk_ids=chunk_ids,
            error=str(exc),
        )
        if document_id:
            await DocumentEventBus.emit(
                session,
                document_id,
                "reembed_failed",
                {"chunk_count": len(rows), "error": str(exc)[:200]},
            )
            await session.commit()
        raise IngestError(str(exc), code="REEMBED_ERROR") from exc

    logger.info(
        "reembed_chunks_done",
        document_id=document_id,
        chunk_count=embedded,
    )
    return {"reembedded": embedded, "document_id": document_id}


HANDLERS[JobKind.REEMBED_CHUNKS.value] = handle_reembed_chunks
