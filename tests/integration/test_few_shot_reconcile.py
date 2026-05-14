"""Integration: Reconciler re-enqueues unindexed edits with correct dedup semantics."""
from __future__ import annotations

import json
import math

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


class _UniformEmbedder:
    name = "uniform"
    dim = 1024

    async def embed(self, texts):
        val = 1.0 / math.sqrt(1024)
        return [[val] * 1024 for _ in texts]

    async def health(self):
        return True


@pytest_asyncio.fixture
async def _old_unindexed_edit(test_session_factory):
    """Insert an edit created 2 minutes ago with few_shot_indexed_at IS NULL."""
    from app.core.ids import new_uuid7

    draft_id = new_uuid7()
    edit_id = new_uuid7()

    async with test_session_factory() as s:
        await s.execute(
            text("""
                INSERT INTO app.drafts (id, template_id, template_version, prompt_fingerprint, status)
                VALUES (:id, 'case_fact_summary', 1, 'fp', 'ready')
            """),
            {"id": draft_id},
        )
        await s.execute(
            text("""
                INSERT INTO app.edits
                    (id, draft_id, template_id, template_version, prompt_fingerprint,
                     field_or_section_name, field_type, ai_value, user_value, diff, context,
                     created_at)
                VALUES
                    (:id, :did, 'case_fact_summary', 1, 'fp',
                     'parties', 'field',
                     CAST(:ai AS jsonb), CAST(:user AS jsonb),
                     CAST('{}' AS jsonb), CAST('{}' AS jsonb),
                     NOW() - INTERVAL '2 minutes')
            """),
            {
                "id": edit_id, "did": draft_id,
                "ai": json.dumps({"value": []}), "user": json.dumps({"value": []}),
            },
        )
        await s.commit()

    yield edit_id, draft_id


@pytest.mark.asyncio
async def test_reconciler_enqueues_unindexed_edit(
    _old_unindexed_edit, test_session_factory, cleanup_drafts_and_jobs
):
    edit_id, _ = _old_unindexed_edit

    from app.jobs.reconciler import Reconciler

    async with test_session_factory() as s:
        rec = Reconciler(s)
        count = await rec.reconcile_unembedded_edits()
        await s.commit()

    assert count == 1

    async with test_session_factory() as s:
        row = (await s.execute(
            text("SELECT dedup_key, status FROM jobs.jobs WHERE kind = 'few_shot_index'"),
        )).fetchone()

    assert row is not None
    assert row[0] == f"few_shot_index:{edit_id}"
    assert row[1] == "pending"


@pytest.mark.asyncio
async def test_reconciler_deduplicated_on_second_call(
    _old_unindexed_edit, test_session_factory, cleanup_drafts_and_jobs
):
    edit_id, _ = _old_unindexed_edit

    from app.jobs.reconciler import Reconciler

    async with test_session_factory() as s:
        rec = Reconciler(s)
        await rec.reconcile_unembedded_edits()
        await s.commit()

    # Second call — dedup_key collision, no second job
    async with test_session_factory() as s:
        rec = Reconciler(s)
        await rec.reconcile_unembedded_edits()
        await s.commit()

    async with test_session_factory() as s:
        job_count = (await s.execute(
            text("SELECT COUNT(*) FROM jobs.jobs WHERE kind = 'few_shot_index'"),
        )).scalar_one()

    assert job_count == 1


@pytest.mark.asyncio
async def test_reconciler_then_worker_indexes(
    _old_unindexed_edit, test_session_factory, cleanup_drafts_and_jobs
):
    edit_id, _ = _old_unindexed_edit

    from app.jobs.reconciler import Reconciler

    async with test_session_factory() as s:
        rec = Reconciler(s)
        await rec.reconcile_unembedded_edits()
        await s.commit()

    # Simulate the worker executing the job
    from app.edits.few_shot_store import FewShotStore

    embedder = _UniformEmbedder()
    store = FewShotStore(embedder=embedder)
    async with test_session_factory() as s:
        await store.index(edit_id, session=s)
        await s.commit()

    async with test_session_factory() as s:
        row = (await s.execute(
            text("SELECT few_shot_indexed_at FROM app.edits WHERE id = :id"),
            {"id": edit_id},
        )).fetchone()

    assert row[0] is not None
