"""Real OCR job handler — replaces ocr_stub.py."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IngestError
from app.ingest.service import IngestService
from app.jobs.kinds import HANDLERS, JobKind
from app.llm.deps import get_llm_router

logger = structlog.get_logger(__name__)


async def handle_ocr(payload: dict, session: AsyncSession) -> dict:
    doc_id = payload.get("document_id")
    if not doc_id:
        raise IngestError("OCR job payload missing document_id", code="OCR_BAD_PAYLOAD")

    # Obtain the shared LLM router so the VLM escalation path is live (B-3)
    llm_router = await get_llm_router()

    service = IngestService(session)
    try:
        result = await service.ocr_document(doc_id, llm_router=llm_router)
    except IngestError as exc:
        if exc.code in ("FILE_NOT_FOUND", "RASTERISE_ERROR", "IMAGE_READ_ERROR"):
            exc.retryable = False
        raise
    except Exception as exc:
        raise IngestError(str(exc), code="OCR_UNEXPECTED_ERROR") from exc

    # Always enqueue layout — dedup_key prevents double-enqueue (NN-1)
    await service.queue.enqueue(
        kind=JobKind.LAYOUT.value,
        payload={"document_id": doc_id},
        dedup_key=f"layout:{doc_id}",
    )
    logger.info("layout_job_enqueued", document_id=doc_id)
    return result


HANDLERS[JobKind.OCR.value] = handle_ocr
