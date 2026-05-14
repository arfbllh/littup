"""Integration: POST /api/drafts/{id}/edit with identical output → no Edit rows, draft still marked edited."""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def _seed(test_session_factory):
    from app.core.ids import new_uuid7
    from app.draft.templates.registry import TemplateRegistry
    from app.settings import settings

    registry = TemplateRegistry()
    registry.load_from_disk(settings.TEMPLATES_DIR)
    async with test_session_factory() as s:
        synced = await registry.sync_to_db(s)
        await s.commit()
    entry = next(e for e in synced if e["id"] == "case_fact_summary")
    tid, version, fp = entry["id"], entry["version"], entry["fingerprint"]

    draft_id = new_uuid7()
    ai_output = {
        "fields": {
            "parties": {"value": [{"name": "Smith"}], "supporting_chunk_ids": [], "confidence": 0.9, "error_code": None},
            "jurisdiction": {"value": "SDNY", "supporting_chunk_ids": [], "confidence": 0.8, "error_code": None},
            "filing_date": {"value": None, "supporting_chunk_ids": [], "confidence": 0.0, "error_code": None},
            "claims": {"value": [], "supporting_chunk_ids": [], "confidence": 0.0, "error_code": None},
            "damages_sought": {"value": None, "supporting_chunk_ids": [], "confidence": 0.0, "error_code": None},
        },
        "sections_text": {
            "procedural_history": "Filed January 2025.",
            "factual_background": "Background here.",
            "key_issues": "The main issue.",
        },
        "validators": [],
        "retrieval_meta": {},
        "sections_groundedness": {},
    }
    async with test_session_factory() as s:
        await s.execute(
            text("""
                INSERT INTO app.drafts
                    (id, template_id, template_version, prompt_fingerprint, status, ai_output, generated_at, model_used)
                VALUES (:id, :tid, :ver, :fp, 'ready', CAST(:ao AS jsonb), NOW(), 'mock')
            """),
            {"id": draft_id, "tid": tid, "ver": version, "fp": fp, "ao": json.dumps(ai_output)},
        )
        for sname in ["procedural_history", "factual_background", "key_issues"]:
            await s.execute(
                text("INSERT INTO app.sections (id, draft_id, name, ai_text) VALUES (gen_random_uuid(), :did, :n, :t)"),
                {"did": draft_id, "n": sname, "t": ai_output["sections_text"][sname]},
            )
        await s.commit()

    return draft_id, registry


@pytest.mark.asyncio
async def test_noop_edit_no_rows_draft_still_marked_edited(
    _seed, test_session_factory, cleanup_drafts_and_jobs
):
    draft_id, registry = _seed

    from app.edits.service import EditService
    from app.jobs.queue import JobQueue

    # Submit identical values — should produce zero diff
    user_output = {
        "fields": {"parties": [{"name": "Smith"}]},
        "sections": [{"name": "factual_background", "text": "Background here."}],
    }

    async with test_session_factory() as s:
        service = EditService(session=s, queue=JobQueue(s), registry=registry)
        result = await service.save_edit(draft_id, user_output, trace_id=None)
        await s.commit()

    assert result.edit_ids == []
    assert result.skipped_reason == "no_changes"

    # No Edit rows
    async with test_session_factory() as s:
        count = (await s.execute(
            text("SELECT COUNT(*) FROM app.edits WHERE draft_id = :id"), {"id": draft_id}
        )).scalar_one()
    assert count == 0

    # No FEW_SHOT_INDEX jobs
    async with test_session_factory() as s:
        jobs = (await s.execute(
            text("SELECT COUNT(*) FROM jobs.jobs WHERE kind = 'few_shot_index'")
        )).scalar_one()
    assert jobs == 0

    # Draft still marked edited and edited_at set
    async with test_session_factory() as s:
        row = (await s.execute(
            text("SELECT status, edited_at FROM app.drafts WHERE id = :id"), {"id": draft_id}
        )).fetchone()
    assert row[0] == "edited"
    assert row[1] is not None
