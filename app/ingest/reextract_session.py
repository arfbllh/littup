"""Operator-driven re-OCR sessions with preview / accept-reject.

Flow:
1. ``start_session(doc_id, page_numbers, provider)`` creates a session row,
   snapshots the current per-page text into ``old_text``, and enqueues a
   ``page_reextract`` job.
2. The worker runs OCR on each selected page using the chosen engine and
   writes results to ``app.reextract_page_results`` — **not** ``app.spans``.
3. UI polls ``get_session(session_id)`` and shows a per-page diff.
4. ``accept_session(session_id, accepted_pages)`` overwrites ``app.spans``
   for the accepted pages, drops blocks/chunks for the doc, and enqueues
   layout → chunking → embedding **once** (cost paid one time even if 30
   pages were re-OCR'd).
5. ``reject_session(session_id)`` just marks the session ``rejected``;
   nothing in ``app.spans`` is touched.

Old session rows + their ``reextract_page_results`` rows are kept for audit
and so the operator can re-open a previous diff.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IngestError
from app.core.ids import new_uuid7
from app.ingest.events import DocumentEventBus
from app.ingest.mime import PDF, ext_for
from app.ingest.storage import LocalBlobStore
from app.jobs.kinds import JobKind
from app.jobs.queue import JobQueue

logger = structlog.get_logger(__name__)

VALID_PROVIDERS = {"pdfplumber", "paddleocr"}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


async def start_session(
    session: AsyncSession,
    *,
    document_id: str,
    page_numbers: list[int],
    provider: str,
    queue: JobQueue,
) -> dict[str, Any]:
    """Snapshot old text, persist the session row, enqueue the worker job."""
    if provider not in VALID_PROVIDERS:
        raise IngestError(
            f"Invalid provider '{provider}'; expected one of {sorted(VALID_PROVIDERS)}.",
            code="INVALID_PROVIDER",
            retryable=False,
        )
    if not page_numbers:
        raise IngestError(
            "page_numbers must contain at least one page",
            code="NO_PAGES_SELECTED",
            retryable=False,
        )

    # Validate doc exists and is in a state where re-extract makes sense.
    doc_row = await session.execute(
        text("SELECT status, mime_type, page_count FROM app.documents WHERE id = :id"),
        {"id": document_id},
    )
    doc = doc_row.fetchone()
    if doc is None:
        raise IngestError(f"Document {document_id} not found", code="DOCUMENT_NOT_FOUND")
    if doc.status not in ("ready", "failed"):
        raise IngestError(
            f"Cannot re-extract while document is in status '{doc.status}'",
            code="NOT_REEXTRACTABLE",
            retryable=False,
        )
    page_count = int(doc.page_count or 0)
    pages_clean = sorted({int(p) for p in page_numbers})
    if pages_clean[0] < 1 or pages_clean[-1] > page_count:
        raise IngestError(
            f"page numbers out of range 1..{page_count}: {pages_clean}",
            code="PAGE_OUT_OF_RANGE",
            retryable=False,
        )
    if provider == "pdfplumber" and (doc.mime_type or "").lower() != PDF:
        raise IngestError(
            "pdfplumber only applies to PDF documents.",
            code="PROVIDER_NOT_APPLICABLE",
            retryable=False,
        )

    session_id = new_uuid7()
    await session.execute(
        text(
            """
            INSERT INTO app.reextract_sessions
                   (id, document_id, provider, page_numbers, status)
            VALUES (:id, :doc, :provider, :pages, 'pending')
            """
        ),
        {
            "id": session_id,
            "doc": document_id,
            "provider": provider,
            "pages": pages_clean,
        },
    )
    await session.commit()

    await queue.enqueue(
        kind=JobKind.PAGE_REEXTRACT.value,
        payload={"session_id": session_id, "document_id": document_id},
        dedup_key=f"page_reextract:{session_id}",
    )
    await session.commit()

    logger.info(
        "reextract_session_started",
        session_id=session_id,
        document_id=document_id,
        provider=provider,
        page_numbers=pages_clean,
    )
    return {
        "session_id": session_id,
        "document_id": document_id,
        "provider": provider,
        "page_numbers": pages_clean,
        "status": "pending",
    }


async def get_session(
    session: AsyncSession, *, session_id: str
) -> dict[str, Any] | None:
    """Return the session row + per-page diffs (old vs new text)."""
    row = await session.execute(
        text(
            """
            SELECT id, document_id, provider, page_numbers, status,
                   error_message, created_at, updated_at, completed_at
              FROM app.reextract_sessions WHERE id = :id
            """
        ),
        {"id": session_id},
    )
    s = row.fetchone()
    if s is None:
        return None

    pages_rows = await session.execute(
        text(
            """
            SELECT page_number, page_id, provider, old_text, new_text,
                   status, error_message
              FROM app.reextract_page_results
             WHERE session_id = :sid
             ORDER BY page_number
            """
        ),
        {"sid": session_id},
    )
    pages = [
        {
            "page_number": int(p.page_number),
            "page_id": str(p.page_id),
            "provider": p.provider,
            "old_text": p.old_text,
            "new_text": p.new_text,
            "status": p.status,
            "error_message": p.error_message,
        }
        for p in pages_rows.fetchall()
    ]
    return {
        "session_id": str(s.id),
        "document_id": str(s.document_id),
        "provider": s.provider,
        "page_numbers": list(s.page_numbers or []),
        "status": s.status,
        "error_message": s.error_message,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "completed_at": s.completed_at,
        "pages": pages,
    }


async def accept_session(
    session: AsyncSession,
    *,
    session_id: str,
    accepted_pages: list[int] | None,
    queue: JobQueue,
) -> dict[str, Any]:
    """Apply the chosen pages' new spans, drop downstream artefacts, re-run."""
    s_row = await session.execute(
        text(
            "SELECT id, document_id, status FROM app.reextract_sessions WHERE id = :id"
        ),
        {"id": session_id},
    )
    s = s_row.fetchone()
    if s is None:
        raise IngestError(f"Session {session_id} not found", code="SESSION_NOT_FOUND")
    if s.status != "ready":
        raise IngestError(
            f"Cannot accept a session in status '{s.status}' (expected 'ready')",
            code="SESSION_NOT_READY",
            retryable=False,
        )

    document_id = str(s.document_id)
    # Default = accept every successfully OCR'd page in the session.
    page_filter_clause = ""
    params: dict[str, Any] = {"sid": session_id}
    if accepted_pages is not None:
        if not accepted_pages:
            raise IngestError(
                "accepted_pages cannot be empty; use reject_session instead.",
                code="NO_PAGES_ACCEPTED",
                retryable=False,
            )
        page_filter_clause = "AND r.page_number = ANY(:pages)"
        params["pages"] = sorted({int(p) for p in accepted_pages})

    # Pull the page_results we'll commit.
    rows = await session.execute(
        text(
            f"""
            SELECT r.page_id, r.page_number, r.provider, r.new_spans, r.status
              FROM app.reextract_page_results r
             WHERE r.session_id = :sid
               AND r.status = 'ok'
               {page_filter_clause}
            """
        ),
        params,
    )
    accepted_rows = rows.fetchall()
    if not accepted_rows:
        raise IngestError(
            "No accepted pages have a successful re-extract result.",
            code="NO_RESULTS_TO_APPLY",
            retryable=False,
        )

    # Replace spans for each accepted page, page-by-page.
    for r in accepted_rows:
        await session.execute(
            text("DELETE FROM app.spans WHERE page_id = :pid"),
            {"pid": str(r.page_id)},
        )
        spans = r.new_spans or []
        if spans:
            await session.execute(
                text(
                    """
                    INSERT INTO app.spans
                           (page_id, text, bbox_x0, bbox_y0, bbox_x1, bbox_y1, confidence, source)
                    VALUES (:page_id, :text, :x0, :y0, :x1, :y1, :confidence, :source)
                    """
                ),
                [
                    {
                        "page_id": str(r.page_id),
                        "text": sp.get("text", ""),
                        "x0": sp.get("x0"),
                        "y0": sp.get("y0"),
                        "x1": sp.get("x1"),
                        "y1": sp.get("y1"),
                        "confidence": sp.get("confidence"),
                        "source": sp.get("source") or r.provider,
                    }
                    for sp in spans
                ],
            )
        # Sticky per-page provider override so subsequent automatic re-runs
        # remember the operator's choice.
        await session.execute(
            text(
                "UPDATE app.pages SET status = 'ocr_done', "
                "ocr_provider_override = :prov WHERE id = :pid"
            ),
            {"pid": str(r.page_id), "prov": r.provider},
        )

    # Drop derived rows for the whole doc — they cross page boundaries.
    await session.execute(
        text("DELETE FROM app.chunks WHERE document_id = :id"),
        {"id": document_id},
    )
    await session.execute(
        text("DELETE FROM app.blocks WHERE document_id = :id"),
        {"id": document_id},
    )
    # Wipe prior job rows so the cascade's stable dedup keys aren't blocked.
    await session.execute(
        text("DELETE FROM jobs.jobs WHERE payload->>'document_id' = :id"),
        {"id": document_id},
    )
    # Reset doc status so layout's claim succeeds.
    await session.execute(
        text(
            """
            UPDATE app.documents
               SET status = 'ocr_done',
                   embedded_at = NULL,
                   updated_at = NOW()
             WHERE id = :id
            """
        ),
        {"id": document_id},
    )
    await DocumentEventBus.emit(
        session, document_id, "status_changed", {"to": "ocr_done"}
    )

    await session.execute(
        text(
            """
            UPDATE app.reextract_sessions
               SET status = 'accepted',
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE id = :id
            """
        ),
        {"id": session_id},
    )

    await queue.enqueue(
        kind=JobKind.LAYOUT.value,
        payload={"document_id": document_id},
        dedup_key=f"layout:{document_id}",
    )
    await session.commit()

    logger.info(
        "reextract_session_accepted",
        session_id=session_id,
        document_id=document_id,
        applied_pages=[int(r.page_number) for r in accepted_rows],
    )
    return {
        "session_id": session_id,
        "document_id": document_id,
        "applied_pages": [int(r.page_number) for r in accepted_rows],
    }


