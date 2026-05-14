"""Integration: running extractor twice with same edits is a no-op on second run."""
from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

_TEMPLATE_ID = "case_fact_summary"
_RULE = "When listing parties, always include their role in parentheses."


class _UniformEmbedder:
    name = "uniform"
    dim = 1024

    async def embed(self, texts):
        val = 1.0 / math.sqrt(1024)
        return [[val] * 1024 for _ in texts]


def _make_router(rule: str = _RULE, evidence: int = 5):
    resp = MagicMock()
    resp.text = json.dumps({"rule": rule, "evidence_count": evidence, "rationale": "consistent"})
    resp.structured = None
    router = MagicMock()
    router.generate = AsyncMock(return_value=resp)
    return router


@pytest_asyncio.fixture
async def _seeded_idempotent(test_session_factory):
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
        for i in range(5):
            await s.execute(
                text(
                    "INSERT INTO app.edits "
                    "(id, draft_id, template_id, template_version, prompt_fingerprint, "
                    "field_or_section_name, field_type, ai_value, user_value) "
                    "VALUES (gen_random_uuid(), :did, :tid, :ver, :fp, 'parties', 'field', "
                    "CAST(:ai AS jsonb), CAST(:usr AS jsonb))"
                ),
                {
                    "did": draft_id,
                    "tid": _TEMPLATE_ID,
                    "ver": entry["version"],
                    "fp": entry["fingerprint"],
                    "ai": json.dumps({"value": f"Smith {i}"}),
                    "usr": json.dumps({"value": f"Smith {i}, LLC"}),
                },
            )
        await s.commit()
    yield entry, registry


@pytest.mark.asyncio
async def test_idempotent_second_run_noop(
    _seeded_idempotent, test_session_factory, cleanup_drafts_and_jobs
):
    entry, registry = _seeded_idempotent
    from app.edits import rule_extractor as _re

    with patch("app.edits.rule_extractor._try_acquire", new=AsyncMock(return_value=True)), \
         patch("app.edits.rule_extractor._release", new=AsyncMock()):

        # First run
        async with test_session_factory() as session:
            async with test_session_factory() as lock_session:
                extractor = _re.RuleExtractor(
                    session=session,
                    lock_session=lock_session,
                    registry=registry,
                    llm_router=_make_router(),
                    embedder=_UniformEmbedder(),
                )
                result1 = await extractor.run(_TEMPLATE_ID, trace_id="t1")
                await session.commit()

        assert result1.new_version == 2
        assert len(result1.new_rules) == 1

        # Invalidate registry cache so it picks up v2
        registry._cache.clear()

        # Second run - same rule should be rejected as duplicate
        async with test_session_factory() as session:
            async with test_session_factory() as lock_session:
                extractor = _re.RuleExtractor(
                    session=session,
                    lock_session=lock_session,
                    registry=registry,
                    llm_router=_make_router(),
                    embedder=_UniformEmbedder(),
                )
                result2 = await extractor.run(_TEMPLATE_ID, trace_id="t2")
                await session.commit()

    assert result2.new_rules == []
    assert result2.new_version is None

    # Confirm only 2 template versions exist
    async with test_session_factory() as s:
        row = (
            await s.execute(
                text("SELECT COUNT(*) FROM app.templates WHERE template_id = :tid"),
                {"tid": _TEMPLATE_ID},
            )
        ).fetchone()
    assert row[0] == 2, f"Expected exactly 2 versions, got {row[0]}"

    # Cleanup
    async with test_session_factory() as s:
        await s.execute(text("DELETE FROM app.template_extractor_state WHERE template_id = :tid"), {"tid": _TEMPLATE_ID})
        await s.execute(text("DELETE FROM app.templates WHERE template_id = :tid AND version > 1"), {"tid": _TEMPLATE_ID})
        await s.commit()
