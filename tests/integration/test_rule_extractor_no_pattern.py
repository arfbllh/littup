"""Integration: NO_RULE response produces no new version."""
from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_no_rule_router():
    resp = MagicMock()
    resp.text = json.dumps({"rule": "NO_RULE", "evidence_count": 0, "rationale": "no pattern"})
    resp.structured = None
    router = MagicMock()
    router.generate = AsyncMock(return_value=resp)
    return router


@pytest_asyncio.fixture
async def _seeded_no_pattern(test_session_factory):
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
        for i in range(4):
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
                    "ai": json.dumps({"value": f"random AI {i}"}),
                    "usr": json.dumps({"value": f"unrelated correction {i}"}),
                },
            )
        await s.commit()
    yield entry, registry


@pytest.mark.asyncio
async def test_no_pattern_no_version(
    _seeded_no_pattern, test_session_factory, cleanup_drafts_and_jobs, caplog
):
    import logging

    entry, registry = _seeded_no_pattern
    from app.edits import rule_extractor as _re

    with caplog.at_level(logging.INFO), \
         patch("app.edits.rule_extractor._try_acquire", new=AsyncMock(return_value=True)), \
         patch("app.edits.rule_extractor._release", new=AsyncMock()):
        async with test_session_factory() as session:
            async with test_session_factory() as lock_session:
                extractor = _re.RuleExtractor(
                    session=session,
                    lock_session=lock_session,
                    registry=registry,
                    llm_router=_make_no_rule_router(),
                    embedder=_UniformEmbedder(),
                )
                result = await extractor.run(_TEMPLATE_ID, trace_id="t1")
                await session.commit()

    assert result.new_rules == []
    assert result.new_version is None

    no_rule_events = [r for r in caplog.records if "rule_extractor.no_rule" in r.message]
    assert len(no_rule_events) == 1

    async with test_session_factory() as s:
        row = (
            await s.execute(
                text("SELECT COUNT(*) FROM app.templates WHERE template_id = :tid AND version > 1"),
                {"tid": _TEMPLATE_ID},
            )
        ).fetchone()
    assert row[0] == 0, "No v2 should be created when NO_RULE"

    async with test_session_factory() as s:
        state = (
            await s.execute(
                text("SELECT rules_added FROM app.template_extractor_state WHERE template_id = :tid"),
                {"tid": _TEMPLATE_ID},
            )
        ).fetchone()
    assert state is not None
    assert state[0] == 0

    # Cleanup
    async with test_session_factory() as s:
        await s.execute(text("DELETE FROM app.template_extractor_state WHERE template_id = :tid"), {"tid": _TEMPLATE_ID})
        await s.commit()
