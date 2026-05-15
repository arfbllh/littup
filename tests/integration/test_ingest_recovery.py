"""Reconciler recovery for stuck documents with idempotent re-enqueue."""

import uuid

import pytest
from sqlalchemy import text

from app.jobs.reconciler import Reconciler


@pytest.mark.asyncio
async def test_stuck_doc_is_requeued_idempotently(cleanup_documents_and_jobs, db_session):
    doc_id = str(uuid.uuid4())
    sha = f"sha_recover_{uuid.uuid4().hex}"

    await db_session.execute(
        text(
            """
            INSERT INTO app.documents (id, sha256, filename, status, updated_at)
            VALUES (:id, :sha, 'stuck.pdf', 'ocr_running',
                    NOW() - INTERVAL '20 minutes')
            """
        ),
        {"id": doc_id, "sha": sha},
    )
    await db_session.commit()

    rec = Reconciler(db_session)
    await rec.find_partial_documents()
    await db_session.commit()

    jobs = await db_session.execute(
        text(
            """
            SELECT id, kind, dedup_key FROM jobs.jobs
            WHERE payload->>'document_id' = :id
            """
        ),
        {"id": doc_id},
    )
    rows = jobs.fetchall()
    assert len(rows) == 1
    assert rows[0].kind == "ocr"
    assert rows[0].dedup_key == f"reconcile:{doc_id}:ocr"

    # Second sweep must not enqueue a duplicate (dedup_key prevents it).
    await rec.find_partial_documents()
    await db_session.commit()
    jobs2 = await db_session.execute(
        text("SELECT COUNT(*) FROM jobs.jobs WHERE payload->>'document_id' = :id"),
        {"id": doc_id},
    )
    assert jobs2.scalar_one() == 1