async def reject_session(
    session: AsyncSession, *, session_id: str
) -> dict[str, Any]:
    s_row = await session.execute(
        text("SELECT id, document_id, status FROM app.reextract_sessions WHERE id = :id"),
        {"id": session_id},
    )
    s = s_row.fetchone()
    if s is None:
        raise IngestError(f"Session {session_id} not found", code="SESSION_NOT_FOUND")
    if s.status in ("accepted", "rejected"):
        return {"session_id": session_id, "status": s.status}

    await session.execute(
        text(
            """
            UPDATE app.reextract_sessions
               SET status = 'rejected',
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE id = :id
            """
        ),
        {"id": session_id},
    )
    await session.commit()

    logger.info(
        "reextract_session_rejected",
        session_id=session_id,
        document_id=str(s.document_id),
    )
    return {"session_id": session_id, "status": "rejected"}


# ─────────────────────────────────────────────────────────────────────────────
# Worker entry — invoked by the page_reextract handler
# ─────────────────────────────────────────────────────────────────────────────


async def run_session(session: AsyncSession, *, session_id: str) -> dict[str, Any]:
    """Run OCR for every page in the session into the staging table."""
    s_row = await session.execute(
        text(
            "SELECT id, document_id, provider, page_numbers, status "
            "FROM app.reextract_sessions WHERE id = :id"
        ),
        {"id": session_id},
    )
    s = s_row.fetchone()
    if s is None:
        raise IngestError(f"Session {session_id} not found", code="SESSION_NOT_FOUND")
    if s.status not in ("pending", "running"):
        return {"skipped": True, "reason": s.status}

    document_id = str(s.document_id)
    provider: str = s.provider
    page_numbers: list[int] = list(s.page_numbers or [])

    await session.execute(
        text(
            "UPDATE app.reextract_sessions "
            "SET status = 'running', updated_at = NOW() WHERE id = :id"
        ),
        {"id": session_id},
    )
    await session.commit()

    doc_row = await session.execute(
        text(
            "SELECT sha256, mime_type FROM app.documents WHERE id = :id"
        ),
        {"id": document_id},
    )
    doc = doc_row.fetchone()
    if doc is None:
        await _mark_session_failed(session, session_id, "DOCUMENT_NOT_FOUND")
        raise IngestError("Document gone", code="DOCUMENT_NOT_FOUND")

    store = LocalBlobStore()
    file_path = store.path_for(doc.sha256, ext_for(doc.mime_type or PDF))
    if not file_path.exists():
        await _mark_session_failed(session, session_id, "FILE_NOT_FOUND")
        raise IngestError(f"Blob missing: {file_path}", code="FILE_NOT_FOUND")

    # Lazy imports — these are heavy.
    from app.ingest.ocr.base import load_ocr_config
    from app.ingest.service import IngestService

    cfg = load_ocr_config()
    svc = IngestService(session)

    successes = 0
    for page_num in page_numbers:
        try:
            page_id, old_text = await _snapshot_page(session, document_id, page_num)
            new_spans, new_text = await _ocr_page_to_dicts(
                svc=svc,
                file_path=file_path,
                page_num=page_num,
                provider=provider,
                cfg=cfg,
            )
            await _upsert_page_result(
                session,
                session_id=session_id,
                page_id=page_id,
                page_number=page_num,
                provider=provider,
                old_text=old_text,
                new_text=new_text,
                new_spans=new_spans,
                status="ok",
                error_message=None,
            )
            successes += 1
            logger.info(
                "reextract_page_done",
                session_id=session_id,
                document_id=document_id,
                page=page_num,
                provider=provider,
                span_count=len(new_spans),
            )
        except Exception as exc:
            logger.warning(
                "reextract_page_failed",
                session_id=session_id,
                document_id=document_id,
                page=page_num,
                error=str(exc),
            )
            try:
                page_id, old_text = await _snapshot_page(session, document_id, page_num)
            except Exception:
                page_id, old_text = ("", "")
            if page_id:
                await _upsert_page_result(
                    session,
                    session_id=session_id,
                    page_id=page_id,
                    page_number=page_num,
                    provider=provider,
                    old_text=old_text,
                    new_text="",
                    new_spans=[],
                    status="failed",
                    error_message=str(exc)[:500],
                )
        await session.commit()

    final_status = "ready" if successes > 0 else "failed"
    await session.execute(
        text(
            """
            UPDATE app.reextract_sessions
               SET status = :s, completed_at = NOW(), updated_at = NOW()
             WHERE id = :id
            """
        ),
        {"s": final_status, "id": session_id},
    )
    await session.commit()

    return {
        "session_id": session_id,
        "document_id": document_id,
        "status": final_status,
        "successes": successes,
        "total": len(page_numbers),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _snapshot_page(
    session: AsyncSession, document_id: str, page_num: int
) -> tuple[str, str]:
    row = await session.execute(
        text(
            "SELECT id FROM app.pages WHERE document_id = :doc AND page_number = :pn"
        ),
        {"doc": document_id, "pn": page_num},
    )
    page = row.fetchone()
    if page is None:
        raise IngestError(
            f"Page {page_num} not found for document {document_id}",
            code="PAGE_NOT_FOUND",
            retryable=False,
        )
    page_id = str(page.id)
    text_row = await session.execute(
        text(
            "SELECT COALESCE(string_agg(text, ' ' ORDER BY bbox_y0, bbox_x0), '') AS old_text "
            "FROM app.spans WHERE page_id = :pid"
        ),
        {"pid": page_id},
    )
    return page_id, text_row.scalar_one() or ""


async def _ocr_page_to_dicts(
    *,
    svc,
    file_path,
    page_num: int,
    provider: str,
    cfg,
) -> tuple[list[dict], str]:
    """Run OCR for one page using the chosen engine; return spans + joined text."""
    from app.ingest.ocr.paddle_ocr import PaddleProvider
    from app.ingest.ocr.pdfplumber_ocr import PdfplumberProvider

    if provider == "pdfplumber":
        extraction = await PdfplumberProvider().extract_page(file_path, page_num)
    elif provider == "paddleocr":
        page_img = await svc._rasterise_one_page(file_path, page_num)
        extraction = await PaddleProvider().extract_page(
            file_path, page_num, page_image=page_img
        )
    else:
        raise IngestError(
            f"Unsupported provider '{provider}'", code="UNSUPPORTED_PROVIDER"
        )

    spans = [
        {
            "text": sp.text,
            "x0": sp.bbox[0],
            "y0": sp.bbox[1],
            "x1": sp.bbox[2],
            "y1": sp.bbox[3],
            "confidence": sp.confidence,
            "source": sp.source,
        }
        for sp in extraction.spans
    ]
    return spans, extraction.full_text or ""


async def _upsert_page_result(
    session: AsyncSession,
    *,
    session_id: str,
    page_id: str,
    page_number: int,
    provider: str,
    old_text: str,
    new_text: str,
    new_spans: list[dict],
    status: str,
    error_message: str | None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO app.reextract_page_results
                   (session_id, page_id, page_number, provider, old_text,
                    new_text, new_spans, status, error_message)
            VALUES (:sid, :pid, :pn, :prov, :ot, :nt, CAST(:ns AS jsonb), :st, :em)
            ON CONFLICT (session_id, page_number) DO UPDATE
              SET page_id = EXCLUDED.page_id,
                  provider = EXCLUDED.provider,
                  old_text = EXCLUDED.old_text,
                  new_text = EXCLUDED.new_text,
                  new_spans = EXCLUDED.new_spans,
                  status = EXCLUDED.status,
                  error_message = EXCLUDED.error_message
            """
        ),
        {
            "sid": session_id,
            "pid": page_id,
            "pn": page_number,
            "prov": provider,
            "ot": old_text,
            "nt": new_text,
            "ns": json.dumps(new_spans),
            "st": status,
            "em": error_message,
        },
    )


async def _mark_session_failed(
    session: AsyncSession, session_id: str, reason: str
) -> None:
    await session.execute(
        text(
            """
            UPDATE app.reextract_sessions
               SET status = 'failed',
                   error_message = :err,
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE id = :id
            """
        ),
        {"id": session_id, "err": reason},
    )
    await session.commit()
