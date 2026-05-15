"""HTTP surface for document ingestion."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
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
from app.ingest.block_edits import edit_block_text
from app.ingest.mime import ext_for
from app.ingest.page_render import render_page_png, render_text_to_png
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
                has_blocks=d.has_blocks,
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
        ocr_provider_override=doc.ocr_provider_override,
        ocr_provider_used=doc.ocr_provider_used,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: str,
    svc: IngestService = Depends(get_ingest_service),
) -> Response:
    await svc.delete_document(document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{document_id}/pages/{page_num}/reextract", response_model=DocumentStatus
)
async def reextract_page(
    document_id: str,
    page_num: int,
    provider: str = Query(
        ...,
        description="OCR engine for this page: 'pdfplumber' or 'paddleocr'.",
    ),
    svc: IngestService = Depends(get_ingest_service),
) -> DocumentStatus:
    await svc.reextract_page(document_id, page_num, provider=provider)
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
        ocr_provider_override=doc.ocr_provider_override,
        ocr_provider_used=doc.ocr_provider_used,
    )


@router.post("/{document_id}/retry", response_model=DocumentStatus)
async def retry_document(
    document_id: str,
    force: bool = Query(False, description="Force retry from non-failed terminal states (e.g. 'ready')."),
    provider: str | None = Query(
        None,
        description="OCR engine override: 'pdfplumber', 'paddleocr', or 'auto' to clear a prior override.",
    ),
    svc: IngestService = Depends(get_ingest_service),
) -> DocumentStatus:
    await svc.retry_document(document_id, force=force, provider_override=provider)
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
        ocr_provider_override=doc.ocr_provider_override,
        ocr_provider_used=doc.ocr_provider_used,
    )


# ─── Re-extract sessions (operator preview / accept-reject) ───────────────────


class ReextractStartRequest(BaseModel):
    pages: list[int] = Field(..., min_length=1, description="1-based page numbers to re-OCR")
    provider: str = Field(..., description="'pdfplumber' or 'paddleocr'")


class ReextractAcceptRequest(BaseModel):
    pages: list[int] | None = Field(
        default=None,
        description="Subset of session pages to apply. None = apply every successful page.",
    )


class ReextractPageView(BaseModel):
    page_number: int
    page_id: str
    provider: str
    old_text: str
    new_text: str
    status: str
    error_message: str | None = None


class ReextractSessionView(BaseModel):
    session_id: str
    document_id: str
    provider: str
    page_numbers: list[int]
    status: str
    error_message: str | None = None
    pages: list[ReextractPageView] = Field(default_factory=list)


@router.post(
    "/{document_id}/reextract-sessions", response_model=ReextractSessionView
)
async def start_reextract_session(
    document_id: str,
    body: ReextractStartRequest,
    svc: IngestService = Depends(get_ingest_service),
) -> ReextractSessionView:
    from app.ingest.reextract_session import get_session, start_session

    await start_session(
        svc.session,
        document_id=document_id,
        page_numbers=body.pages,
        provider=body.provider,
        queue=svc.queue,
    )
    # Return the freshly-created row (status=pending, no pages yet) so the UI
    # has the session_id immediately. It then polls GET to watch it run.
    # We re-look-up to include any defaults set by the DB.
    sessions = await svc.session.execute(
        text(
            "SELECT id FROM app.reextract_sessions WHERE document_id = :doc "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"doc": document_id},
    )
    s = sessions.fetchone()
    if s is None:
        raise NotFoundError("Session row not visible after insert; retry shortly.")
    view = await get_session(svc.session, session_id=str(s.id))
    if view is None:
        raise NotFoundError(f"Session {s.id} not found")
    return ReextractSessionView(**_session_view_payload(view))


@router.get(
    "/{document_id}/reextract-sessions/{session_id}",
    response_model=ReextractSessionView,
)
async def get_reextract_session(
    document_id: str,
    session_id: str,
    svc: IngestService = Depends(get_ingest_service),
) -> ReextractSessionView:
    from app.ingest.reextract_session import get_session

    view = await get_session(svc.session, session_id=session_id)
    if view is None or str(view["document_id"]) != document_id:
        raise NotFoundError(f"Session {session_id} not found for document {document_id}")
    return ReextractSessionView(**_session_view_payload(view))


@router.post(
    "/{document_id}/reextract-sessions/{session_id}/accept",
    response_model=ReextractSessionView,
)
async def accept_reextract_session(
    document_id: str,
    session_id: str,
    body: ReextractAcceptRequest | None = None,
    svc: IngestService = Depends(get_ingest_service),
) -> ReextractSessionView:
    from app.ingest.reextract_session import accept_session, get_session

    await accept_session(
        svc.session,
        session_id=session_id,
        accepted_pages=(body.pages if body else None),
        queue=svc.queue,
    )
    view = await get_session(svc.session, session_id=session_id)
    if view is None:
        raise NotFoundError(f"Session {session_id} not found")
    return ReextractSessionView(**_session_view_payload(view))


@router.post(
    "/{document_id}/reextract-sessions/{session_id}/reject",
    response_model=ReextractSessionView,
)
async def reject_reextract_session(
    document_id: str,  # noqa: ARG001 — kept for symmetry / route grouping
    session_id: str,
    svc: IngestService = Depends(get_ingest_service),
) -> ReextractSessionView:
    from app.ingest.reextract_session import get_session, reject_session

    await reject_session(svc.session, session_id=session_id)
    view = await get_session(svc.session, session_id=session_id)
    if view is None:
        raise NotFoundError(f"Session {session_id} not found")
    return ReextractSessionView(**_session_view_payload(view))


def _session_view_payload(view: dict) -> dict:
    return {
        "session_id": view["session_id"],
        "document_id": view["document_id"],
        "provider": view["provider"],
        "page_numbers": view["page_numbers"],
        "status": view["status"],
        "error_message": view.get("error_message"),
        "pages": [
            ReextractPageView(
                page_number=p["page_number"],
                page_id=p["page_id"],
                provider=p["provider"],
                old_text=p["old_text"],
                new_text=p["new_text"],
                status=p["status"],
                error_message=p.get("error_message"),
            )
            for p in view.get("pages", [])
        ],
    }


class BlockEditRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)


class BlockEditResponse(BaseModel):
    block_id: str
    affected_chunks: int
    stale_citations: int
    no_change: bool = False


@router.patch(
    "/{document_id}/blocks/{block_id}",
    response_model=BlockEditResponse,
)
async def patch_block_text(
    document_id: str,
    block_id: str,
    body: BlockEditRequest,
    session: AsyncSession = Depends(get_session),
) -> BlockEditResponse:
    """WS-E: replace a block's transcript and cascade through chunks + citations."""
    result = await edit_block_text(
        document_id=document_id,
        block_id=block_id,
        new_text=body.text,
        session=session,
    )
    return BlockEditResponse(**{"block_id": block_id, **result})


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

    mime = (doc.mime_type or "").lower()
    store = LocalBlobStore()

    # ── Raster image uploads ─────────────────────────────────────────────
    # PNG/JPEG/WEBP: source file is the page image — serve it directly with
    # its original MIME. TIFF isn't natively renderable in browsers, so
    # convert to PNG once and cache. Image uploads are always single-page.
    if mime in ("image/png", "image/jpeg", "image/webp", "image/tiff"):
        if n != 1:
            raise NotFoundError(f"Document {document_id} has only one page")
        src = store.path_for(doc.sha256, ext_for(mime))
        if not src.exists():
            raise NotFoundError(f"Source file for {document_id} missing on disk")
        if mime == "image/tiff":
            cached = store.page_image_path(document_id, n)
            if not cached.exists():
                cached.parent.mkdir(parents=True, exist_ok=True)
                from PIL import Image  # Pillow ships with the OCR stack
                with Image.open(src) as im:
                    im.convert("RGB").save(cached, format="PNG")
            return FileResponse(cached, media_type="image/png")
        return FileResponse(src, media_type=mime)

    # ── Text uploads (.txt / .md / .json): rasterise text to PNG so the
    # existing PageWithBboxes UI component can render a preview.
    if mime in ("text/plain", "text/markdown", "application/json"):
        if n != 1:
            raise NotFoundError(f"Document {document_id} has only one page")
        src = store.path_for(doc.sha256, ext_for(mime))
        if not src.exists():
            raise NotFoundError(f"Source file for {document_id} missing on disk")
        cached = store.page_image_path(document_id, n)
        if not cached.exists():
            render_text_to_png(src, cached)
        return FileResponse(cached, media_type="image/png")

    if mime != "application/pdf":
        raise NotFoundError(f"Preview unavailable for mime type {mime!r}")

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
