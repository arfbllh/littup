"""Draft generation routes (M7)."""
from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.drafts import (
    CitationView,
    DraftCreateRequest,
    DraftCreateResponse,
    DraftResponse,
    SectionView,
)
from app.core.errors import ConflictError, NotFoundError
from app.db.models.document import Document
from app.db.models.draft import Citation, Draft, Section
from app.db.session import get_session
from app.draft.draft_repo import DraftRepo
from app.jobs.kinds import JobKind
from app.jobs.queue import JobQueue
from app.settings import settings

router = APIRouter(prefix="/api/drafts", tags=["drafts"])

log = structlog.get_logger(__name__)


@router.post("", response_model=DraftCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_draft(
    body: DraftCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> DraftCreateResponse:
    document_ids = [str(doc_id) for doc_id in body.document_ids]

    # Validate all documents exist and are status='ready'
    if document_ids:
        rows = (
            await session.execute(
                select(Document.id, Document.status).where(
                    Document.id.in_(document_ids)
                )
            )
        ).all()
        found_ids = {str(r.id) for r in rows}
        for doc_id in document_ids:
            if doc_id not in found_ids:
                raise NotFoundError(f"Document {doc_id} not found", code="DOCUMENT_NOT_FOUND")
            doc_status = next(r.status for r in rows if str(r.id) == doc_id)
            if doc_status != "ready":
                raise ConflictError(
                    f"Document {doc_id} is not ready (status={doc_status})",
                    code="DOCS_NOT_READY",
                )

    # Read trace_id from structlog context (bound by RequestIDMiddleware)
    try:
        import structlog.contextvars as sv
        trace_id = sv.get_contextvars().get("request_id")
    except Exception:
        trace_id = None

    repo = DraftRepo(session)
    draft_id = await repo.create_queued(body.template_id, document_ids)

    q = JobQueue(session)
    await q.enqueue(
        JobKind.DRAFT_GENERATION,
        {
            "draft_id": draft_id,
            "template_id": body.template_id,
            "document_ids": document_ids,
            "trace_id": trace_id,
        },
    )
    await session.commit()

    return DraftCreateResponse(draft_id=draft_id, status="queued")


@router.get("/{draft_id}", response_model=DraftResponse)
async def get_draft(
    draft_id: str,
    session: AsyncSession = Depends(get_session),
) -> DraftResponse:
    result = await session.execute(select(Draft).where(Draft.id == draft_id))
    draft = result.scalar_one_or_none()
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} not found", code="DRAFT_NOT_FOUND")

    # Load sections + citations
    sections_result = await session.execute(
        select(Section).where(Section.draft_id == draft_id)
    )
    sections = sections_result.scalars().all()

    section_views: list[SectionView] = []
    for sec in sections:
        cit_result = await session.execute(
            select(Citation).where(Citation.section_id == sec.id)
        )
        citations = cit_result.scalars().all()
        section_views.append(
            SectionView(
                name=sec.name,
                text=sec.ai_text,
                target_length_min=sec.target_length_min,
                target_length_max=sec.target_length_max,
                citations=[
                    CitationView(
                        chunk_id=str(c.chunk_id),
                        claim_span_start=c.claim_span_start,
                        claim_span_end=c.claim_span_end,
                        validation_status=c.validation_status,
                        validation_reason=c.validation_reason,
                    )
                    for c in citations
                ],
            )
        )

    ai_output = draft.ai_output or {}
    error_info = None
    if draft.status == "failed":
        error_info = {
            "error_code": ai_output.get("error_code"),
            "error_message": ai_output.get("error_message"),
        }

    return DraftResponse(
        draft_id=str(draft.id),
        template_id=draft.template_id,
        template_version=draft.template_version,
        prompt_fingerprint=draft.prompt_fingerprint,
        status=draft.status,
        fields=ai_output.get("fields"),
        sections=section_views,
        validators=ai_output.get("validators"),
        generated_at=draft.generated_at,
        model_used=draft.model_used,
        cost_usd=float(draft.cost_usd) if draft.cost_usd is not None else None,
        error=error_info,
    )


@router.post("/{draft_id}/sections/{section_name}/regenerate", response_model=SectionView)
async def regenerate_section(
    draft_id: str,
    section_name: str,
    session: AsyncSession = Depends(get_session),
) -> SectionView:
    from app.api.deps import get_draft_engine

    # Validate draft exists and is ready
    result = await session.execute(select(Draft).where(Draft.id == draft_id))
    draft = result.scalar_one_or_none()
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} not found", code="DRAFT_NOT_FOUND")
    if draft.status != "ready":
        raise ConflictError(
            f"Draft {draft_id} is not ready (status={draft.status})",
            code="DRAFT_NOT_READY",
        )

    # Validate section exists
    sec_result = await session.execute(
        select(Section).where(Section.draft_id == draft_id, Section.name == section_name)
    )
    if sec_result.scalar_one_or_none() is None:
        raise NotFoundError(
            f"Section '{section_name}' not found in draft {draft_id}",
            code="SECTION_NOT_FOUND",
        )

    try:
        import structlog.contextvars as sv
        trace_id = sv.get_contextvars().get("request_id")
    except Exception:
        trace_id = None

    engine = await get_draft_engine()

    try:
        await asyncio.wait_for(
            engine.regenerate_section(draft_id, section_name, trace_id),
            timeout=settings.DRAFT_REGENERATE_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        raise ConflictError(
            "Section regeneration timed out", code="REGENERATE_TIMEOUT"
        ) from exc

    # Reload the updated section — use a fresh query so we see the committed data
    await session.rollback()  # end the prior read transaction; next query starts fresh
    sec_result2 = await session.execute(
        select(Section).where(Section.draft_id == draft_id, Section.name == section_name)
    )
    sec = sec_result2.scalar_one()
    cit_result2 = await session.execute(
        select(Citation).where(Citation.section_id == sec.id)
    )
    citations = cit_result2.scalars().all()

    return SectionView(
        name=sec.name,
        text=sec.ai_text,
        target_length_min=sec.target_length_min,
        target_length_max=sec.target_length_max,
        citations=[
            CitationView(
                chunk_id=str(c.chunk_id),
                claim_span_start=c.claim_span_start,
                claim_span_end=c.claim_span_end,
                validation_status=c.validation_status,
                validation_reason=c.validation_reason,
            )
            for c in citations
        ],
    )
