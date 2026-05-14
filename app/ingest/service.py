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
from app.ingest.mime import PDF, detect_mime, ext_for
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
        # NN-3 backpressure pre-check
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
            mime = detect_mime(hashed.path)
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
        """NN-2: single statement returning (id, was_new, prev_was_failed)."""
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
                SELECT id, sha256, filename, mime_type, size_bytes, page_count,
                       status, last_event_seq, error_code, error_message,
                       created_at, updated_at
                FROM app.documents WHERE id = :id
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
            where = "WHERE status = :status"
            params["status"] = status_filter
        result = await self.session.execute(
            text(
                f"""
                SELECT id, sha256, filename, mime_type, size_bytes, page_count,
                       status, last_event_seq, error_code, error_message,
                       created_at, updated_at
                FROM app.documents {where}
                ORDER BY created_at DESC
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

    # ── OCR orchestration (M4) ────────────────────────────────────────────────

    async def ocr_document(
        self,
        document_id: str,
        *,
        llm_router: "LLMRouter | None" = None,
        config=None,  # OCRConfig | None — pass in tests to override YAML config
    ) -> dict[str, Any]:
        """Drive a document through the OCR state machine.

        Returns a summary dict used as the job result payload.
        State transitions: ocr_pending → ocr_running → ocr_done (or failed).
        """
        from app.ingest.ocr.base import load_ocr_config
        from app.ingest.ocr.pdfplumber_ocr import has_text_layer
        from app.ingest.ocr.paddle_ocr import release_paddle
        from app.ingest.ocr.routing import route_and_extract

        cfg = config if config is not None else load_ocr_config(settings.OCR_CONFIG_PATH)

        # ── Load document record ──────────────────────────────────────────────
        row = await self.session.execute(
            text(
                "SELECT id, sha256, mime_type, status FROM app.documents WHERE id = :id"
            ),
            {"id": document_id},
        )
        doc = row.fetchone()
        if doc is None:
            raise IngestError(f"Document {document_id} not found", code="DOCUMENT_NOT_FOUND")

        if doc.status in ("ocr_done", "ready"):
            return {"skipped": True, "reason": "already_done"}

        # ── ocr_pending ───────────────────────────────────────────────────────
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
        claimed = await self._claim_ocr_running(document_id)
        if not claimed:
            return {"skipped": True, "reason": "already_claimed"}
        await DocumentEventBus.emit(
            self.session, document_id, "status_changed", {"to": "ocr_running"}
        )
        await self.session.commit()

        # ── Determine if this is a native PDF ─────────────────────────────────
        is_pdf = (doc.mime_type or "").lower() == PDF
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
        results: list[dict[str, Any]] = []
        try:
            for page_num in range(1, page_count + 1):
                if page_images is not None:
                    page_img = page_images[page_num - 1]
                elif is_scan_pdf:
                    try:
                        page_img = await self._rasterise_one_page(file_path, page_num)
                    except Exception as exc:
                        results.append({"page": page_num, "status": "failed", "error": str(exc)})
                        continue
                else:
                    page_img = None
                page_result = await self._ocr_one_page(
                    document_id=document_id,
                    page_num=page_num,
                    page_img=page_img,
                    file_path=file_path,
                    has_text_layer=native,
                    cfg=cfg,
                    llm_router=llm_router,
                    route_and_extract=route_and_extract,
                )
                results.append(page_result)
        except Exception as exc:
            await self._fail_document(document_id, "OCR_LOOP_ERROR", str(exc))
            await self.session.commit()
            raise IngestError(str(exc), code="OCR_LOOP_ERROR") from exc
        finally:
            # Always release PaddleOCR memory regardless of success/failure (B-1)
            release_paddle()

        # ── ocr_done ──────────────────────────────────────────────────────────
        await self._set_doc_status(document_id, "ocr_done")
        await DocumentEventBus.emit(
            self.session, document_id, "status_changed", {"to": "ocr_done"}
        )
        await self.session.commit()

        total_spans = sum(r.get("span_count", 0) for r in results)
        vlm_pages = sum(1 for r in results if r.get("source") == "vlm")
        logger.info(
            "ocr_document_done",
            document_id=document_id,
            page_count=page_count,
            total_spans=total_spans,
            vlm_pages=vlm_pages,
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
            return [img]

        # Scanned PDF: return None — caller uses _rasterise_one_page per page
        return None

    async def _rasterise_one_page(self, file_path: Path, page_num: int):
        """Rasterise a single page from a scanned PDF. Frees memory after each call."""
        try:
            from pdf2image import convert_from_path
        except ImportError as exc:
            raise IngestError("pdf2image not installed", code="PDF2IMAGE_MISSING") from exc

        import numpy as np
        import cv2

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
            return cv2.cvtColor(np.array(pages[0]), cv2.COLOR_RGB2BGR)

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

    # ── Layout / Chunking / Embedding orchestration (M5) ──────────────────────

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

        Commits after each batch so a crash mid-document resumes correctly (NN-1):
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
