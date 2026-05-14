"""EMBEDDING job handler — final ingestion stage, transitions docs to 'ready'."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_embedder
from app.core.errors import IngestError
from app.ingest.service import IngestService
from app.jobs.kinds import HANDLERS, JobKind

logger = structlog.get_logger(__name__)


async def handle_embedding(payload: dict, session: AsyncSession) -> dict:
    doc_id = payload.get("document_id")
    if not doc_id:
        raise IngestError("EMBEDDING job missing document_id", code="EMBEDDING_BAD_PAYLOAD")

    embedder = await get_embedder()
    service = IngestService(session)
    try:
        result = await service.embed_chunks(doc_id, embedder)
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(str(exc), code="EMBEDDING_UNEXPECTED_ERROR") from exc

    logger.info("embedding_done", document_id=doc_id)
    return result


HANDLERS[JobKind.EMBEDDING.value] = handle_embedding
