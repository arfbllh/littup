"""Integration: replace_section correctly recomputes groundedness_score in Postgres.

Requires a running Postgres 16 instance:
    TEST_DATABASE_URL=postgresql+asyncpg://littup:littup@localhost:5432/littup_test
    pytest tests/integration/test_replace_section_groundedness.py

Exercises the jsonb AVG recomputation SQL that cannot be tested with mocks.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def draft_with_sections(db_session):
    """Insert a draft with two sections and known per-section groundedness."""
    from app.core.ids import new_uuid7

    draft_id = new_uuid7()
    section_a_id = new_uuid7()
    section_b_id = new_uuid7()

    sections_gnd = {"section_a": 0.8, "section_b": 0.6}
    initial_gnd_score = sum(sections_gnd.values()) / len(sections_gnd)  # 0.7

    ai_output = json.dumps({
        "fields": {},
        "sections_text": {"section_a": "Text A.", "section_b": "Text B."},
        "validators": [],
        "retrieval_meta": {"dangling_citations": 0},
        "sections_groundedness": sections_gnd,
    })

    await db_session.execute(text("""
        INSERT INTO app.drafts
            (id, template_id, template_version, prompt_fingerprint,
             status, ai_output, groundedness_score)
        VALUES
            (:id, 'test-tmpl', 1, 'fp',
             'ready', CAST(:ai_output AS jsonb), :gnd)
    """), {"id": draft_id, "ai_output": ai_output, "gnd": initial_gnd_score})

    for sec_id, sec_name in [(section_a_id, "section_a"), (section_b_id, "section_b")]:
        await db_session.execute(text("""
            INSERT INTO app.sections (id, draft_id, name, ai_text)
            VALUES (:id, :draft_id, :name, :text)
        """), {"id": sec_id, "draft_id": draft_id, "name": sec_name, "text": f"Text {sec_name}."})

    await db_session.flush()

    yield {
        "draft_id": draft_id,
        "section_a_id": section_a_id,
        "section_b_id": section_b_id,
    }

    # Cleanup
    await db_session.execute(text("DELETE FROM app.sections WHERE draft_id=:id"), {"id": draft_id})
    await db_session.execute(text("DELETE FROM app.drafts WHERE id=:id"), {"id": draft_id})
    await db_session.flush()


@pytest.mark.asyncio
async def test_replace_section_recomputes_groundedness(draft_with_sections, db_session):
    """After replace_section, groundedness_score = AVG of all section groundedness values."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.draft.draft_repo import DraftRepo
    from app.draft.generator import CitationDraft

    draft_id = draft_with_sections["draft_id"]

    # Replace section_a with a new groundedness of 1.0
    # sections_gnd was {section_a: 0.8, section_b: 0.6}
    # After update: {section_a: 1.0, section_b: 0.6} → avg = 0.8
    new_section_gnd = 1.0
    expected_draft_gnd = (1.0 + 0.6) / 2  # 0.8

    repo = DraftRepo(db_session)
    await repo.replace_section(
        draft_id=draft_id,
        section_name="section_a",
        new_text="Updated text for section A.",
        new_citations=[],
        section_groundedness=new_section_gnd,
    )
    await db_session.flush()

    row = (await db_session.execute(
        text("SELECT groundedness_score, ai_output FROM app.drafts WHERE id=:id"),
        {"id": draft_id},
    )).one()

    actual_gnd = float(row.groundedness_score)
    assert abs(actual_gnd - expected_draft_gnd) < 0.001, (
        f"Expected groundedness_score≈{expected_draft_gnd}, got {actual_gnd}"
    )

    ai_output = row.ai_output
    sections_gnd = ai_output.get("sections_groundedness", {})
    assert abs(float(sections_gnd["section_a"]) - 1.0) < 0.001
    assert abs(float(sections_gnd["section_b"]) - 0.6) < 0.001


@pytest.mark.asyncio
async def test_replace_section_null_groundedness_excluded_from_avg(
    draft_with_sections, db_session
):
    """A section with null groundedness is excluded from the AVG computation."""
    import json as _json
    from app.draft.draft_repo import DraftRepo

    draft_id = draft_with_sections["draft_id"]

    # Patch section_b groundedness to null in ai_output
    await db_session.execute(text("""
        UPDATE app.drafts
        SET ai_output = jsonb_set(ai_output, '{sections_groundedness,section_b}', 'null'::jsonb)
        WHERE id = :id
    """), {"id": draft_id})
    await db_session.flush()

    # Now replace section_a with groundedness 0.5
    # sections_gnd: {section_a: 0.5, section_b: null}
    # AVG should be 0.5 (null excluded)
    repo = DraftRepo(db_session)
    await repo.replace_section(
        draft_id=draft_id,
        section_name="section_a",
        new_text="Updated again.",
        new_citations=[],
        section_groundedness=0.5,
    )
    await db_session.flush()

    row = (await db_session.execute(
        text("SELECT groundedness_score FROM app.drafts WHERE id=:id"),
        {"id": draft_id},
    )).one()

    actual_gnd = float(row.groundedness_score)
    assert abs(actual_gnd - 0.5) < 0.001, (
        f"Expected groundedness_score≈0.5 (null section excluded), got {actual_gnd}"
    )
