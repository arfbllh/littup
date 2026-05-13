"""HTTP surface for document ingestion (M3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.documents import (
    BlocksResponse,
    DocumentList,
    DocumentStatus,
    DocumentSummary,
    UploadResponse,
)
from app.api.sse import parse_last_event_id, stream_document_events
from app.core.errors import NotFoundError
from app.db.session import get_session
from app.ingest.mime import ext_for
from app.ingest.page_render import render_page_png
from app.ingest.service import IngestService
from app.ingest.storage import LocalBlobStore

router = APIRouter(prefix="/api/documents", tags=["documents"])


def get_ingest_service(session: AsyncSession = Depends(get_session)) -> IngestService:
    return IngestService(session)


@router.post("", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    svc: IngestService = Depends(get_ingest_service),
) -> UploadResponse:
    result = await svc.upload(file)
    return UploadResponse(
        document_id=result.document_id,
        status=result.status,
        was_new=result.was_new,
        sha256=result.sha256,
    )


@router.get("", response_model=DocumentList)
async def list_documents(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    svc: IngestService = Depends(get_ingest_service),
) -> DocumentList:
    items, next_offset = await svc.list_documents(
        limit=limit, offset=offset, status_filter=status_filter
    )
    return DocumentList(
        items=[
            DocumentSummary(
                document_id=d.id,
                filename=d.filename,
                status=d.status,
                page_count=d.page_count,
                size_bytes=d.size_bytes,
                created_at=d.created_at,
            )
            for d in items
        ],
        next_offset=next_offset,
    )


@router.get("/{document_id}", response_model=DocumentStatus)
async def get_document(
    document_id: str,
    svc: IngestService = Depends(get_ingest_service),
) -> DocumentStatus:
    doc = await svc.get_document(document_id)
    if doc is None:
        raise NotFoundError(f"Document {document_id} not found")
    return DocumentStatus(
        document_id=doc.id,
        sha256=doc.sha256,
        filename=doc.filename,
        mime_type=doc.mime_type,
        size_bytes=doc.size_bytes,
        page_count=doc.page_count,
        status=doc.status,
        last_event_seq=doc.last_event_seq,
        error_code=doc.error_code,
        error_message=doc.error_message,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


@router.get("/{document_id}/blocks", response_model=BlocksResponse)
async def get_blocks(
    document_id: str,
    svc: IngestService = Depends(get_ingest_service),
) -> BlocksResponse:
    payload = await svc.get_blocks(document_id)
    if payload["status"] is None:
        raise NotFoundError(f"Document {document_id} not found")
    return BlocksResponse(status=payload["status"], blocks=payload["blocks"])


@router.get("/{document_id}/pages/{n}")
async def get_page_image(
    document_id: str,
    n: int,
    svc: IngestService = Depends(get_ingest_service),
):
    doc = await svc.get_document(document_id)
    if doc is None:
        raise NotFoundError(f"Document {document_id} not found")
    if (doc.mime_type or "") != "application/pdf":
        raise NotFoundError("Page image rendering only supported for PDFs in M3")

    store = LocalBlobStore()
    cached = store.page_image_path(document_id, n)
    if not cached.exists():
        pdf_path = store.path_for(doc.sha256, ext_for(doc.mime_type))
        if not pdf_path.exists():
            raise NotFoundError(f"Source file for {document_id} missing on disk")
        render_page_png(pdf_path, n, cached)
    return FileResponse(cached, media_type="image/png")


@router.get("/{document_id}/events")
async def stream_events(
    request: Request,
    document_id: str,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    svc: IngestService = Depends(get_ingest_service),
):
    doc = await svc.get_document(document_id)
    if doc is None:
        raise NotFoundError(f"Document {document_id} not found")

    after_seq = parse_last_event_id(last_event_id)
    generator = stream_document_events(request, document_id, after_seq)
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(generator, media_type="text/event-stream", headers=headers)
