"""Integration: basic rule extraction creates a new template version."""
from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

_TEMPLATE_ID = "case_fact_summary"


class _UniformEmbedder:
    name = "uniform"
    dim = 1024

    async def embed(self, texts):
        val = 1.0 / math.sqrt(1024)
        return [[val] * 1024 for _ in texts]


def _make_router(rule: str, evidence: int = 5):
    resp = MagicMock()
    resp.text = json.dumps({"rule": rule, "evidence_count": evidence, "rationale": "consistent"})
    resp.structured = None
    router = MagicMock()
    router.generate = AsyncMock(return_value=resp)
    return router


async def _seed_edits(session_factory, template_id, draft_id, version, fingerprint, n=5):
    async with session_factory() as s:
        for i in range(n):
            await s.execute(
                text(
                    "INSERT INTO app.edits "
                    "(id, draft_id, template_id, template_version, prompt_fingerprint, "
                    "field_or_section_name, field_type, ai_value, user_value) "
                    "VALUES (gen_random_uuid(), :did, :tid, :ver, :fp, :name, :ftype, "
                    "CAST(:ai AS jsonb), CAST(:usr AS jsonb))"
                ),
                {
                    "did": draft_id,
                    "tid": template_id,
                    "ver": version,
                    "fp": fingerprint,
                    "name": "parties",
                    "ftype": "field",
                    "ai": json.dumps({"value": f"Smith {i}"}),
                    "usr": json.dumps({"value": f"Smith {i}, LLC"}),
                },
            )
        await s.commit()


@pytest_asyncio.fixture
async def _seeded(test_session_factory):
    from app.core.ids import new_uuid7
    from app.draft.templates.registry import TemplateRegistry
    from app.settings import settings

    registry = TemplateRegistry()
    registry.load_from_disk(settings.TEMPLATES_DIR)
    async with test_session_factory() as s:
        synced = await registry.sync_to_db(s)
        await s.commit()
    entry = next(e for e in synced if e["id"] == _TEMPLATE_ID)

    draft_id = new_uuid7()
    async with test_session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO app.drafts (id, template_id, template_version, prompt_fingerprint, status) "
                "VALUES (:id, :tid, :ver, :fp, 'ready')"
            ),
            {"id": draft_id, "tid": _TEMPLATE_ID, "ver": entry["version"], "fp": entry["fingerprint"]},
        )
        await s.commit()

    await _seed_edits(test_session_factory, _TEMPLATE_ID, draft_id, entry["version"], entry["fingerprint"], n=5)
    yield entry, registry, draft_id


@pytest.mark.asyncio
async def test_basic_extraction_creates_version(
    _seeded, test_session_factory, cleanup_drafts_and_jobs
):
    entry, registry, draft_id = _seeded
    fp1 = entry["fingerprint"]

    from app.db.session import direct_session_factory
    from app.edits.rule_extractor import RuleExtractor
    from app.settings import settings

    rule = "When listing parties, always include LLC suffix."
    router = _make_router(rule, evidence=5)
    embedder = _UniformEmbedder()

    from unittest.mock import patch

    with patch("app.edits.rule_extractor._try_acquire", new=AsyncMock(return_value=True)), \
         patch("app.edits.rule_extractor._release", new=AsyncMock()):
        async with test_session_factory() as session:
            async with test_session_factory() as lock_session:
                extractor = RuleExtractor(
                    session=session,
                    lock_session=lock_session,
                    registry=registry,
                    llm_router=router,
                    embedder=embedder,
                )
                result = await extractor.run(_TEMPLATE_ID, trace_id="test-trace")
                await session.commit()

    assert result.skipped_reason is None
    assert result.edits_processed == 5
    assert result.groups_evaluated == 1
    assert result.new_rules == [rule]
    assert result.new_version == 2
    assert result.prompt_fingerprint != fp1

    async with test_session_factory() as s:
        row = (
            await s.execute(
                text("SELECT appended_rules FROM app.templates WHERE template_id = :tid AND version = 2"),
                {"tid": _TEMPLATE_ID},
            )
        ).fetchone()
    assert row is not None, "v2 row not found"
    rules = row[0] if isinstance(row[0], list) else json.loads(row[0])
    assert any(rule in r for r in rules)

    async with test_session_factory() as s:
        state = (
            await s.execute(
                text("SELECT rules_added, last_edit_id FROM app.template_extractor_state WHERE template_id = :tid"),
                {"tid": _TEMPLATE_ID},
            )
        ).fetchone()
    assert state is not None
    assert state[0] == 1  # rules_added cumulative
    assert state[1] is not None  # last_edit_id set

    # Cleanup
    async with test_session_factory() as s:
        await s.execute(text("DELETE FROM app.template_extractor_state WHERE template_id = :tid"), {"tid": _TEMPLATE_ID})
        await s.execute(text("DELETE FROM app.templates WHERE template_id = :tid AND version > 1"), {"tid": _TEMPLATE_ID})
        await s.commit()
