import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.queue import JobQueue
from app.settings import settings

logger = structlog.get_logger(__name__)

# Maps document status to the job kind needed to move it forward
_NEXT_STAGE: dict[str, str] = {
    "uploaded": "ocr",
    "ocr_pending": "ocr",
    "ocr_running": "ocr",
    "ocr_done": "layout",
    "layout_running": "layout",
    "layout_done": "chunking",
    "chunking_running": "chunking",
    "chunking_done": "embedding",
    "embedding_running": "embedding",
}


class Reconciler:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def reclaim_stuck_jobs(self) -> int:
        """Re-queue running jobs whose heartbeat has expired (NN-1)."""
        queue = JobQueue(self._session)
        count = await queue.reclaim_stuck(timeout_seconds=settings.JOB_STALE_TIMEOUT)
        if count:
            logger.warning("reconciler_reclaimed_jobs", count=count)
        return count

    async def find_partial_documents(self) -> int:
        """Re-enqueue the missing pipeline stage for stuck documents (NN-1)."""
        result = await self._session.execute(
            text("""
                SELECT id, status
                FROM app.documents
                WHERE status NOT IN ('ready', 'failed')
                  AND updated_at < NOW() - INTERVAL '10 minutes'
            """)
        )
        rows = result.fetchall()
        count = 0
        queue = JobQueue(self._session)
        for row in rows:
            next_kind = _NEXT_STAGE.get(row.status)
            if next_kind is None:
                continue
            await queue.enqueue(
                kind=next_kind,
                payload={"document_id": str(row.id)},
                dedup_key=f"reconcile:{row.id}:{next_kind}",
            )
            count += 1
            logger.info("reconciler_requeued", document_id=str(row.id), status=row.status, next_kind=next_kind)
        return count

    async def find_unembedded_edits(self) -> list[str]:
        """Return edit IDs whose few-shot embedding failed (NN-11). Stub — wired in M9."""
        result = await self._session.execute(
            text("""
                SELECT id FROM app.edits
                WHERE few_shot_indexed_at IS NULL
                  AND created_at < NOW() - INTERVAL '1 minute'
                LIMIT 100
            """)
        )
        ids = [str(r.id) for r in result.fetchall()]
        if ids:
            logger.info("reconciler_unembedded_edits", count=len(ids))
        return ids
