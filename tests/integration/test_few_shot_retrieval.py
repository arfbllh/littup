"""Integration: FewShotStore.retrieve returns ranked results with correct filters."""
from __future__ import annotations

import json
import math

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

_TEMPLATE_ID = "case_fact_summary"
_FIELD = "parties"


def _unit_vec(seed: float) -> list[float]:
    """Return a deterministic 1024-dim vector biased toward `seed`."""
    import random
    rng = random.Random(seed)
    v = [rng.gauss(0, 1) for _ in range(1024)]
    mag = math.sqrt(sum(x * x for x in v))
    return [x / mag for x in v]


class _PresetEmbedder:
    """Returns pre-specified vectors in order of calls."""
    name = "preset"
    dim = 1024

    def __init__(self, vectors: list[list[float]]):
        self._vectors = list(vectors)
        self._idx = 0

    async def embed(self, texts):
        results = []
        for _ in texts:
            results.append(self._vectors[self._idx % len(self._vectors)])
            self._idx += 1
        return results

    async def health(self):
        return True


class _FixedEmbedder:
    """Always returns the same vector."""
    name = "fixed"
    dim = 1024

    def __init__(self, vec: list[float]):
        self._vec = vec

    async def embed(self, texts):
        return [list(self._vec) for _ in texts]

    async def health(self):
        return True


@pytest_asyncio.fixture
async def _seeded_edits(test_session_factory):
    """
    Insert 3 indexed edits for parties in case_fact_summary,
    plus 1 for a different template (should be filtered out),
    plus 1 unindexed (should be filtered out).

    Returns (edit_ids_indexed, draft_id_base).
    """
    from app.core.ids import new_uuid7

    draft_id = new_uuid7()
    vec_a = _unit_vec(1)
    vec_b = _unit_vec(2)
    vec_c = _unit_vec(3)

    vec_str = lambda v: "[" + ",".join(str(x) for x in v) + "]"

    edit_ids = []
    async with test_session_factory() as s:
        await s.execute(
            text("""
                INSERT INTO app.drafts (id, template_id, template_version, prompt_fingerprint, status)
                VALUES (:id, :tid, 1, 'fp', 'ready')
            """),
            {"id": draft_id, "tid": _TEMPLATE_ID},
        )
        for i, vec in enumerate([vec_a, vec_b, vec_c], 1):
            eid = new_uuid7()
            edit_ids.append(eid)
            await s.execute(
                text("""
                    INSERT INTO app.edits
                        (id, draft_id, template_id, template_version, prompt_fingerprint,
                         field_or_section_name, field_type,
                         ai_value, user_value, diff, context,
                         embedding, few_shot_indexed_at)
                    VALUES
                        (:id, :did, :tid, 1, 'fp',
                         'parties', 'field',
                         CAST(:ai AS jsonb), CAST(:user AS jsonb),
                         CAST(:diff AS jsonb), CAST(:ctx AS jsonb),
                         CAST(:vec AS vector), NOW())
                """),
                {
                    "id": eid, "did": draft_id, "tid": _TEMPLATE_ID,
                    "ai": json.dumps({"value": [{"name": f"AI{i}"}]}),
                    "user": json.dumps({"value": [{"name": f"User{i}"}]}),
                    "diff": json.dumps({}), "ctx": json.dumps({}),
                    "vec": vec_str(vec),
                },
            )

        # Edit for a different template — should NOT appear in results
        eid_other = new_uuid7()
        await s.execute(
            text("""
                INSERT INTO app.edits
                    (id, draft_id, template_id, template_version, prompt_fingerprint,
                     field_or_section_name, field_type, ai_value, user_value, diff, context,
                     embedding, few_shot_indexed_at)
                VALUES
                    (:id, :did, 'other_template', 1, 'fp',
                     'parties', 'field',
                     CAST(:ai AS jsonb), CAST(:user AS jsonb),
                     CAST(:diff AS jsonb), CAST(:ctx AS jsonb),
                     CAST(:vec AS vector), NOW())
            """),
            {
                "id": eid_other, "did": draft_id,
                "ai": json.dumps({"value": []}), "user": json.dumps({"value": []}),
                "diff": json.dumps({}), "ctx": json.dumps({}),
                "vec": vec_str(vec_a),
            },
        )

        # Unindexed edit — should NOT appear
        eid_unindexed = new_uuid7()
        await s.execute(
            text("""
                INSERT INTO app.edits
                    (id, draft_id, template_id, template_version, prompt_fingerprint,
                     field_or_section_name, field_type, ai_value, user_value, diff, context)
                VALUES
                    (:id, :did, :tid, 1, 'fp', 'parties', 'field',
                     CAST(:ai AS jsonb), CAST(:user AS jsonb),
                     CAST(:diff AS jsonb), CAST(:ctx AS jsonb))
            """),
            {
                "id": eid_unindexed, "did": draft_id, "tid": _TEMPLATE_ID,
                "ai": json.dumps({"value": []}), "user": json.dumps({"value": []}),
                "diff": json.dumps({}), "ctx": json.dumps({}),
            },
        )
        await s.commit()

    yield edit_ids, vec_a, vec_b, vec_c, draft_id


