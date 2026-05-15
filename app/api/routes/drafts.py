"""Draft generation routes."""
from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.drafts import (
    CitationView,
    DraftCreateRequest,
    DraftCreateResponse,
    DraftListResponse,
    DraftResponse,
    DraftSummary,
    SectionView,
)
from app.core.errors import ConflictError, NotFoundError
from app.db.models.chunk import Chunk
from app.db.models.document import Document
from app.db.models.draft import Citation, Draft, Section
from app.db.models.edit import Edit
from app.db.session import get_session
from app.draft.draft_repo import DraftRepo
from app.jobs.kinds import JobKind
from app.jobs.queue import JobQueue
from app.settings import settings

router = APIRouter(prefix="/api/drafts", tags=["drafts"])

log = structlog.get_logger(__name__)


async def _build_draft_response(draft_id: str, session: AsyncSession) -> DraftResponse:
    """Build a DraftResponse from the current DB state. Re-usable by multiple routes."""
    result = await session.execute(select(Draft).where(Draft.id == draft_id))
    draft = result.scalar_one_or_none()
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} not found", code="DRAFT_NOT_FOUND")

    sections_result = await session.execute(
        select(Section).where(Section.draft_id == draft_id)
    )
    sections = sections_result.scalars().all()

    sections_groundedness: dict = (draft.ai_output or {}).get("sections_groundedness", {})

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
                groundedness=sections_groundedness.get(sec.name),
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

    count_result = await session.execute(
        select(func.count()).select_from(Edit).where(Edit.draft_id == draft_id)
    )
    edit_count = count_result.scalar_one()

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
        groundedness_score=float(draft.groundedness_score) if draft.groundedness_score is not None else None,
        edit_count=int(edit_count),
        error=error_info,
        document_ids=[str(d) for d in (draft.document_ids or [])],
        extra_instructions=draft.extra_instructions,
    )


@router.get("", response_model=DraftListResponse)
async def list_drafts(
    session: AsyncSession = Depends(get_session),
) -> DraftListResponse:
    # Per-draft edit count via correlated subquery — keeps the response shape
    # consistent with GET /api/drafts/{id} (which also surfaces edit_count).
    edit_count_sq = (
        select(func.count())
        .select_from(Edit)
        .where(Edit.draft_id == Draft.id)
        .correlate(Draft)
        .scalar_subquery()
    )
    result = await session.execute(
        select(Draft, edit_count_sq.label("edit_count"))
        .order_by(Draft.created_at.desc())
        .limit(200)
    )
    rows = result.all()

    items: list[DraftSummary] = []
    for draft, edit_count in rows:
        ai_output = draft.ai_output or {}
        items.append(
            DraftSummary(
                draft_id=str(draft.id),
                template_id=draft.template_id,
                template_version=draft.template_version,
                status=draft.status,
                document_count=len(draft.document_ids or []),
                model_used=draft.model_used,
                cost_usd=float(draft.cost_usd) if draft.cost_usd is not None else None,
                groundedness_score=(
                    float(draft.groundedness_score)
                    if draft.groundedness_score is not None
                    else None
                ),
                edit_count=int(edit_count or 0),
                error_code=ai_output.get("error_code") if draft.status == "failed" else None,
                generated_at=draft.generated_at,
                created_at=draft.created_at,
                has_extra_instructions=bool(draft.extra_instructions),
            )
        )
    return DraftListResponse(items=items)


