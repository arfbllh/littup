"""CHUNKING job handler — produces app.chunks rows then enqueues embedding."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IngestError
from app.ingest.service import IngestService
from app.jobs.kinds import HANDLERS, JobKind

logger = structlog.get_logger(__name__)


async def handle_chunking(payload: dict, session: AsyncSession) -> dict:
    doc_id = payload.get("document_id")
    if not doc_id:
        raise IngestError("CHUNKING job missing document_id", code="CHUNKING_BAD_PAYLOAD")

    service = IngestService(session)
    try:
        result = await service.chunk_document(doc_id)
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(str(exc), code="CHUNKING_UNEXPECTED_ERROR") from exc

    await service.queue.enqueue(
        kind=JobKind.EMBEDDING.value,
        payload={"document_id": doc_id},
        dedup_key=f"embedding:{doc_id}",
    )
    logger.info("embedding_job_enqueued", document_id=doc_id)
    return result


HANDLERS[JobKind.CHUNKING.value] = handle_chunking
