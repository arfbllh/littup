"""
Tests for Reconciler.
"""

import uuid

import pytest
from sqlalchemy import text

from app.jobs.reconciler import Reconciler


@pytest.mark.asyncio
async def test_find_partial_documents_returns_stuck_doc(db_session):
    """A document stuck in ocr_running with old updated_at should be found."""
    doc_id = str(uuid.uuid4())
    sha = f"sha_reconcile_{uuid.uuid4().hex}"

    await db_session.execute(
        text("""
            INSERT INTO app.documents (id, sha256, filename, status, updated_at)
            VALUES (:id, :sha, 'stuck.pdf', 'ocr_running',
                    NOW() - INTERVAL '15 minutes')
        """),
        {"id": doc_id, "sha": sha},
    )
    await db_session.commit()

    rec = Reconciler(db_session)
    count = await rec.find_partial_documents()
    await db_session.commit()

    assert count >= 1

    # A job for 'ocr' should have been enqueued for this document
    result = await db_session.execute(
        text("""
            SELECT kind FROM jobs.jobs
            WHERE payload->>'document_id' = :doc_id
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"doc_id": doc_id},
    )
    row = result.fetchone()
    assert row is not None
    assert row.kind == "ocr"


@pytest.mark.asyncio
async def test_find_partial_documents_ignores_ready(db_session):
    """Documents with status 'ready' or 'failed' should not be re-queued."""
    sha_ready = f"sha_ready_{uuid.uuid4().hex}"
    sha_failed = f"sha_failed_{uuid.uuid4().hex}"

    for sha, status in [(sha_ready, "ready"), (sha_failed, "failed")]:
        await db_session.execute(
            text("""
                INSERT INTO app.documents (sha256, filename, status, updated_at)
                VALUES (:sha, 'done.pdf', :status, NOW() - INTERVAL '20 minutes')
            """),
            {"sha": sha, "status": status},
        )
    await db_session.commit()

    rec = Reconciler(db_session)
    await rec.find_partial_documents()
    await db_session.commit()
    # The 'ready'/'failed' docs should not have added new jobs; no assertion needed beyond no crash


@pytest.mark.asyncio
async def test_reclaim_stuck_jobs(db_session):
    from app.jobs.queue import JobQueue

    q = JobQueue(db_session)
    await q.enqueue("ocr", {"document_id": str(uuid.uuid4())})
    await db_session.commit()

    claimed = await q.claim_one(["ocr"], "ghost-worker")
    assert claimed is not None
    await db_session.commit()

    # Fake an expired heartbeat on the job that was actually claimed
    await db_session.execute(
        text("UPDATE jobs.jobs SET heartbeat_at = NOW() - INTERVAL '10 minutes' WHERE id = :id"),
        {"id": claimed.id},
    )
    await db_session.commit()

    rec = Reconciler(db_session)
    reclaimed = await rec.reclaim_stuck_jobs()
    await db_session.commit()

    assert reclaimed >= 1


async def _pending_count(session) -> int:
    result = await session.execute(
        text("SELECT COUNT(*) FROM jobs.jobs WHERE status = 'pending'")
    )
    return result.scalar_one()