@router.post("", response_model=DraftCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_draft(
    body: DraftCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> DraftCreateResponse:
    document_ids = [str(doc_id) for doc_id in body.document_ids]

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

    try:
        import structlog.contextvars as sv
        trace_id = sv.get_contextvars().get("request_id")
    except Exception:
        trace_id = None

    repo = DraftRepo(session)
    draft_id = await repo.create_queued(
        body.template_id, document_ids, extra_instructions=body.extra_instructions
    )

    q = JobQueue(session)
    await q.enqueue(
        JobKind.DRAFT_GENERATION,
        {
            "draft_id": draft_id,
            "template_id": body.template_id,
            "document_ids": document_ids,
            "extra_instructions": body.extra_instructions,
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
    return await _build_draft_response(draft_id, session)


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(
    draft_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    # Cancel any in-flight generation first and commit so the worker's cancel
    # watcher sees the flag. Pending jobs are flipped to 'cancelled'; running
    # jobs get cancel_requested=TRUE and stop at the next handler checkpoint.
    queue = JobQueue(session)
    await queue.request_cancel_for_draft(draft_id)
    await session.commit()

    # Sections, citations, and edits cascade via ON DELETE CASCADE in the schema.
    result = await session.execute(delete(Draft).where(Draft.id == draft_id))
    if result.rowcount == 0:
        raise NotFoundError(f"Draft {draft_id} not found", code="DRAFT_NOT_FOUND")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class RevalidateResponse(BaseModel):
    draft_id: str
    revalidated: int
    by_status: dict[str, int]


@router.post("/{draft_id}/revalidate", response_model=RevalidateResponse)
async def revalidate_draft(
    draft_id: str,
    session: AsyncSession = Depends(get_session),
) -> RevalidateResponse:
    """Re-run Pass-3 validator for citations marked stale/unchecked.

    Cheap: only flagged citations are re-checked, not the whole draft. The
    block-edit cascade marks citations as ``stale`` when their underlying
    chunk text changes; this endpoint clears the flag.
    """
    from app.api.deps import get_llm_router
    from app.draft.validator import CitationValidator, _apply_report_to_citations

    result = await session.execute(select(Draft).where(Draft.id == draft_id))
    draft = result.scalar_one_or_none()
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} not found", code="DRAFT_NOT_FOUND")

    sections_result = await session.execute(
        select(Section).where(Section.draft_id == draft_id)
    )
    sections = sections_result.scalars().all()

    llm_router = await get_llm_router()
    validator = CitationValidator(llm_router)

    by_status: dict[str, int] = {}
    revalidated_total = 0

    class _CitDraft:
        """Tiny adapter matching the shape ``_apply_report_to_citations`` expects."""

        def __init__(self, citation: Citation) -> None:
            self._c = citation
            self.chunk_id = str(citation.chunk_id)
            self.validation_status = citation.validation_status
            self.validation_reason = citation.validation_reason

        def commit(self) -> None:
            self._c.validation_status = self.validation_status
            self._c.validation_reason = self.validation_reason

    for sec in sections:
        cit_rows = await session.execute(
            select(Citation).where(
                Citation.section_id == sec.id,
                Citation.validation_status.in_(("stale", "unchecked")),
            )
        )
        flagged = cit_rows.scalars().all()
        if not flagged:
            continue

        chunk_ids = [c.chunk_id for c in flagged]
        chunks_result = await session.execute(
            select(Chunk).where(Chunk.id.in_(chunk_ids))
        )
        chunks_by_id = {str(c.id): c for c in chunks_result.scalars().all()}

        adapters = [_CitDraft(c) for c in flagged]
        report = await validator.validate_section(
            sec.ai_text or "",
            adapters,
            chunks_by_id,
            draft.prompt_fingerprint,
            None,
        )
        _apply_report_to_citations(report, adapters)
        for ad in adapters:
            ad.commit()
            revalidated_total += 1
            by_status[ad.validation_status] = by_status.get(ad.validation_status, 0) + 1

    await session.commit()
    log.info(
        "draft_revalidated",
        draft_id=draft_id,
        revalidated=revalidated_total,
        by_status=by_status,
    )
    return RevalidateResponse(
        draft_id=draft_id,
        revalidated=revalidated_total,
        by_status=by_status,
    )


@router.post("/{draft_id}/sections/{section_name}/regenerate", response_model=SectionView)
async def regenerate_section(
    draft_id: str,
    section_name: str,
    session: AsyncSession = Depends(get_session),
) -> SectionView:
    from app.api.deps import get_draft_engine

    result = await session.execute(select(Draft).where(Draft.id == draft_id))
    draft = result.scalar_one_or_none()
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} not found", code="DRAFT_NOT_FOUND")
    # Widened from status != 'ready' to allow regenerate after edit
    if draft.status not in ("ready", "edited"):
        raise ConflictError(
            f"Draft {draft_id} is not ready (status={draft.status})",
            code="DRAFT_NOT_READY",
        )

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
            engine.regenerate_section(draft_id, section_name, trace_id, session=session),
            timeout=settings.DRAFT_REGENERATE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise ConflictError(
            "Section regeneration timed out", code="REGENERATE_TIMEOUT"
        ) from exc

    # Reload the updated section — use a fresh query so we see the committed data
    await session.rollback()
    sec_result2 = await session.execute(
        select(Section).where(Section.draft_id == draft_id, Section.name == section_name)
    )
    sec = sec_result2.scalar_one()
    cit_result2 = await session.execute(
        select(Citation).where(Citation.section_id == sec.id)
    )
    citations_updated = cit_result2.scalars().all()

    draft_result2 = await session.execute(select(Draft).where(Draft.id == draft_id))
    draft_updated = draft_result2.scalar_one()
    updated_sections_gnd: dict = (draft_updated.ai_output or {}).get("sections_groundedness", {})

    return SectionView(
        name=sec.name,
        text=sec.ai_text,
        target_length_min=sec.target_length_min,
        target_length_max=sec.target_length_max,
        groundedness=updated_sections_gnd.get(section_name),
        citations=[
            CitationView(
                chunk_id=str(c.chunk_id),
                claim_span_start=c.claim_span_start,
                claim_span_end=c.claim_span_end,
                validation_status=c.validation_status,
                validation_reason=c.validation_reason,
            )
            for c in citations_updated
        ],
    )
