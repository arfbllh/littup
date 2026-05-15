"""page_reextract job handler — runs the OCR for an operator session.

The handler is intentionally thin: ``run_session`` in
``app.ingest.reextract_session`` does the orchestration.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IngestError
from app.ingest.reextract_session import run_session
from app.jobs.kinds import HANDLERS, JobKind

logger = structlog.get_logger(__name__)


async def handle_page_reextract(payload: dict, session: AsyncSession) -> dict:
    session_id = payload.get("session_id")
    if not session_id:
        raise IngestError(
            "page_reextract job payload missing session_id",
            code="REEXTRACT_BAD_PAYLOAD",
        )
    return await run_session(session, session_id=session_id)


HANDLERS[JobKind.PAGE_REEXTRACT.value] = handle_page_reextract
