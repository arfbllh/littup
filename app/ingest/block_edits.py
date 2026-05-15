"""Block-edit cascade — WS-E.

Operator edits a block's transcript text. We:
  1. Update ``app.blocks.text`` for the edited block.
  2. Rebuild ``app.chunks.text`` for every chunk whose ``block_ids[]`` contains
     the block — concatenating the current block texts in order so the chunk
     stays internally consistent with its source spans.
  3. Mark every citation that references those chunks as
     ``validation_status='stale'`` so the draft UI can flag them.
  4. Enqueue a single ``reembed_chunks`` job covering the affected chunk IDs;
     the chunk embeddings change because the chunk text changed.

The PATCH route returns fast; the embedder runs out of band.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IngestError
from app.ingest.events import DocumentEventBus
from app.jobs.kinds import JobKind
from app.jobs.queue import JobQueue

logger = structlog.get_logger(__name__)


async def edit_block_text(
    *,
    document_id: str,
    block_id: str,
    new_text: str,
    session: AsyncSession,
    queue: JobQueue | None = None,
) -> dict:
    """Apply a block-text edit and cascade through chunks + citations."""
    if not new_text or not new_text.strip():
        raise IngestError("Block text cannot be empty", code="BLOCK_TEXT_EMPTY", retryable=False)

    queue = queue or JobQueue(session)

    doc_row = await session.execute(
        text(
            "SELECT id, status FROM app.documents WHERE id = :id FOR UPDATE"
        ),
        {"id": document_id},
    )
    doc = doc_row.fetchone()
    if doc is None:
        raise IngestError(f"Document {document_id} not found", code="DOCUMENT_NOT_FOUND")
    if doc.status != "ready":
        raise IngestError(
            f"Block edits only allowed on ready documents (status={doc.status})",
            code="DOCUMENT_NOT_READY",
            retryable=False,
        )

    block_row = await session.execute(
        text(
            """
            SELECT id, text FROM app.blocks
             WHERE id = :id AND document_id = :doc_id
             FOR UPDATE
            """
        ),
        {"id": block_id, "doc_id": document_id},
    )
    block = block_row.fetchone()
    if block is None:
        raise IngestError(f"Block {block_id} not found", code="BLOCK_NOT_FOUND")

    old_text = block.text or ""
    if old_text == new_text:
        return {
            "block_id": block_id,
            "affected_chunks": 0,
            "stale_citations": 0,
            "no_change": True,
        }

    await session.execute(
        text("UPDATE app.blocks SET text = :t WHERE id = :id"),
        {"t": new_text, "id": block_id},
    )

    # Find affected chunks. ``block_ids`` is a TEXT[] of stringified block IDs.
    affected_rows = await session.execute(
        text(
            """
            SELECT id, block_ids
              FROM app.chunks
             WHERE document_id = :doc_id
               AND :block_id = ANY(block_ids)
             ORDER BY id
             FOR UPDATE
            """
        ),
        {"doc_id": document_id, "block_id": block_id},
    )
    affected = affected_rows.fetchall()

    affected_chunk_ids: list[str] = []
    for chunk in affected:
        block_ids = list(chunk.block_ids or [])
        if not block_ids:
            continue
        # Rebuild chunk.text by concatenating current block texts in declared
        # order. We tolerate missing blocks — a deleted block contributes nothing.
        texts_row = await session.execute(
            text(
                """
                SELECT id::text AS id, text
                  FROM app.blocks
                 WHERE id::text = ANY(:ids)
                """
            ),
            {"ids": block_ids},
        )
        by_id = {r.id: r.text or "" for r in texts_row.fetchall()}
        new_chunk_text = "\n".join(by_id.get(bid, "") for bid in block_ids).strip()
        await session.execute(
            text("UPDATE app.chunks SET text = :t, embedding = NULL WHERE id = :id"),
            {"t": new_chunk_text, "id": chunk.id},
        )
        affected_chunk_ids.append(str(chunk.id))

    stale_citations = 0
    if affected_chunk_ids:
        result = await session.execute(
            text(
                """
                UPDATE app.citations
                   SET validation_status = 'stale'
                 WHERE chunk_id::text = ANY(:ids)
                """
            ),
            {"ids": affected_chunk_ids},
        )
        stale_citations = int(result.rowcount or 0)

        await queue.enqueue(
            kind=JobKind.REEMBED_CHUNKS.value,
            payload={"document_id": document_id, "chunk_ids": affected_chunk_ids},
        )

    await DocumentEventBus.emit(
        session,
        document_id,
        "block_edited",
        {
            "block_id": block_id,
            "old_text_len": len(old_text),
            "new_text_len": len(new_text),
            "affected_chunks": len(affected_chunk_ids),
            "stale_citations": stale_citations,
        },
    )
    await session.commit()

    logger.info(
        "block_edited",
        document_id=document_id,
        block_id=block_id,
        affected_chunks=len(affected_chunk_ids),
        stale_citations=stale_citations,
    )
    return {
        "block_id": block_id,
        "affected_chunks": len(affected_chunk_ids),
        "stale_citations": stale_citations,
    }
