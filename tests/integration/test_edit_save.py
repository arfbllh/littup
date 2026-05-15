"""Integration: POST /api/drafts/{id}/edit saves Edit rows, updates draft status, enqueues jobs."""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def _seed_template(test_session_factory):
    """Ensure case_fact_summary template is in the DB and return (template_id, version, fp)."""
    from app.draft.templates.registry import TemplateRegistry
    from app.settings import settings

    registry = TemplateRegistry()
    registry.load_from_disk(settings.TEMPLATES_DIR)
    async with test_session_factory() as s:
        synced = await registry.sync_to_db(s)
        await s.commit()
    entry = next(e for e in synced if e["id"] == "case_fact_summary")
    return entry["id"], entry["version"], entry["fingerprint"], registry


@pytest_asyncio.fixture
async def _seed_draft(_seed_template, test_session_factory):
    """Insert a ready draft with known ai_output."""
    from app.core.ids import new_uuid7

    tid, version, fp, registry = _seed_template
    draft_id = new_uuid7()
    ai_output = {
        "fields": {
            "parties": {
                "value": [{"name": "Smith", "role": "plaintiff"}],
                "supporting_chunk_ids": ["aaa-bbb"],
                "confidence": 0.9,
                "error_code": None,
            },
            "jurisdiction": {"value": "SDNY", "supporting_chunk_ids": [], "confidence": 0.8, "error_code": None},
            "filing_date": {"value": None, "supporting_chunk_ids": [], "confidence": 0.0, "error_code": None},
            "claims": {"value": ["breach of contract"], "supporting_chunk_ids": [], "confidence": 0.8, "error_code": None},
            "damages_sought": {"value": None, "supporting_chunk_ids": [], "confidence": 0.0, "error_code": None},
        },
        "sections_text": {
            "procedural_history": "Complaint filed January 2025.",
            "factual_background": "Background facts here.",
            "key_issues": "Key disputed issue.",
        },
        "validators": [],
        "retrieval_meta": {},
        "sections_groundedness": {},
    }
    async with test_session_factory() as s:
        await s.execute(
            text("""
                INSERT INTO app.drafts
                    (id, template_id, template_version, prompt_fingerprint, status,
                     ai_output, generated_at, model_used)
                VALUES (:id, :tid, :ver, :fp, 'ready',
                        CAST(:ao AS jsonb), NOW(), 'mock')
            """),
            {
                "id": draft_id, "tid": tid, "ver": version, "fp": fp,
                "ao": json.dumps(ai_output),
            },
        )
        # Insert section rows so citations query doesn't fail
        for sname in ["procedural_history", "factual_background", "key_issues"]:
            from app.core.ids import new_uuid7 as uuid7
            await s.execute(
                text("INSERT INTO app.sections (id, draft_id, name, ai_text) VALUES (:id, :did, :n, :t)"),
                {"id": uuid7(), "did": draft_id, "n": sname, "t": ai_output["sections_text"][sname]},
            )
        await s.commit()

    return draft_id, tid, version, fp, registry


@pytest.mark.asyncio
async def test_edit_save_creates_rows_and_enqueues_jobs(
    _seed_draft, test_session_factory, cleanup_drafts_and_jobs
):
    draft_id, tid, version, fp, registry = _seed_draft

    from app.edits.service import EditService
    from app.jobs.queue import JobQueue

    user_output = {
        "fields": {
            "parties": [{"name": "Smith, et al.", "role": "plaintiff"}],
        },
        "sections": [
            {"name": "factual_background", "text": "Updated background."},
        ],
    }

    async with test_session_factory() as s:
        service = EditService(session=s, queue=JobQueue(s), registry=registry)
        result = await service.save_edit(draft_id, user_output, trace_id="t1")
        await s.commit()

    # 2 Edit rows expected: parties (field) + factual_background (section)
    assert len(result.edit_ids) == 2
    assert result.skipped_reason is None

    async with test_session_factory() as s:
        rows = (await s.execute(
            text("SELECT field_or_section_name, field_type, template_version, prompt_fingerprint FROM app.edits WHERE draft_id = :did"),
            {"did": draft_id},
        )).fetchall()

    names = {r[0] for r in rows}
    assert "parties" in names
    assert "factual_background" in names

    # template_version and prompt_fingerprint must be copied from the draft
    for row in rows:
        assert row[2] == version
        assert row[3] == fp

    # Jobs enqueued
    async with test_session_factory() as s:
        job_rows = (await s.execute(
            text("SELECT payload, dedup_key, max_attempts FROM jobs.jobs WHERE kind = 'few_shot_index'"),
        )).fetchall()

    assert len(job_rows) == 2
    dedup_keys = {r[1] for r in job_rows}
    for edit_id in result.edit_ids:
        assert f"few_shot_index:{edit_id}" in dedup_keys
    assert all(r[2] == 5 for r in job_rows)

    # Draft status updated
    async with test_session_factory() as s:
        d = (await s.execute(text("SELECT status, edited_at FROM app.drafts WHERE id = :id"), {"id": draft_id})).fetchone()
    assert d[0] == "edited"
    assert d[1] is not None


@pytest.mark.asyncio
async def test_edit_save_http_route_returns_edit_count(
    _seed_draft, app_client, cleanup_drafts_and_jobs
):
    """POST /api/drafts/{id}/edit returns DraftResponse with edit_count and status='edited'."""
    draft_id, tid, version, fp, _registry = _seed_draft

    body = {
        "final_output": {
            "fields": {
                "parties": [{"name": "Smith, et al.", "role": "plaintiff"}],
            },
            "sections": [
                {"name": "factual_background", "text": "Updated background."},
            ],
        }
    }

    resp = await app_client.post(f"/api/drafts/{draft_id}/edit", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "edited"
    assert data["edit_count"] == 2


@pytest.mark.asyncio
async def test_edit_save_final_output_merged(
    _seed_draft, test_session_factory, cleanup_drafts_and_jobs
):
    """final_output is a complete merged document regardless of partial user_output."""
    draft_id, tid, version, fp, registry = _seed_draft

    from app.edits.service import EditService
    from app.jobs.queue import JobQueue

    # Only submit parties — jurisdiction should carry forward from ai_output
    user_output = {"fields": {"parties": [{"name": "Smith, et al."}]}, "sections": []}

    async with test_session_factory() as s:
        service = EditService(session=s, queue=JobQueue(s), registry=registry)
        await service.save_edit(draft_id, user_output, trace_id=None)
        await s.commit()

    async with test_session_factory() as s:
        row = (await s.execute(
            text("SELECT final_output FROM app.drafts WHERE id = :id"), {"id": draft_id}
        )).fetchone()
    fo = row[0]
    if isinstance(fo, str):
        fo = json.loads(fo)

    assert fo["fields"]["parties"] == [{"name": "Smith, et al."}]
    assert fo["fields"]["jurisdiction"] == "SDNY"   # carried from ai_output
    # All sections present
    section_names = {s["name"] for s in fo["sections"]}
    assert {"procedural_history", "factual_background", "key_issues"} == section_names
