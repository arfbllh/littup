"""Edit capture routes."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.drafts import _build_draft_response
from app.api.schemas.drafts import DraftResponse
from app.api.schemas.edits import EditCreateRequest, EditMetricsResponse
from app.core.trace import current_trace_id as _current_trace_id
from app.db.session import get_session
from app.edits.service import EditService
from app.jobs.queue import JobQueue
from app.settings import settings

router = APIRouter(tags=["edits"])

log = structlog.get_logger(__name__)


@router.post("/api/drafts/{draft_id}/edit", response_model=DraftResponse)
async def save_edit(
    draft_id: str,
    body: EditCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> DraftResponse:
    from app.api.deps import get_template_registry

    service = EditService(
        session=session,
        queue=JobQueue(session),
        registry=get_template_registry(),
    )
    await service.save_edit(draft_id, body.final_output, trace_id=_current_trace_id())
    await session.commit()
    return await _build_draft_response(draft_id, session)


@router.get("/api/templates/{template_id}/edit-metrics", response_model=EditMetricsResponse)
async def edit_metrics(
    template_id: str,
    days: int = Query(default=None, gt=0, le=365),
    session: AsyncSession = Depends(get_session),
) -> EditMetricsResponse:
    from app.api.deps import get_template_registry

    effective_days = days if days is not None else settings.EDIT_METRICS_DEFAULT_DAYS
    service = EditService(
        session=session,
        queue=JobQueue(session),
        registry=get_template_registry(),
    )
    return await service.metrics(template_id, days=effective_days)
