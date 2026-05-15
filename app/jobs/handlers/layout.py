"""LAYOUT job handler — parses document blocks then enqueues chunking."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IngestError
from app.ingest.service import IngestService
from app.jobs.kinds import HANDLERS, JobKind

logger = structlog.get_logger(__name__)


async def handle_layout(payload: dict, session: AsyncSession) -> dict:
    doc_id = payload.get("document_id")
    if not doc_id:
        raise IngestError("LAYOUT job payload missing document_id", code="LAYOUT_BAD_PAYLOAD")

    service = IngestService(session)
    try:
        result = await service.parse_layout(doc_id)
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(str(exc), code="LAYOUT_UNEXPECTED_ERROR") from exc

    # Enqueue next stage — dedup_key prevents double-enqueue
    await service.queue.enqueue(
        kind=JobKind.CHUNKING.value,
        payload={"document_id": doc_id},
        dedup_key=f"chunking:{doc_id}",
    )
    logger.info("chunking_job_enqueued", document_id=doc_id)
    return result


HANDLERS[JobKind.LAYOUT.value] = handle_layout
