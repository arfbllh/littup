"""
Tests for JobQueue:
- Enqueue + claim with two workers → each job claimed exactly once
- Complete a job
- Fail a job with retry logic
- Stale reclaim: simulated dead worker re-queues its job
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.queue import JobQueue


async def _fresh_queue(session: AsyncSession) -> JobQueue:
    return JobQueue(session)


@pytest.mark.asyncio
async def test_enqueue_and_claim(db_session):
    q = JobQueue(db_session)
    job = await q.enqueue("ocr", {"document_id": str(uuid.uuid4())})
    await db_session.commit()

    claimed = await q.claim_one(["ocr"], "worker-1")
    await db_session.commit()

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert claimed.worker_id == "worker-1"


@pytest.mark.asyncio
async def test_two_workers_exclusive_claim(db_session, db_engine):
    """Two workers racing for one job — only one should get it (SKIP LOCKED)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    q0 = JobQueue(db_session)
    job = await q0.enqueue("ocr", {"document_id": str(uuid.uuid4())})
    await db_session.commit()

    async with factory() as s1, factory() as s2:
        q1, q2 = JobQueue(s1), JobQueue(s2)
        c1 = await q1.claim_one(["ocr"], "worker-A")
        c2 = await q2.claim_one(["ocr"], "worker-B")
        await s1.commit()
        await s2.commit()

    claimed = [c for c in (c1, c2) if c is not None]
    assert len(claimed) == 1, f"Expected exactly 1 worker to claim the job, got {len(claimed)}"
    assert claimed[0].id == job.id


@pytest.mark.asyncio
async def test_complete_job(db_session):
    q = JobQueue(db_session)
    job = await q.enqueue("layout", {"document_id": str(uuid.uuid4())})
    await db_session.commit()

    await q.claim_one(["layout"], "worker-1")
    await db_session.commit()

    await q.complete(job.id, {"blocks": 42})
    await db_session.commit()

    result = await db_session.execute(
        text("SELECT status, result FROM jobs.jobs WHERE id = :id"), {"id": job.id}
    )
    row = result.fetchone()
    assert row.status == "completed"
    assert row.result["blocks"] == 42


@pytest.mark.asyncio
async def test_fail_job_retryable(db_session):
    q = JobQueue(db_session)
    job = await q.enqueue("chunking", {"document_id": str(uuid.uuid4())}, max_attempts=3)
    await db_session.commit()

    await q.claim_one(["chunking"], "worker-1")
    await db_session.commit()

    # First failure — should go back to pending (attempts=1, max=3)
    await q.fail(job.id, "timeout", retryable=True)
    await db_session.commit()

    result = await db_session.execute(
        text("SELECT status, attempts FROM jobs.jobs WHERE id = :id"), {"id": job.id}
    )
    row = result.fetchone()
    assert row.status == "pending"
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_fail_job_non_retryable(db_session):
    q = JobQueue(db_session)
    job = await q.enqueue("embedding", {"document_id": str(uuid.uuid4())})
    await db_session.commit()

    await q.claim_one(["embedding"], "worker-1")
    await db_session.commit()

    await q.fail(job.id, "unrecoverable", retryable=False)
    await db_session.commit()

    result = await db_session.execute(
        text("SELECT status FROM jobs.jobs WHERE id = :id"), {"id": job.id}
    )
    assert result.fetchone().status == "failed"


@pytest.mark.asyncio
async def test_stale_reclaim(db_session):
    """Simulate a dead worker: claim a job then update heartbeat to old timestamp.
    reclaim_stuck should re-queue it."""
    q = JobQueue(db_session)
    job = await q.enqueue("ocr", {"document_id": str(uuid.uuid4())})
    await db_session.commit()

    await q.claim_one(["ocr"], "dead-worker")
    await db_session.commit()

    # Fake an old heartbeat (simulate the worker dying 5 minutes ago)
    await db_session.execute(
        text("""
            UPDATE jobs.jobs
            SET heartbeat_at = NOW() - INTERVAL '5 minutes'
            WHERE id = :id
        """),
        {"id": job.id},
    )
    await db_session.commit()

    reclaimed = await q.reclaim_stuck(timeout_seconds=120)
    await db_session.commit()

    assert reclaimed >= 1

    result = await db_session.execute(
        text("SELECT status, worker_id FROM jobs.jobs WHERE id = :id"), {"id": job.id}
    )
    row = result.fetchone()
    assert row.status == "pending"
    assert row.worker_id is None


@pytest.mark.asyncio
async def test_pending_count(db_session):
    q = JobQueue(db_session)
    before = await q.pending_count()

    await q.enqueue("ocr", {"document_id": str(uuid.uuid4())})
    await q.enqueue("ocr", {"document_id": str(uuid.uuid4())})
    await db_session.commit()

    after = await q.pending_count()
    assert after >= before + 2


@pytest.mark.asyncio
async def test_dedup_key_prevents_double_enqueue(db_session):
    q = JobQueue(db_session)
    dedup = f"dedup-{uuid.uuid4()}"
    await q.enqueue("ocr", {"x": 1}, dedup_key=dedup)
    await q.enqueue("ocr", {"x": 2}, dedup_key=dedup)
    await db_session.commit()

    # Both return a job object but only one row should exist
    result = await db_session.execute(
        text("SELECT COUNT(*) FROM jobs.jobs WHERE dedup_key = :dk"), {"dk": dedup}
    )
    assert result.scalar_one() == 1
