import json

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_uuid7
from app.db.models.job import Job

logger = structlog.get_logger(__name__)


class JobQueue:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def _fetch_job(self, job_id: str) -> Job | None:
        """Fetch a Job by id, forcing a fresh DB read (populate_existing bypasses identity-map cache)."""
        result = await self._session.execute(
            select(Job).where(Job.id == job_id).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        dedup_key: str | None = None,
        max_attempts: int = 3,
    ) -> Job:
        job_id = new_uuid7()
        result = await self._session.execute(
            text("""
                INSERT INTO jobs.jobs (id, kind, payload, status, max_attempts, dedup_key)
                VALUES (:id, :kind, CAST(:payload AS jsonb), 'pending', :max_attempts, :dedup_key)
                ON CONFLICT (dedup_key) DO NOTHING
                RETURNING id
            """),
            {
                "id": job_id,
                "kind": kind,
                "payload": json.dumps(payload),
                "max_attempts": max_attempts,
                "dedup_key": dedup_key,
            },
        )
        row = result.fetchone()
        if row is None:
            logger.debug("job_deduplicated", kind=kind, dedup_key=dedup_key)
            # Return the already-existing job (matched via dedup_key)
            if dedup_key:
                dk_result = await self._session.execute(
                    text("SELECT id FROM jobs.jobs WHERE dedup_key = :dk"), {"dk": dedup_key}
                )
                existing = dk_result.fetchone()
                if existing:
                    return await self._fetch_job(existing.id)  # type: ignore[return-value]
        else:
            logger.info("job_enqueued", job_id=row.id, kind=kind)

        return await self._fetch_job(job_id)  # type: ignore[return-value]

    async def claim_one(self, allowed_kinds: list[str], worker_id: str) -> Job | None:
        if not allowed_kinds:
            return None
        result = await self._session.execute(
            text("""
                UPDATE jobs.jobs
                SET status       = 'running',
                    picked_up_at = NOW(),
                    heartbeat_at = NOW(),
                    worker_id    = :worker_id,
                    attempts     = attempts + 1,
                    updated_at   = NOW()
                WHERE id = (
                    SELECT id FROM jobs.jobs
                    WHERE  status = 'pending'
                    AND    kind = ANY(:kinds)
                    ORDER  BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT  1
                )
                RETURNING id
            """),
            {"worker_id": worker_id, "kinds": allowed_kinds},
        )
        row = result.fetchone()
        if row is None:
            return None
        # Use _fetch_job (ORM SELECT) to refresh the identity map — session.get() would
        # return the stale cached version if enqueue() already loaded this Job this session.
        job = await self._fetch_job(row.id)
        logger.info("job_claimed", job_id=row.id, worker_id=worker_id)
        return job

    async def heartbeat(self, job_id: str, worker_id: str) -> None:
        result = await self._session.execute(
            text("""
                UPDATE jobs.jobs
                SET heartbeat_at = NOW(), updated_at = NOW()
                WHERE id = :job_id AND worker_id = :worker_id AND status = 'running'
                RETURNING id
            """),
            {"job_id": job_id, "worker_id": worker_id},
        )
        if result.fetchone() is None:
            logger.warning("heartbeat_mismatch", job_id=job_id, worker_id=worker_id)

    async def _write_history(self, job_id: str, kind: str, status: str, worker_id: str | None = None, error: str | None = None) -> None:
        await self._session.execute(
            text("""
                INSERT INTO jobs.job_history (job_id, kind, status, worker_id, error)
                VALUES (:job_id, :kind, :status, :worker_id, :error)
            """),
            {"job_id": job_id, "kind": kind, "status": status, "worker_id": worker_id, "error": error},
        )

    async def complete(self, job_id: str, result_json: dict | None = None) -> None:
        result = await self._session.execute(
            text("""
                UPDATE jobs.jobs
                SET status     = 'completed',
                    result     = CAST(:result AS jsonb),
                    updated_at = NOW()
                WHERE id = :job_id
                RETURNING kind, worker_id
            """),
            {"job_id": job_id, "result": json.dumps(result_json or {})},
        )
        row = result.fetchone()
        if row:
            await self._write_history(job_id, row.kind, "completed", worker_id=row.worker_id)
        logger.info("job_completed", job_id=job_id)

    async def fail(self, job_id: str, error_msg: str, *, retryable: bool = True) -> None:
        if retryable:
            result = await self._session.execute(
                text("""
                    UPDATE jobs.jobs
                    SET status     = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
                        error      = :error,
                        updated_at = NOW()
                    WHERE id = :job_id
                    RETURNING kind, status, worker_id
                """),
                {"job_id": job_id, "error": error_msg},
            )
        else:
            result = await self._session.execute(
                text("""
                    UPDATE jobs.jobs
                    SET status = 'failed', error = :error, updated_at = NOW()
                    WHERE id = :job_id
                    RETURNING kind, status, worker_id
                """),
                {"job_id": job_id, "error": error_msg},
            )
        row = result.fetchone()
        if row:
            await self._write_history(job_id, row.kind, row.status, worker_id=row.worker_id, error=error_msg)
        logger.warning("job_failed", job_id=job_id, retryable=retryable, error=error_msg)

    async def reclaim_stuck(self, timeout_seconds: int = 120) -> int:
        result = await self._session.execute(
            text("""
                UPDATE jobs.jobs
                SET status       = 'pending',
                    picked_up_at = NULL,
                    heartbeat_at = NULL,
                    worker_id    = NULL,
                    updated_at   = NOW()
                WHERE status = 'running'
                  AND heartbeat_at < NOW() - (:timeout * INTERVAL '1 second')
                RETURNING id
            """),
            {"timeout": timeout_seconds},
        )
        rows = result.fetchall()
        if rows:
            logger.warning("jobs_reclaimed", count=len(rows), ids=[r.id for r in rows])
        return len(rows)

    async def pending_count(self) -> int:
        result = await self._session.execute(
            text("SELECT COUNT(*) FROM jobs.jobs WHERE status IN ('pending', 'running')")
        )
        return result.scalar_one()
