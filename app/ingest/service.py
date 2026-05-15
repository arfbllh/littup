"""IngestService — orchestrates upload → atomic upsert → file commit → enqueue → OCR."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IngestError, RateLimitError
from app.ingest.events import DocumentEventBus, DocumentEventRow
from app.ingest.hashing import HashedUpload, stream_to_tempfile
from app.ingest.mime import JSON_MIME, MARKDOWN, PDF, TXT, detect_mime, ext_for
from app.ingest.storage import LocalBlobStore
from app.jobs.kinds import JobKind
from app.jobs.queue import JobQueue
from app.settings import settings

if TYPE_CHECKING:
    from app.llm.router import LLMRouter

logger = structlog.get_logger(__name__)

# Shared thread pool for CPU-bound OCR steps (classifier, preprocess)
_OCR_EXECUTOR = ThreadPoolExecutor(max_workers=settings.OCR_PAGE_WORKERS)



@dataclass
class UploadResult:
    document_id: str
    sha256: str
    status: str
    was_new: bool


@dataclass
class DocumentRecord:
    id: str
    sha256: str
    filename: str
    mime_type: str | None
    size_bytes: int | None
    page_count: int | None
    status: str
    last_event_seq: int
    error_code: str | None
    error_message: str | None
    created_at: Any
    updated_at: Any
    has_blocks: bool = False
    ocr_provider_override: str | None = None
    ocr_provider_used: str | None = None


class IngestService:
    def __init__(
        self,
        session: AsyncSession,
        store: LocalBlobStore | None = None,
        queue: JobQueue | None = None,
        max_pending: int | None = None,
    ) -> None:
        self.session = session
        self.store = store or LocalBlobStore()
        self.queue = queue or JobQueue(session)
        self.max_pending = (
            max_pending if max_pending is not None else settings.JOB_QUEUE_MAX_PENDING
        )

    # ── Upload ───────────────────────────────────────────────────────
    async def upload(self, upload: UploadFile) -> UploadResult:
        # Backpressure pre-check
        pending = await self.queue.pending_count()
        if pending >= self.max_pending:
            raise RateLimitError(
                "Ingestion queue is saturated; retry shortly",
                code="QUEUE_SATURATED",
                retry_after=10,
            )

        hashed = await stream_to_tempfile(
            upload, tmp_dir=self.store.tmp_dir, max_bytes=settings.MAX_UPLOAD_BYTES
        )
        try:
            mime = detect_mime(hashed.path, filename_hint=upload.filename)
            ext = ext_for(mime)
            filename = upload.filename or f"{hashed.sha256}{ext}"

            doc_id, was_new, prev_was_failed = await self._atomic_upsert(
                sha256=hashed.sha256,
                filename=filename,
                mime=mime,
                size=hashed.size_bytes,
            )

            if was_new or prev_was_failed:
                if was_new:
                    final_path = self.store.path_for(hashed.sha256, ext)
                    self.store.commit(hashed.path, final_path)
                    hashed = HashedUpload(  # tempfile now lives at final_path
                        path=final_path, sha256=hashed.sha256, size_bytes=hashed.size_bytes
                    )
                else:
                    hashed.path.unlink(missing_ok=True)
                await DocumentEventBus.emit(
                    self.session,
                    doc_id,
                    "uploaded",
                    {"sha256": hashed.sha256, "mime": mime, "size_bytes": hashed.size_bytes},
                )
                await self.queue.enqueue(
                    kind=JobKind.OCR.value,
                    payload={"document_id": doc_id},
                    dedup_key=f"ocr:{doc_id}",
                )
                log_msg = "ingest_uploaded" if was_new else "ingest_requeued_after_failure"
                logger.info(
                    log_msg,
                    document_id=doc_id,
                    sha256=hashed.sha256,
                    size_bytes=hashed.size_bytes,
                    mime=mime,
                )
            else:
                # Duplicate by content: drop the tempfile, leave the existing record alone.
                hashed.path.unlink(missing_ok=True)
                logger.info("ingest_deduplicated", document_id=doc_id, sha256=hashed.sha256)
        except Exception:
            hashed.path.unlink(missing_ok=True)
            raise

        status_row = await self.session.execute(
            text("SELECT status FROM app.documents WHERE id = :id"), {"id": doc_id}
        )
        status = status_row.scalar_one()

        return UploadResult(
            document_id=doc_id,
            sha256=hashed.sha256,
            status=status,
            was_new=was_new or prev_was_failed,
        )

    async def _atomic_upsert(
        self, *, sha256: str, filename: str, mime: str, size: int
    ) -> tuple[str, bool, bool]:
        """Single statement returning (id, was_new, prev_was_failed)."""
        result = await self.session.execute(
            text(
                """
                WITH prev AS (
                    SELECT status FROM app.documents WHERE sha256 = :sha
                )
                INSERT INTO app.documents (sha256, filename, mime_type, size_bytes, status)
                VALUES (:sha, :name, :mime, :size, 'uploaded')
                ON CONFLICT (sha256) DO UPDATE
                   SET last_accessed_at = NOW(),
                       status = CASE
                                  WHEN app.documents.status = 'failed' THEN 'uploaded'
                                  ELSE app.documents.status
                                END,
                       updated_at = NOW()
                RETURNING id, (xmax = 0) AS inserted,
                         COALESCE((SELECT status = 'failed' FROM prev), FALSE) AS prev_was_failed
                """
            ),
            {"sha": sha256, "name": filename, "mime": mime, "size": size},
        )
        row = result.fetchone()
        if row is None:
            raise RuntimeError("atomic upsert returned no row")
        return str(row.id), bool(row.inserted), bool(row.prev_was_failed)

    # ── Reads ────────────────────────────────────────────────────────
    async def get_document(self, document_id: str) -> DocumentRecord | None:
        result = await self.session.execute(
            text(
                """
                SELECT d.id, d.sha256, d.filename, d.mime_type, d.size_bytes, d.page_count,
                       d.status, d.last_event_seq, d.error_code, d.error_message,
                       d.created_at, d.updated_at, d.ocr_provider_override,
                       (
                           SELECT s.source FROM app.spans s
                             JOIN app.pages p ON p.id = s.page_id
                            WHERE p.document_id = d.id AND s.source IS NOT NULL
                            GROUP BY s.source
                            ORDER BY COUNT(*) DESC
                            LIMIT 1
                       ) AS ocr_provider_used
                FROM app.documents d WHERE d.id = :id
                """
            ),
            {"id": document_id},
        )
        row = result.fetchone()
        if row is None:
            return None
        return DocumentRecord(
            id=str(row.id),
            sha256=row.sha256,
            filename=row.filename,
            mime_type=row.mime_type,
            size_bytes=row.size_bytes,
            page_count=row.page_count,
            status=row.status,
            last_event_seq=int(row.last_event_seq),
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
            ocr_provider_override=row.ocr_provider_override,
            ocr_provider_used=row.ocr_provider_used,
        )

    async def list_documents(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status_filter: str | None = None,
    ) -> tuple[list[DocumentRecord], int]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        where = ""
        if status_filter:
            where = "WHERE d.status = :status"
            params["status"] = status_filter
        # `has_blocks` lets the list page surface a retry button on
        # `ready` docs with zero extracted blocks without an N+1 fetch.
        result = await self.session.execute(
            text(
                f"""
                SELECT d.id, d.sha256, d.filename, d.mime_type, d.size_bytes,
                       d.page_count, d.status, d.last_event_seq, d.error_code,
                       d.error_message, d.created_at, d.updated_at,
                       EXISTS (
                           SELECT 1 FROM app.blocks b WHERE b.document_id = d.id
                       ) AS has_blocks
                FROM app.documents d {where}
                ORDER BY d.created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        rows = result.fetchall()
        items = [
            DocumentRecord(
                id=str(r.id),
                sha256=r.sha256,
                filename=r.filename,
                mime_type=r.mime_type,
                size_bytes=r.size_bytes,
                page_count=r.page_count,
                status=r.status,
                last_event_seq=int(r.last_event_seq),
                error_code=r.error_code,
                error_message=r.error_message,
                created_at=r.created_at,
                updated_at=r.updated_at,
                has_blocks=bool(r.has_blocks),
            )
            for r in rows
        ]
        return items, offset + len(items)

    async def get_blocks(self, document_id: str) -> dict[str, Any]:
        doc = await self.get_document(document_id)
        if doc is None:
            return {"status": None, "blocks": []}
        result = await self.session.execute(
            text(
                """
                SELECT id, block_type, text, page_start, page_end,
                       reading_order, bbox_x0, bbox_y0, bbox_x1, bbox_y1, metadata
                FROM app.blocks
                WHERE document_id = :doc_id
                ORDER BY reading_order
                """
            ),
            {"doc_id": document_id},
        )
        blocks = [
            {
                "id": str(r.id),
                "block_type": r.block_type,
                "text": r.text,
                "page_start": r.page_start,
                "page_end": r.page_end,
                "reading_order": r.reading_order,
                "bbox": [r.bbox_x0, r.bbox_y0, r.bbox_x1, r.bbox_y1],
                "metadata": r.metadata or {},
            }
            for r in result.fetchall()
        ]
        return {"status": doc.status, "blocks": blocks}

    async def replay_events(self, document_id: str, after_seq: int) -> list[DocumentEventRow]:
        return await DocumentEventBus.replay(self.session, document_id, after_seq)

    def upload_path(self, sha256: str, ext: str):
        return self.store.path_for(sha256, ext)

    # ── OCR orchestration ─────────────────────────────────────────────────────

    async def ocr_document(
        self,
        document_id: str,
        *,
        llm_router: LLMRouter | None = None,
        config=None,  # OCRConfig | None — pass in tests to override YAML config
        only_page: int | None = None,
    ) -> dict[str, Any]:
        """Drive a document through the OCR state machine.

        Returns a summary dict used as the job result payload.
        State transitions: ocr_pending → ocr_running → ocr_done (or failed).

        ``only_page``: when set, OCR only that single page (1-based) and skip
        the full-document state transitions. Used by per-page re-extract — the
        caller is responsible for re-enqueueing layout/chunking/embedding so
        downstream artifacts pick up the new spans.
        """
        from app.ingest.ocr.base import load_ocr_config
        from app.ingest.ocr.pdfplumber_ocr import has_text_layer
        from app.ingest.ocr.routing import route_and_extract

        cfg = config if config is not None else load_ocr_config(settings.OCR_CONFIG_PATH)

        # ── Load document record ──────────────────────────────────────────────
        row = await self.session.execute(
            text(
                "SELECT id, sha256, mime_type, status, ocr_provider_override "
                "FROM app.documents WHERE id = :id"
            ),
            {"id": document_id},
        )
        doc = row.fetchone()
        if doc is None:
            raise IngestError(f"Document {document_id} not found", code="DOCUMENT_NOT_FOUND")
        provider_override: str | None = doc.ocr_provider_override

        # In single-page mode the document is already past `ready` — we do
        # NOT short-circuit and we do NOT touch the doc-level status. Layout
        # and downstream stages re-run only because reextract_page enqueues
        # them after this OCR job completes.
        if only_page is None and doc.status in ("ocr_done", "ready"):
            return {"skipped": True, "reason": "already_done"}

        # ── ocr_pending ───────────────────────────────────────────────────────
        if only_page is None:
            await self._set_doc_status(document_id, "ocr_pending")
            await DocumentEventBus.emit(
                self.session, document_id, "status_changed", {"to": "ocr_pending"}
            )
            await self.session.commit()

        # ── Locate file on disk ───────────────────────────────────────────────
        ext = ext_for(doc.mime_type or PDF)
        file_path = self.store.path_for(doc.sha256, ext)
        if not file_path.exists():
            await self._fail_document(document_id, "FILE_NOT_FOUND", f"Blob missing: {file_path}")
            await self.session.commit()
            raise IngestError(f"Uploaded file not found at {file_path}", code="FILE_NOT_FOUND")

        # ── ocr_running ───────────────────────────────────────────────────────
        if only_page is None:
            claimed = await self._claim_ocr_running(document_id)
            if not claimed:
                return {"skipped": True, "reason": "already_claimed"}
            await DocumentEventBus.emit(
                self.session, document_id, "status_changed", {"to": "ocr_running"}
            )
            await self.session.commit()

        # ── Text-bearing formats (.txt, .md, .json) bypass OCR entirely ───────
        mime_lower = (doc.mime_type or "").lower()
        if mime_lower in (TXT, MARKDOWN, JSON_MIME):
            return await self._extract_text_document(
                document_id=document_id, file_path=file_path, mime=mime_lower
            )

        # ── Determine if this is a native PDF ─────────────────────────────────
        is_pdf = mime_lower == PDF
        if provider_override == "paddleocr":
            # Operator forced PaddleOCR for this doc — ignore any text layer.
            native = False
        elif provider_override == "pdfplumber" and is_pdf:
            # Operator forced pdfplumber — assume native regardless of heuristic.
            native = True
        elif settings.OCR_FORCE_RASTER:
            # Global setting: ignore embedded text layers across all PDFs.
            native = False
        else:
            native = is_pdf and has_text_layer(file_path)
        is_scan_pdf = is_pdf and not native

        # ── Rasterise pages (scan/image path only; native PDFs skip rasterisation) ─
        try:
            page_images = await self._rasterise_pages(file_path, is_pdf=is_pdf, native=native)
        except Exception as exc:
            await self._fail_document(document_id, "RASTERISE_ERROR", str(exc))
            await self.session.commit()
            raise IngestError(f"Failed to rasterise {file_path}: {exc}", code="RASTERISE_ERROR") from exc

        try:
            page_count = len(page_images) if page_images is not None else await self._count_pdf_pages(file_path)
        except Exception as exc:
            await self._fail_document(document_id, "CORRUPT_PDF", str(exc))
            await self.session.commit()
            raise IngestError(str(exc), code="CORRUPT_PDF", retryable=False) from exc
        await self.session.execute(
            text("UPDATE app.documents SET page_count = :n WHERE id = :id"),
            {"n": page_count, "id": document_id},
        )
        # Commit page_count so other readers see it immediately (C-6)
        await self.session.commit()

        # ── Per-page OCR ───────────────────────────────────────────────────────
        from app.core.errors import CancelledIngest
        from app.jobs.context import current_job_id
        from app.jobs.queue import JobQueue as _JobQueue

        active_job_id = current_job_id.get()
        cancel_probe = _JobQueue(self.session) if active_job_id else None

        async def _check_cancel(stage: str, page: int) -> None:
            if cancel_probe is None:
                return
            if await cancel_probe.is_cancel_requested(active_job_id):
                logger.info(
                    "ocr_cancelled",
                    document_id=document_id,
                    job_id=active_job_id,
                    stage=stage,
                    page=page,
                )
                raise CancelledIngest(
                    f"OCR cancelled at {stage} (page {page}) for document {document_id}"
                )

        # Single-page mode (per-page re-extract) — clamp the loop to that page.
        if only_page is not None:
            if only_page < 1 or only_page > page_count:
                raise IngestError(
                    f"only_page={only_page} out of range for {page_count}-page doc",
                    code="PAGE_OUT_OF_RANGE",
                    retryable=False,
                )
            page_iter = range(only_page, only_page + 1)
        else:
            page_iter = range(1, page_count + 1)

        results: list[dict[str, Any]] = []
        try:
            for page_num in page_iter:
                await _check_cancel("page_start", page_num)

                # Per-page override beats doc-level routing — operator can pin
                # a single page to pdfplumber/paddleocr without re-running the
                # whole doc through the unwanted engine.
                page_override = await self._get_page_provider_override(
                    document_id, page_num
                )
                if page_override == "pdfplumber" and is_pdf:
                    page_native = True
                elif page_override == "paddleocr":
                    page_native = False
                else:
                    page_native = native

                if page_native:
                    page_img = None
                elif page_images is not None and page_num - 1 < len(page_images):
                    page_img = page_images[page_num - 1]
                else:
                    # is_scan_pdf or page_override forced us off the native path
                    try:
                        page_img = await self._rasterise_one_page(file_path, page_num)
                    except Exception as exc:
                        results.append({"page": page_num, "status": "failed", "error": str(exc)})
                        continue
                await _check_cancel("post_rasterise", page_num)
                page_result = await self._ocr_one_page(
                    document_id=document_id,
                    page_num=page_num,
                    page_img=page_img,
                    file_path=file_path,
                    has_text_layer=page_native,
                    cfg=cfg,
                    llm_router=llm_router,
                    route_and_extract=route_and_extract,
                )
                logger.info(
                    "ocr_page_done",
                    document_id=document_id,
                    page=page_num,
                    provider=page_result.get("source"),
                    span_count=page_result.get("span_count"),
                    mean_confidence=page_result.get("mean_confidence"),
                )
                results.append(page_result)
        except CancelledIngest:
            # Don't touch app.documents — the row may already be deleted.
            raise
        except Exception as exc:
            await self._fail_document(document_id, "OCR_LOOP_ERROR", str(exc))
            await self.session.commit()
            raise IngestError(str(exc), code="OCR_LOOP_ERROR") from exc

        # ── ocr_done ──────────────────────────────────────────────────────────
        # Single-page mode never sets the doc-level status — the doc is already
        # past `ready`; only the targeted page's spans were updated.
        if only_page is None:
            await self._set_doc_status(document_id, "ocr_done")
            await DocumentEventBus.emit(
                self.session, document_id, "status_changed", {"to": "ocr_done"}
            )
            await self.session.commit()

        total_spans = sum(r.get("span_count", 0) for r in results)
        vlm_pages = sum(1 for r in results if r.get("source") in ("vlm", "vlm_description"))
        provider_counts: dict[str, int] = {}
        for r in results:
            src = r.get("source") or "unknown"
            provider_counts[src] = provider_counts.get(src, 0) + 1
        logger.info(
            "ocr_document_done",
            document_id=document_id,
            page_count=page_count,
            total_spans=total_spans,
            vlm_pages=vlm_pages,
            providers=provider_counts,
        )
        return {
            "document_id": document_id,
            "page_count": page_count,
            "total_spans": total_spans,
            "vlm_pages": vlm_pages,
            "pages": results,
        }

    async def _rasterise_pages(self, file_path: Path, *, is_pdf: bool, native: bool = False) -> list | None:
        """Return a list of numpy arrays, one per page, or None for native/scanned PDFs.

        Native PDFs skip rasterisation entirely — pdfplumber reads the text layer
        directly, so allocating page images would be wasteful (C-1).
        Scanned PDFs also return None — caller uses _rasterise_one_page per page
        to avoid loading all pages into RAM at once (M-1).
        """
        if is_pdf and native:
            return None  # native path; caller uses _count_pdf_pages instead

        if not is_pdf:
            import cv2
            img = cv2.imread(str(file_path))
            if img is None:
                raise IngestError(f"Cannot read image: {file_path}", code="IMAGE_READ_ERROR")
            max_side = settings.OCR_MAX_IMAGE_SIDE
            h, w = img.shape[:2]
            longest = max(h, w)
            if longest > max_side:
                scale = max_side / longest
                img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            return [img]

        # Scanned PDF: return None — caller uses _rasterise_one_page per page
        return None

    async def _get_page_provider_override(
        self, document_id: str, page_num: int
    ) -> str | None:
        """Return the per-page OCR engine override, or None if not set."""
        result = await self.session.execute(
            text(
                "SELECT ocr_provider_override FROM app.pages "
                "WHERE document_id = :doc AND page_number = :pn"
            ),
            {"doc": document_id, "pn": page_num},
        )
        row = result.fetchone()
        return row.ocr_provider_override if row else None

    async def _rasterise_one_page(self, file_path: Path, page_num: int):
        """Rasterise a single page from a scanned PDF. Frees memory after each call."""
        try:
            from pdf2image import convert_from_path
        except ImportError as exc:
            raise IngestError("pdf2image not installed", code="PDF2IMAGE_MISSING") from exc

        import cv2
        import numpy as np

        loop = asyncio.get_running_loop()

        def _convert():
            pages = convert_from_path(
                str(file_path),
                dpi=settings.OCR_RASTER_DPI,
                first_page=page_num,
                last_page=page_num,
            )
            if not pages:
                raise IngestError(
                    f"pdf2image returned no image for page {page_num}", code="RASTERISE_ERROR"
                )
            img = cv2.cvtColor(np.array(pages[0]), cv2.COLOR_RGB2BGR)
            # Cap the long side. PaddleOCR's detector resizes anything bigger to
            # OCR_MAX_IMAGE_SIDE internally; doing it here saves RAM and avoids
            # the "Resized image size exceeds max_side_limit" warning.
            max_side = settings.OCR_MAX_IMAGE_SIDE
            h, w = img.shape[:2]
            longest = max(h, w)
            if longest > max_side:
                scale = max_side / longest
                new_w = int(w * scale)
                new_h = int(h * scale)
                img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            return img

        return await loop.run_in_executor(_OCR_EXECUTOR, _convert)

    async def _count_pdf_pages(self, file_path: Path) -> int:
        """Count pages in a native PDF without rasterising."""
        import pdfplumber

        loop = asyncio.get_running_loop()

        def _count() -> int:
            with pdfplumber.open(str(file_path)) as pdf:
                return len(pdf.pages)

        return await loop.run_in_executor(_OCR_EXECUTOR, _count)

    async def _ocr_one_page(
        self,
        *,
        document_id: str,
        page_num: int,
        page_img,
        file_path: Path,
        has_text_layer: bool,
        cfg,
        llm_router,
        route_and_extract,
    ) -> dict[str, Any]:
        """OCR a single page and persist spans transactionally."""
        page_id: str | None = None
        try:
            # Insert page row — inside the try so PAGE_CREATE_ERROR is caught (E-1)
            if page_img is not None:
                w = float(page_img.shape[1])
                h = float(page_img.shape[0])
            else:
                loop = asyncio.get_running_loop()
                w, h = await loop.run_in_executor(
                    _OCR_EXECUTOR, self._get_pdf_page_dimensions, file_path, page_num
                )
            page_id_row = await self.session.execute(
                text(
                    """
                    INSERT INTO app.pages (document_id, page_number, width, height, status)
                    VALUES (:doc_id, :pn, :w, :h, 'ocr_running')
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """
                ),
                {"doc_id": document_id, "pn": page_num, "w": w, "h": h},
            )
            row = page_id_row.fetchone()
            if row is None:
                existing = await self.session.execute(
                    text("SELECT id FROM app.pages WHERE document_id = :d AND page_number = :p"),
                    {"d": document_id, "p": page_num},
                )
                row = existing.fetchone()
            if row is None:
                raise IngestError(
                    f"Could not create or find page {page_num} for document {document_id}",
                    code="PAGE_CREATE_ERROR",
                )
            page_id = str(row.id)

            extraction = await route_and_extract(
                page_image=page_img,
                source_path=file_path,
                page_num=page_num,
                has_text_layer=has_text_layer,
                doc_id=document_id,
                session=self.session,
                config=cfg,
                llm_router=llm_router,
            )
        except Exception as exc:
            # Per-page failure doesn't kill the whole document
            if page_id is not None:
                try:
                    await self.session.execute(
                        text("UPDATE app.pages SET status = 'ocr_failed' WHERE id = :id"),
                        {"id": page_id},
                    )
                except Exception as update_exc:
                    logger.warning(
                        "ocr_page_failed_update_error",
                        document_id=document_id,
                        page=page_num,
                        error=str(update_exc),
                    )
            logger.warning(
                "ocr_page_failed",
                document_id=document_id,
                page=page_num,
                error=str(exc),
            )
            await self.session.commit()
            return {"page": page_num, "status": "failed", "error": str(exc)}

        # Single bulk INSERT for all spans (M-3)
        if extraction.spans:
            span_rows = [
                {
                    "page_id": page_id,
                    "text": s.text,
                    "x0": s.bbox[0],
                    "y0": s.bbox[1],
                    "x1": s.bbox[2],
                    "y1": s.bbox[3],
                    "confidence": s.confidence,
                    "source": s.source,
                }
                for s in extraction.spans
            ]
            await self.session.execute(
                text(
                    """
                    INSERT INTO app.spans
                           (page_id, text, bbox_x0, bbox_y0, bbox_x1, bbox_y1, confidence, source)
                    VALUES (:page_id, :text, :x0, :y0, :x1, :y1, :confidence, :source)
                    """
                ),
                span_rows,
            )

        page_status = "ocr_done" if extraction.source != "vlm_budget_exceeded" else "vlm_budget_exceeded"
        await self.session.execute(
            text("UPDATE app.pages SET status = :s WHERE id = :id"),
            {"s": page_status, "id": page_id},
        )
        await self.session.commit()

        return {
            "page": page_num,
            "status": page_status,
            "span_count": len(extraction.spans),
            "mean_confidence": round(extraction.mean_confidence, 3),
            "source": extraction.source,
        }

    def _get_pdf_page_dimensions(self, file_path: Path, page_num: int) -> tuple[float, float]:
        """Return (width, height) in points for a native PDF page."""
        import pdfplumber
        with pdfplumber.open(str(file_path)) as pdf:
            if page_num < 1 or page_num > len(pdf.pages):
                return 612.0, 792.0  # fallback to letter size
            page = pdf.pages[page_num - 1]
            return float(page.width or 612.0), float(page.height or 792.0)

    async def _extract_text_document(
        self, *, document_id: str, file_path: Path, mime: str
    ) -> dict[str, Any]:
        """Bypass OCR for plain-text formats.

        Reads the file as UTF-8, splits on blank lines into paragraphs, and
        writes one synthetic page + one span per paragraph (with sequential
        non-overlapping normalised bboxes so the layout pass groups them as
        distinct blocks). Drives the doc straight to ocr_done; the OCR job
        handler then enqueues layout as usual.
        """
        try:
            raw = await asyncio.get_running_loop().run_in_executor(
                _OCR_EXECUTOR, file_path.read_text, "utf-8"
            )
        except UnicodeDecodeError as exc:
            await self._fail_document(
                document_id, "TEXT_DECODE_ERROR", f"Could not decode as UTF-8: {exc}"
            )
            await self.session.commit()
            raise IngestError(
                f"Text file {file_path} is not valid UTF-8: {exc}",
                code="TEXT_DECODE_ERROR",
                retryable=False,
            ) from exc

        # Split on blank lines; collapse interior whitespace; drop empties.
        paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = [raw.strip() or ""]

        await self.session.execute(
            text("UPDATE app.documents SET page_count = 1 WHERE id = :id"),
            {"id": document_id},
        )
        await self.session.commit()

        page_row = await self.session.execute(
            text(
                """
                INSERT INTO app.pages (document_id, page_number, width, height, status)
                VALUES (:doc_id, 1, 1.0, 1.0, 'ocr_done')
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            {"doc_id": document_id},
        )
        row = page_row.fetchone()
        if row is None:
            existing = await self.session.execute(
                text("SELECT id FROM app.pages WHERE document_id = :d AND page_number = 1"),
                {"d": document_id},
            )
            row = existing.fetchone()
        if row is None:
            await self._fail_document(
                document_id, "PAGE_CREATE_ERROR", "Could not create page row for text doc"
            )
            await self.session.commit()
            raise IngestError("Page row insert failed for text document", code="PAGE_CREATE_ERROR")
        page_id = str(row.id)

        # One span per paragraph, normalised bbox spanning a vertical slice
        # of the page. The layout pass groups by vertical proximity, so the
        # slices need clear gaps to land as separate blocks.
        if mime == MARKDOWN:
            source_tag = "markdown"
        elif mime == JSON_MIME:
            source_tag = "json"
        else:
            source_tag = "text"
        n = len(paragraphs)
        slice_h = 1.0 / max(n, 1)
        span_rows = []
        for i, para in enumerate(paragraphs):
            y0 = i * slice_h
            y1 = (i + 1) * slice_h - (slice_h * 0.2 if n > 1 else 0)
            span_rows.append({
                "page_id": page_id,
                "text": para,
                "x0": 0.05,
                "y0": y0,
                "x1": 0.95,
                "y1": max(y1, y0 + 1e-4),
                "confidence": 1.0,
                "source": source_tag,
            })
        if span_rows:
            await self.session.execute(
                text(
                    """
                    INSERT INTO app.spans
                           (page_id, text, bbox_x0, bbox_y0, bbox_x1, bbox_y1, confidence, source)
                    VALUES (:page_id, :text, :x0, :y0, :x1, :y1, :confidence, :source)
                    """
                ),
                span_rows,
            )

        await self._set_doc_status(document_id, "ocr_done")
        await DocumentEventBus.emit(
            self.session, document_id, "status_changed", {"to": "ocr_done"}
        )
        await self.session.commit()

        logger.info(
            "text_document_extracted",
            document_id=document_id,
            mime=mime,
            paragraph_count=n,
            char_count=len(raw),
        )
        return {
            "document_id": document_id,
            "page_count": 1,
            "total_spans": n,
            "vlm_pages": 0,
            "pages": [{"page": 1, "status": "ocr_done", "span_count": n, "source": source_tag}],
        }

    # ── Layout / Chunking / Embedding orchestration ───────────────────────────

    async def parse_layout(self, document_id: str) -> dict[str, Any]:
        """Drive a document through ocr_done → layout_done.

        layout.parse_layout owns the state machine (claim, transition, emit).
        This wrapper exists so callers go through the service surface.
        """
        from app.ingest.layout import parse_layout as _parse_layout

        return await _parse_layout(document_id, self.session)

    async def chunk_document(self, document_id: str) -> dict[str, Any]:
        """Drive a document through layout_done → chunking_done."""
        from app.ingest.chunker import chunk_blocks

        claimed = await self._claim_chunking_running(document_id)
        if not claimed:
            return {"skipped": True, "reason": "already_claimed_or_not_ready"}
        await DocumentEventBus.emit(
            self.session, document_id, "status_changed", {"to": "chunking_running"}
        )
        await self.session.commit()

        try:
            block_rows = await self.session.execute(
                text(
                    """
                    SELECT id, block_type, text, page_start, page_end, reading_order,
                           metadata AS metadata_
                    FROM app.blocks
                    WHERE document_id = :doc_id
                    ORDER BY reading_order
                    """
                ),
                {"doc_id": document_id},
            )
            blocks = list(block_rows.fetchall())

            chunks = chunk_blocks(document_id, blocks)

            if chunks:
                # Pass Python lists directly — asyncpg infers TEXT[] from column defs.
                # Using CAST(:x AS text[]) with a PG literal string fails in executemany.
                rows = [
                    {
                        "id": c["id"],
                        "document_id": c["document_id"],
                        "text": c["text"],
                        "token_count": c["token_count"],
                        "chunk_type": c["chunk_type"],
                        "section_path": list(c["section_path"]),
                        "block_ids": list(c["block_ids"]),
                        "page_start": c["page_start"],
                        "page_end": c["page_end"],
                        "char_start": c["char_start"],
                        "char_end": c["char_end"],
                        "entities": list(c["entities"]),
                        "metadata": json.dumps(c.get("metadata") or {}),
                    }
                    for c in chunks
                ]
                await self.session.execute(
                    text(
                        """
                        INSERT INTO app.chunks
                               (id, document_id, text, token_count, chunk_type,
                                section_path, block_ids, page_start, page_end,
                                char_start, char_end, entities, metadata)
                        VALUES (:id, :document_id, :text, :token_count, :chunk_type,
                                :section_path,
                                :block_ids,
                                :page_start, :page_end, :char_start, :char_end,
                                :entities,
                                CAST(:metadata AS jsonb))
                        """
                    ),
                    rows,
                )

            await self._set_doc_status(document_id, "chunking_done")
            await DocumentEventBus.emit(
                self.session, document_id, "status_changed", {"to": "chunking_done"}
            )
            await self.session.commit()
        except Exception as exc:
            await self._fail_document(document_id, "CHUNKING_ERROR", str(exc))
            await self.session.commit()
            raise

        logger.info(
            "chunk_document_done", document_id=document_id, chunk_count=len(chunks)
        )
        return {"document_id": document_id, "chunk_count": len(chunks)}

    async def embed_chunks(self, document_id: str, embedder) -> dict[str, Any]:
        """Drive a document through chunking_done → ready.

        Commits after each batch so a crash mid-document resumes correctly:
        only un-embedded chunks are selected on re-entry.
        """
        claimed = await self._claim_embedding_running(document_id)
        if not claimed:
            return {"skipped": True, "reason": "already_claimed_or_not_ready"}
        await DocumentEventBus.emit(
            self.session, document_id, "status_changed", {"to": "embedding_running"}
        )
        await self.session.commit()

        try:
            result = await self.session.execute(
                text(
                    """
                    SELECT id, text FROM app.chunks
                    WHERE document_id = :doc_id AND embedding IS NULL
                    ORDER BY id
                    """
                ),
                {"doc_id": document_id},
            )
            chunks = result.fetchall()

            batch_size = settings.EMBEDDING_BATCH_SIZE
            embedded_count = 0
            for i in range(0, len(chunks), batch_size):
                batch = chunks[i : i + batch_size]
                vectors = await embedder.embed([c.text for c in batch])
                for chunk_row, vec in zip(batch, vectors):
                    vec_str = "[" + ",".join(str(f) for f in vec) + "]"
                    await self.session.execute(
                        text(
                            "UPDATE app.chunks SET embedding = CAST(:v AS vector) "
                            "WHERE id = :id"
                        ),
                        {"v": vec_str, "id": str(chunk_row.id)},
                    )
                await self.session.commit()
                embedded_count += len(batch)

            await self.session.execute(
                text(
                    """
                    UPDATE app.documents
                       SET status = 'ready', embedded_at = NOW(), updated_at = NOW()
                     WHERE id = :doc_id
                    """
                ),
                {"doc_id": document_id},
            )
            await DocumentEventBus.emit(
                self.session, document_id, "status_changed", {"to": "ready"}
            )
            await self.session.commit()
        except Exception as exc:
            await self._fail_document(document_id, "EMBEDDING_ERROR", str(exc))
            await self.session.commit()
            raise

        logger.info(
            "embed_chunks_done", document_id=document_id, embedded_count=embedded_count
        )
        return {"document_id": document_id, "embedded_count": embedded_count}

    async def _claim_chunking_running(self, document_id: str) -> bool:
        result = cast(CursorResult, await self.session.execute(
            text(
                """
                UPDATE app.documents
                   SET status = 'chunking_running', updated_at = NOW()
                 WHERE id = :id AND status = 'layout_done'
                RETURNING id
                """
            ),
            {"id": document_id},
        ))
        return result.rowcount > 0

    async def _claim_embedding_running(self, document_id: str) -> bool:
        result = cast(CursorResult, await self.session.execute(
            text(
                """
                UPDATE app.documents
                   SET status = 'embedding_running', updated_at = NOW()
                 WHERE id = :id AND status IN ('chunking_done', 'embedding_running')
                RETURNING id
                """
            ),
            {"id": document_id},
        ))
        return result.rowcount > 0

    async def _claim_ocr_running(self, document_id: str) -> bool:
        """Atomically transition to ocr_running only if not already in a terminal/active state.
        Returns True if claimed, False if another worker already owns it."""
        result = cast(CursorResult, await self.session.execute(
            text(
                """
                UPDATE app.documents
                   SET status = 'ocr_running', updated_at = NOW()
                 WHERE id = :id
                   AND status NOT IN (
                       'ocr_running', 'ocr_done',
                       'layout_running', 'layout_done',
                       'chunking_running', 'chunking_done',
                       'embedding_running', 'ready'
                   )
                RETURNING id
                """
            ),
            {"id": document_id},
        ))
        return result.rowcount > 0

    async def _set_doc_status(self, document_id: str, status: str) -> None:
        await self.session.execute(
            text(
                "UPDATE app.documents SET status = :s, updated_at = NOW() WHERE id = :id"
            ),
            {"s": status, "id": document_id},
        )

    async def delete_document(self, document_id: str) -> dict[str, Any]:
        """Delete a document and every artifact derived from it.

        FK cascades cover app.pages → app.spans, app.blocks, app.chunks, and
        app.document_events. We additionally drop jobs.jobs rows for this doc
        (no FK), the uploaded blob on disk (safe: sha256 is unique to one doc),
        and any cached page images.

        If any job is currently running for this document, we signal
        cooperative cancellation (jobs.jobs.cancel_requested = TRUE), poll up
        to ~5s for the worker to bail out between page iterations, then issue
        the DELETE with a short ``lock_timeout``. If the worker is still
        holding an FK row lock past that, the API returns 409 ConflictError
        with a Retry-After hint rather than hanging or erroring opaquely.
        """
        import shutil

        from app.core.errors import ConflictError
        from app.jobs.queue import JobQueue as _JobQueue
        from sqlalchemy.exc import DBAPIError, OperationalError

        doc = await self.get_document(document_id)
        if doc is None:
            raise IngestError(f"Document {document_id} not found", code="DOCUMENT_NOT_FOUND")

        queue = _JobQueue(self.session)
        flagged = await queue.request_cancel_for_document(document_id)
        await self.session.commit()

        if flagged:
            # Poll up to ~5s for any running job to exit (cancel + commit cycle).
            for _ in range(50):
                still_running = await self.session.execute(
                    text(
                        "SELECT 1 FROM jobs.jobs "
                        "WHERE payload->>'document_id' = :id AND status = 'running' LIMIT 1"
                    ),
                    {"id": document_id},
                )
                if still_running.fetchone() is None:
                    break
                await asyncio.sleep(0.1)

        # Bound the DELETE wait so a worker mid-INSERT can't pin the request forever.
        try:
            await self.session.execute(text("SET LOCAL lock_timeout = '3s'"))
            await self.session.execute(
                text("DELETE FROM jobs.jobs WHERE payload->>'document_id' = :id"),
                {"id": document_id},
            )
            await self.session.execute(
                text("DELETE FROM app.documents WHERE id = :id"), {"id": document_id}
            )
            await self.session.commit()
        except (OperationalError, DBAPIError) as exc:
            await self.session.rollback()
            msg = str(exc).lower()
            if "lock_timeout" in msg or "lock timeout" in msg or "canceling statement" in msg:
                logger.warning(
                    "ingest_delete_locked",
                    document_id=document_id,
                    error=str(exc),
                )
                raise ConflictError(
                    "Document is still being processed; cancellation requested. "
                    "Retry in a few seconds."
                ) from exc
            raise

        if doc.mime_type:
            try:
                blob_path = self.store.path_for(doc.sha256, ext_for(doc.mime_type))
                blob_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning(
                    "ingest_delete_blob_failed",
                    document_id=document_id,
                    sha256=doc.sha256,
                    error=str(exc),
                )

        page_image_dir = self.store.page_image_dir / document_id
        if page_image_dir.exists():
            try:
                shutil.rmtree(page_image_dir, ignore_errors=True)
            except Exception as exc:
                logger.warning(
                    "ingest_delete_page_images_failed",
                    document_id=document_id,
                    error=str(exc),
                )

        logger.info(
            "ingest_deleted",
            document_id=document_id,
            sha256=doc.sha256,
            filename=doc.filename,
        )
        return {"document_id": document_id, "deleted": True}

    async def retry_document(
        self,
        document_id: str,
        *,
        force: bool = False,
        provider_override: str | None = None,
    ) -> dict[str, Any]:
        """Reset a document and re-enqueue it at the start of the pipeline.

        Wipes intermediate artifacts (pages → spans cascade, blocks, chunks) so
        the re-run produces a clean state instead of duplicating rows. The dedup
        key gets a UUID suffix because the original `ocr:{doc_id}` row still
        exists in `jobs.jobs` (completed or failed) and the unique constraint
        would otherwise block re-enqueue.

        With ``force=True``, allow retry from any terminal state
        (``ready``, ``failed``) so an operator can unstick a doc that reached
        ``ready`` with zero blocks.

        ``provider_override`` ('pdfplumber'|'paddleocr'|'auto'|None): when set
        to 'pdfplumber' or 'paddleocr', the OCR routing forces that engine for
        every page on this rerun. 'auto' or None clears any prior override and
        restores per-page automatic routing.
        """
        if provider_override is not None and provider_override not in (
            "pdfplumber",
            "paddleocr",
            "auto",
        ):
            raise IngestError(
                f"Invalid provider_override '{provider_override}'; "
                "expected 'pdfplumber', 'paddleocr', or 'auto'.",
                code="INVALID_PROVIDER_OVERRIDE",
                retryable=False,
            )

        doc = await self.get_document(document_id)
        if doc is None:
            raise IngestError(f"Document {document_id} not found", code="DOCUMENT_NOT_FOUND")

        terminal_states = {"failed", "ready"} if force else {"failed"}
        if doc.status not in terminal_states:
            raise IngestError(
                f"Cannot retry document in status '{doc.status}'; "
                f"retryable states are {sorted(terminal_states)}",
                code="NOT_RETRYABLE",
                retryable=False,
            )
        if force and doc.status != "failed":
            logger.warning(
                "ingest_retry_forced",
                document_id=document_id,
                previous_status=doc.status,
            )

        # Cascades: deleting pages drops spans; deleting documents would drop
        # everything but we want to keep the document row itself.
        await self.session.execute(
            text("DELETE FROM app.chunks WHERE document_id = :id"), {"id": document_id}
        )
        await self.session.execute(
            text("DELETE FROM app.blocks WHERE document_id = :id"), {"id": document_id}
        )
        await self.session.execute(
            text("DELETE FROM app.pages WHERE document_id = :id"), {"id": document_id}
        )

        # Drop any prior job rows for this document so downstream stages —
        # which enqueue with stable dedup keys like `layout:{doc_id}` — aren't
        # silently de-duplicated against rows from the failed run.
        await self.session.execute(
            text(
                "DELETE FROM jobs.jobs WHERE payload->>'document_id' = :id"
            ),
            {"id": document_id},
        )

        # Persist the override so the worker reads it inside ocr_document.
        # 'auto' / None resets to NULL so the routing is automatic again.
        new_override = (
            None if provider_override in (None, "auto") else provider_override
        )
        await self.session.execute(
            text(
                """
                UPDATE app.documents
                   SET status = 'uploaded',
                       error_code = NULL,
                       error_message = NULL,
                       page_count = NULL,
                       embedded_at = NULL,
                       ocr_provider_override = :override,
                       updated_at = NOW()
                 WHERE id = :id
                """
            ),
            {"id": document_id, "override": new_override},
        )
        await DocumentEventBus.emit(
            self.session, document_id, "status_changed", {"to": "uploaded"}
        )

        await self.queue.enqueue(
            kind=JobKind.OCR.value,
            payload={"document_id": document_id},
            dedup_key=f"ocr:{document_id}",
        )
        await self.session.commit()

        logger.info(
            "ingest_retried",
            document_id=document_id,
            previous_error_code=doc.error_code,
            provider_override=new_override,
        )
        return {
            "document_id": document_id,
            "status": "uploaded",
            "ocr_provider_override": new_override,
        }

    async def reextract_page(
        self,
        document_id: str,
        page_num: int,
        *,
        provider: str,
    ) -> dict[str, Any]:
        """Re-OCR a single page with the chosen engine, then re-run downstream.

        Wipes only the spans for the target page (other pages keep their OCR),
        but does drop blocks/chunks/embeddings for the whole document because
        layout/chunking/embedding cross page boundaries — they have to re-run.
        After this returns, the worker will:

            1. OCR just ``page_num`` using ``provider`` (single-page mode).
            2. Re-run layout for the whole doc.
            3. Re-run chunking for the whole doc.
            4. Re-embed every chunk.

        OCR is the expensive step and only runs for the one page; the rest is
        seconds-to-minutes total even on a 60-page document.
        """
        if provider not in ("pdfplumber", "paddleocr"):
            raise IngestError(
                f"Invalid provider '{provider}'; expected 'pdfplumber' or 'paddleocr'.",
                code="INVALID_PROVIDER_OVERRIDE",
                retryable=False,
            )

        doc = await self.get_document(document_id)
        if doc is None:
            raise IngestError(f"Document {document_id} not found", code="DOCUMENT_NOT_FOUND")
        if doc.status not in ("ready", "failed"):
            raise IngestError(
                f"Cannot re-extract page in status '{doc.status}'; "
                "document must be 'ready' or 'failed'.",
                code="NOT_REEXTRACTABLE",
                retryable=False,
            )

        # Locate the page row + ensure it exists.
        page_row = await self.session.execute(
            text(
                "SELECT id FROM app.pages "
                "WHERE document_id = :doc AND page_number = :pn"
            ),
            {"doc": document_id, "pn": page_num},
        )
        page = page_row.fetchone()
        if page is None:
            raise IngestError(
                f"Page {page_num} not found for document {document_id}",
                code="PAGE_NOT_FOUND",
                retryable=False,
            )

        # Wipe the spans for the target page; OCR will repopulate.
        await self.session.execute(
            text("DELETE FROM app.spans WHERE page_id = :pid"),
            {"pid": page.id},
        )
        # Reset that page's status so _ocr_one_page's INSERT…ON CONFLICT path
        # finds it but the page is correctly marked as in-progress.
        await self.session.execute(
            text(
                """
                UPDATE app.pages
                   SET status = 'pending',
                       ocr_provider_override = :override
                 WHERE id = :pid
                """
            ),
            {"pid": page.id, "override": provider},
        )
        # Drop downstream artifacts — they cross page boundaries, so they all
        # need to be regenerated once the new spans are in.
        await self.session.execute(
            text("DELETE FROM app.chunks WHERE document_id = :id"), {"id": document_id}
        )
        await self.session.execute(
            text("DELETE FROM app.blocks WHERE document_id = :id"), {"id": document_id}
        )
        # Reset doc-level status to 'ocr_done' so the layout claim succeeds
        # after the single-page OCR completes. Other pages keep their spans.
        await self.session.execute(
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
            self.session, document_id, "status_changed", {"to": "ocr_done"}
        )

        # Drop any prior job rows for this document so the downstream cascade
        # (layout → chunking → embedding) — which all use stable dedup keys
        # like `chunking:{doc_id}` — isn't silently de-duplicated against the
        # rows from the original ingestion. This was the "stuck on chunking"
        # bug: layout ran fine because we used a unique key, but its enqueue
        # of chunking collided with the completed row and was silently dropped.
        await self.session.execute(
            text("DELETE FROM jobs.jobs WHERE payload->>'document_id' = :id"),
            {"id": document_id},
        )

        # Enqueue OCR with single-page payload. Unique dedup_key per re-extract
        # so it doesn't collide with the original `ocr:{doc_id}` row.
        from app.core.ids import new_uuid7
        dedup = f"ocr:{document_id}:p{page_num}:{new_uuid7()[:8]}"
        await self.queue.enqueue(
            kind=JobKind.OCR.value,
            payload={"document_id": document_id, "page_num": page_num},
            dedup_key=dedup,
        )
        await self.session.commit()

        logger.info(
            "ingest_page_reextract_enqueued",
            document_id=document_id,
            page=page_num,
            provider=provider,
        )
        return {
            "document_id": document_id,
            "page_num": page_num,
            "provider": provider,
            "status": "ocr_done",
        }

    async def _fail_document(self, document_id: str, code: str, message: str) -> None:
        await self.session.execute(
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
            {"id": document_id, "code": code, "msg": message},
        )
        await DocumentEventBus.emit(
            self.session,
            document_id,
            "failed",
            {"error_code": code, "error_message": message},
        )
