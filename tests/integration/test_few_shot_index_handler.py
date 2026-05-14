"""Integration: FEW_SHOT_INDEX handler embeds an Edit row and stamps few_shot_indexed_at."""
from __future__ import annotations

import json
import math

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


class _UniformEmbedder:
    """Returns the same 1024-dim unit vector for every input."""
    name = "uniform"
    dim = 1024

    async def embed(self, texts):
        val = 1.0 / math.sqrt(1024)
        return [[val] * 1024 for _ in texts]

    async def health(self):
        return True


@pytest_asyncio.fixture
async def _edit_row(test_session_factory):
    """Insert an unindexed Edit row directly and return its ID."""
    from app.core.ids import new_uuid7

    draft_id = new_uuid7()
    edit_id = new_uuid7()

    async with test_session_factory() as s:
        # Need a draft to satisfy the FK constraint
        await s.execute(
            text("""
                INSERT INTO app.drafts
                    (id, template_id, template_version, prompt_fingerprint, status)
                VALUES (:id, 'case_fact_summary', 1, 'fp_test', 'ready')
            """),
            {"id": draft_id},
        )
        await s.execute(
            text("""
                INSERT INTO app.edits
                    (id, draft_id, template_id, template_version, prompt_fingerprint,
                     field_or_section_name, field_type, ai_value, user_value, diff, context)
                VALUES
                    (:id, :did, 'case_fact_summary', 1, 'fp_test',
                     'parties', 'field',
                     CAST(:ai AS jsonb), CAST(:user AS jsonb),
                     CAST(:diff AS jsonb), CAST(:ctx AS jsonb))
            """),
            {
                "id": edit_id,
                "did": draft_id,
                "ai": json.dumps({"value": [{"name": "Smith"}]}),
                "user": json.dumps({"value": [{"name": "Smith, et al."}]}),
                "diff": json.dumps({"operation": "modified"}),
                "ctx": json.dumps({}),
            },
        )
        await s.commit()

    yield edit_id, draft_id


@pytest.mark.asyncio
async def test_handler_embeds_and_stamps(
    _edit_row, test_session_factory, cleanup_drafts_and_jobs
):
    edit_id, _ = _edit_row
    embedder = _UniformEmbedder()

    from app.edits.few_shot_store import FewShotStore

    store = FewShotStore(embedder=embedder)
    async with test_session_factory() as s:
        await store.index(edit_id, session=s)
        await s.commit()

    async with test_session_factory() as s:
        row = (await s.execute(
            text("SELECT embedding, few_shot_indexed_at FROM app.edits WHERE id = :id"),
            {"id": edit_id},
        )).fetchone()

    assert row is not None
    assert row[0] is not None, "embedding should be set"
    assert row[1] is not None, "few_shot_indexed_at should be set"


@pytest.mark.asyncio
async def test_handler_idempotent(_edit_row, test_session_factory, cleanup_drafts_and_jobs):
    edit_id, _ = _edit_row
    embedder = _UniformEmbedder()

    from app.edits.few_shot_store import FewShotStore

    store = FewShotStore(embedder=embedder)

    # Index twice
    async with test_session_factory() as s:
        await store.index(edit_id, session=s)
        await s.commit()

    async with test_session_factory() as s:
        row1 = (await s.execute(
            text("SELECT few_shot_indexed_at FROM app.edits WHERE id = :id"), {"id": edit_id}
        )).fetchone()

    async with test_session_factory() as s:
        await store.index(edit_id, session=s)
        await s.commit()

    async with test_session_factory() as s:
        row2 = (await s.execute(
            text("SELECT few_shot_indexed_at FROM app.edits WHERE id = :id"), {"id": edit_id}
        )).fetchone()

    # Timestamp must be unchanged
    assert row1[0] == row2[0]


def test_dim_mismatch_raises_at_construction():
    """Constructor raises EditError(EMBEDDER_DIM_MISMATCH) when dim != 1024."""
    from app.core.errors import EditError
    from app.edits.few_shot_store import FewShotStore
    from app.llm.embedder import StubEmbedder

    bad_embedder = StubEmbedder(dim=1536)
    with pytest.raises(EditError) as exc_info:
        FewShotStore(embedder=bad_embedder)

    assert exc_info.value.code == "EMBEDDER_DIM_MISMATCH"
    assert exc_info.value.retryable is False
