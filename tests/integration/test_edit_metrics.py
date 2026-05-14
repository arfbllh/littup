"""Integration: GET /api/templates/{id}/edit-metrics returns per-field/section rates."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

_TEMPLATE_ID = "case_fact_summary"


@pytest_asyncio.fixture
async def _setup(test_session_factory):
    from app.core.ids import new_uuid7
    from app.draft.templates.registry import TemplateRegistry
    from app.settings import settings

    registry = TemplateRegistry()
    registry.load_from_disk(settings.TEMPLATES_DIR)
    async with test_session_factory() as s:
        synced = await registry.sync_to_db(s)
        await s.commit()
    entry = next(e for e in synced if e["id"] == _TEMPLATE_ID)

    async with test_session_factory() as s:
        # 5 drafts
        draft_ids = [new_uuid7() for _ in range(5)]
        for did in draft_ids:
            await s.execute(
                text("""
                    INSERT INTO app.drafts
                        (id, template_id, template_version, prompt_fingerprint,
                         status, generated_at, created_at)
                    VALUES (:id, :tid, :ver, :fp, 'ready', NOW(), NOW() - INTERVAL '2 days')
                """),
                {"id": did, "tid": _TEMPLATE_ID, "ver": entry["version"], "fp": entry["fingerprint"]},
            )

        # parties edited in draft 0 and draft 1 (2 edits)
        for did in draft_ids[:2]:
            eid = new_uuid7()
            await s.execute(
                text("""
                    INSERT INTO app.edits
                        (id, draft_id, template_id, template_version, prompt_fingerprint,
                         field_or_section_name, field_type, ai_value, user_value, diff, context)
                    VALUES (:id, :did, :tid, :ver, :fp, 'parties', 'field',
                            CAST(:ai AS jsonb), CAST(:user AS jsonb),
                            CAST('{}' AS jsonb), CAST('{}' AS jsonb))
                """),
                {
                    "id": eid, "did": did, "tid": _TEMPLATE_ID,
                    "ver": entry["version"], "fp": entry["fingerprint"],
                    "ai": json.dumps({"value": []}), "user": json.dumps({"value": []}),
                },
            )

        # factual_background section edited in draft 2 (1 edit)
        eid2 = new_uuid7()
        await s.execute(
            text("""
                INSERT INTO app.edits
                    (id, draft_id, template_id, template_version, prompt_fingerprint,
                     field_or_section_name, field_type, ai_value, user_value, diff, context)
                VALUES (:id, :did, :tid, :ver, :fp, 'factual_background', 'section',
                        CAST(:ai AS jsonb), CAST(:user AS jsonb),
                        CAST('{}' AS jsonb), CAST('{}' AS jsonb))
            """),
            {
                "id": eid2, "did": draft_ids[2], "tid": _TEMPLATE_ID,
                "ver": entry["version"], "fp": entry["fingerprint"],
                "ai": json.dumps({"text": "old"}), "user": json.dumps({"text": "new"}),
            },
        )
        await s.commit()

    yield registry, draft_ids


@pytest.mark.asyncio
async def test_metrics_per_field_rates(
    _setup, test_session_factory, cleanup_drafts_and_jobs
):
    registry, draft_ids = _setup

    from app.edits.service import EditService
    from app.jobs.queue import JobQueue

    async with test_session_factory() as s:
        service = EditService(session=s, queue=JobQueue(s), registry=registry)
        resp = await service.metrics(_TEMPLATE_ID, days=30)

    assert resp.template_id == _TEMPLATE_ID
    assert resp.drafts_count == 5

    parties_row = next(f for f in resp.fields if f.name == "parties")
    assert parties_row.edited_count == 2
    assert abs(parties_row.edit_rate - 0.4) < 1e-9

    bg_row = next(s for s in resp.sections if s.name == "factual_background")
    assert bg_row.edited_count == 1
    assert abs(bg_row.edit_rate - 0.2) < 1e-9

    # Fields with zero edits have rate=0 (not null) when drafts_count > 0
    jurisdiction_row = next(f for f in resp.fields if f.name == "jurisdiction")
    assert jurisdiction_row.edited_count == 0
    assert jurisdiction_row.edit_rate == 0.0


@pytest.mark.asyncio
async def test_metrics_null_rates_when_no_drafts(
    _setup, test_session_factory, cleanup_drafts_and_jobs
):
    """When drafts_count == 0, all field and section rates must be None (not 0)."""
    registry, _ = _setup

    from app.edits.service import EditService
    from app.jobs.queue import JobQueue

    # _setup inserts case_fact_summary drafts with created_at = NOW() - 2 days.
    # days=1 means cutoff = NOW() - 1 day → all 5 seeded drafts are excluded → drafts_count == 0.
    async with test_session_factory() as s:
        service = EditService(session=s, queue=JobQueue(s), registry=registry)
        resp = await service.metrics(_TEMPLATE_ID, days=1)

    assert resp.drafts_count == 0
    # case_fact_summary has 5 fields — the loop must run at least once
    assert len(resp.fields) > 0, "Expected fields from case_fact_summary template"
    for f in resp.fields:
        assert f.edit_rate is None, f"Expected None for field {f.name!r} when drafts_count=0, got {f.edit_rate}"
    for sec in resp.sections:
        assert sec.edit_rate is None, f"Expected None for section {sec.name!r} when drafts_count=0, got {sec.edit_rate}"


@pytest.mark.asyncio
async def test_metrics_all_fields_and_sections_present(
    _setup, test_session_factory, cleanup_drafts_and_jobs
):
    """Response includes ALL template fields and sections, even those with zero edits."""
    registry, _ = _setup

    from app.edits.service import EditService
    from app.jobs.queue import JobQueue

    async with test_session_factory() as s:
        service = EditService(session=s, queue=JobQueue(s), registry=registry)
        resp = await service.metrics(_TEMPLATE_ID, days=30)

    field_names = {f.name for f in resp.fields}
    assert {"parties", "jurisdiction", "filing_date", "claims", "damages_sought"} == field_names

    section_names = {s.name for s in resp.sections}
    assert {"procedural_history", "factual_background", "key_issues"} == section_names