@pytest.mark.asyncio
async def test_retrieval_order_closest_first(
    _seeded_edits, test_session_factory, cleanup_drafts_and_jobs
):
    edit_ids, vec_a, vec_b, vec_c, _ = _seeded_edits

    from app.edits.few_shot_store import FewShotStore

    # Query with a vector equal to vec_b → edit #2 should be closest
    embedder = _FixedEmbedder(vec_b)
    store = FewShotStore(embedder=embedder)

    async with test_session_factory() as s:
        results = await store.retrieve(
            _TEMPLATE_ID, _FIELD,
            session=s,
            field_type="field",
            top_k=3,
        )

    assert len(results) > 0
    # Edit #2 (edit_ids[1]) should be the first result
    assert results[0].edit_id == edit_ids[1]


@pytest.mark.asyncio
async def test_retrieval_filters_other_template(
    _seeded_edits, test_session_factory, cleanup_drafts_and_jobs
):
    edit_ids, vec_a, *_ = _seeded_edits

    from app.edits.few_shot_store import FewShotStore

    embedder = _FixedEmbedder(vec_a)
    store = FewShotStore(embedder=embedder)

    async with test_session_factory() as s:
        results = await store.retrieve(
            _TEMPLATE_ID, _FIELD,
            session=s,
            field_type="field",
            top_k=10,
        )

    returned_ids = {r.edit_id for r in results}
    for eid in edit_ids:
        assert eid in returned_ids
    # Only 3 results from this template (not the other_template one)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_retrieval_filters_unindexed(
    _seeded_edits, test_session_factory, cleanup_drafts_and_jobs
):
    edit_ids, vec_a, *_ = _seeded_edits

    from app.edits.few_shot_store import FewShotStore

    embedder = _FixedEmbedder(vec_a)
    store = FewShotStore(embedder=embedder)

    async with test_session_factory() as s:
        results = await store.retrieve(
            _TEMPLATE_ID, _FIELD,
            session=s,
            field_type="field",
            top_k=10,
        )

    # Unindexed edit should not appear
    returned_ids = {r.edit_id for r in results}
    assert len(results) == 3  # only the 3 indexed ones


@pytest.mark.asyncio
async def test_retrieval_returns_empty_when_no_matches(
    test_session_factory, cleanup_drafts_and_jobs
):
    from app.edits.few_shot_store import FewShotStore
    from app.llm.embedder import StubEmbedder

    # StubEmbedder returns zero vectors but dim=1024 passes the check
    embedder = StubEmbedder(dim=1024)
    store = FewShotStore(embedder=embedder)

    async with test_session_factory() as s:
        results = await store.retrieve(
            "nonexistent_template", "nonexistent_field",
            session=s,
            field_type="field",
            top_k=3,
        )

    assert results == []
