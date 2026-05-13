"""IngestService — orchestrates upload → atomic upsert → file commit → enqueue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RateLimitError
from app.ingest.events import DocumentEventBus, DocumentEventRow
from app.ingest.hashing import HashedUpload, stream_to_tempfile
from app.ingest.mime import detect_mime, ext_for
from app.ingest.storage import LocalBlobStore
from app.jobs.kinds import JobKind
from app.jobs.queue import JobQueue
from app.settings import settings

logger = structlog.get_logger(__name__)


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

            doc_id, was_new = await self._atomic_upsert(
                sha256=hashed.sha256,
                filename=filename,
                mime=mime,
                size=hashed.size_bytes,
            )

            if was_new:
                final_path = self.store.path_for(hashed.sha256, ext)
                self.store.commit(hashed.path, final_path)
                hashed = HashedUpload(  # tempfile now lives at final_path
                    path=final_path, sha256=hashed.sha256, size_bytes=hashed.size_bytes
                )
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
                logger.info(
                    "ingest_uploaded",
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
            was_new=was_new,
        )

    async def _atomic_upsert(
        self, *, sha256: str, filename: str, mime: str, size: int
    ) -> tuple[str, bool]:
        """NN-2: single statement returning (id, was_new) without races."""
        result = await self.session.execute(
            text(
                """
                INSERT INTO app.documents (sha256, filename, mime_type, size_bytes, status)
                VALUES (:sha, :name, :mime, :size, 'uploaded')
                ON CONFLICT (sha256) DO UPDATE
                   SET last_accessed_at = NOW()
                RETURNING id, (xmax = 0) AS inserted
                """
            ),
            {"sha": sha256, "name": filename, "mime": mime, "size": size},
        )
        row = result.fetchone()
        if row is None:
            # Should be impossible — DO UPDATE always returns a row.
            raise RuntimeError("atomic upsert returned no row")
        return str(row.id), bool(row.inserted)

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
        """Empty until M5; we still surface the document's status."""
        doc = await self.get_document(document_id)
        if doc is None:
            return {"status": None, "blocks": []}
        return {"status": doc.status, "blocks": []}

    async def replay_events(self, document_id: str, after_seq: int) -> list[DocumentEventRow]:
        return await DocumentEventBus.replay(self.session, document_id, after_seq)

    def upload_path(self, sha256: str, ext: str):
        return self.store.path_for(sha256, ext)
