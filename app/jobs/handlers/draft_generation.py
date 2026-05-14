"""Job handler for DRAFT_GENERATION jobs."""
from __future__ import annotations

import structlog
import structlog.contextvars

from app.core.errors import DraftError
from app.jobs.kinds import HANDLERS, JobKind

log = structlog.get_logger(__name__)


async def handle_draft_generation(payload: dict, session) -> dict:
    draft_id = payload["draft_id"]
    template_id = payload["template_id"]
    document_ids = payload["document_ids"]
    trace_id = payload.get("trace_id")

    # Bind trace_id into structlog contextvars so engine/router logs carry it
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    log.info(
        "draft_generation.started",
        draft_id=draft_id,
        template_id=template_id,
        trace_id=trace_id,
    )

    try:
        from app.api.deps import get_draft_engine

        engine = await get_draft_engine()
        await engine.generate(
            draft_id=draft_id,
            template_id=template_id,
            document_ids=document_ids,
            trace_id=trace_id,
        )
    except DraftError:
        raise
    except Exception as exc:
        log.error(
            "draft_generation.unexpected_error",
            draft_id=draft_id,
            error=str(exc),
            exc_info=True,
        )
        raise DraftError(
            str(exc), code="DRAFT_GENERATION_UNEXPECTED", retryable=False
        ) from exc

    log.info("draft_generation.completed", draft_id=draft_id)
    return {"draft_id": draft_id, "status": "ready"}


HANDLERS[JobKind.DRAFT_GENERATION] = handle_draft_generation
