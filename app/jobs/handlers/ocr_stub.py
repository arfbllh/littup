"""Stub OCR handler — placeholder until M4 lands the real pipeline.

It walks the document through the visible state transitions and then
marks it `failed` with a specific error_code so the end-to-end SSE/
status path can be verified without actual OCR work."""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IngestError
from app.ingest.events import DocumentEventBus
from app.jobs.kinds import HANDLERS, JobKind

logger = structlog.get_logger(__name__)

_ERROR_CODE = "OCR_HANDLER_NOT_IMPLEMENTED"


async def _set_status(session: AsyncSession, doc_id: str, new_status: str) -> None:
    await session.execute(
        text(
            "UPDATE app.documents SET status = :status, updated_at = NOW() WHERE id = :id"
        ),
        {"id": doc_id, "status": new_status},
    )


async def _fail_document(session: AsyncSession, doc_id: str, code: str, message: str) -> None:
    await session.execute(
        text(
            """
            UPDATE app.documents
            SET status = 'failed',
                error_code = :code,
                error_message = :msg,
                updated_at = NOW()
            WHERE id = :id
            """
        ),
        {"id": doc_id, "code": code, "msg": message},
    )


async def handle_ocr(payload: dict, session: AsyncSession) -> dict:
    doc_id = payload.get("document_id")
    if not doc_id:
        raise IngestError("OCR job payload missing document_id", code="OCR_BAD_PAYLOAD")

    # uploaded → ocr_pending → ocr_running → failed
    prev = await session.execute(
        text("SELECT status FROM app.documents WHERE id = :id"), {"id": doc_id}
    )
    prev_status = prev.scalar_one_or_none()
    if prev_status is None:
        raise IngestError(
            f"Document {doc_id} not found", code="DOCUMENT_NOT_FOUND"
        )

    # Idempotency: a retried run on an already-failed doc just exits cleanly.
    if prev_status == "failed":
        return {"already_failed": True}

    for next_status in ("ocr_pending", "ocr_running"):
        await _set_status(session, doc_id, next_status)
        await DocumentEventBus.emit(
            session,
            doc_id,
            "status_changed",
            {"from": prev_status, "to": next_status},
        )
        prev_status = next_status

    await _fail_document(
        session,
        doc_id,
        _ERROR_CODE,
        "OCR pipeline not yet implemented (M3 stub); M4 wires the real handler.",
    )
    await DocumentEventBus.emit(
        session,
        doc_id,
        "failed",
        {"from": prev_status, "to": "failed", "error_code": _ERROR_CODE},
    )

    logger.info("ocr_stub_failed_document", document_id=doc_id, error_code=_ERROR_CODE)

    # Commit document state and events BEFORE raising so they survive the
    # session close that the worker triggers when the exception propagates.
    # The worker opens a separate session to mark the job itself as failed.
    await session.commit()

    err = IngestError(_ERROR_CODE, code=_ERROR_CODE)
    err.retryable = False
    raise err


HANDLERS[JobKind.OCR.value] = handle_ocr
