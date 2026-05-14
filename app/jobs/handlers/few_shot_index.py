"""FEW_SHOT_INDEX job handler — embeds an Edit row for few-shot retrieval."""
from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_embedder
from app.core.errors import EditError
from app.edits.few_shot_store import FewShotStore
from app.jobs.kinds import HANDLERS, JobKind

logger = structlog.get_logger(__name__)


async def handle_few_shot_index(payload: dict, session: AsyncSession) -> dict:
    edit_id = payload.get("edit_id")
    if not edit_id:
        raise EditError(
            "FEW_SHOT_INDEX job missing edit_id",
            code="FEW_SHOT_INDEX_BAD_PAYLOAD",
            retryable=False,
        )

    embedder = await get_embedder()
    # Raises EditError(EMBEDDER_DIM_MISMATCH, retryable=False) if dim != 1024
    store = FewShotStore(embedder=embedder)

    try:
        await store.index(edit_id, session=session)
    except EditError:
        raise
    except Exception as exc:
        raise EditError(
            str(exc),
            code="FEW_SHOT_INDEX_FAILED",
            retryable=True,
        ) from exc

    logger.info("few_shot.indexed", edit_id=edit_id)
    return {"edit_id": edit_id, "status": "indexed"}


HANDLERS[JobKind.FEW_SHOT_INDEX] = handle_few_shot_index
