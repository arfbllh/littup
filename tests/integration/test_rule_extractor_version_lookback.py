"""Integration: version-floor filter controls which edits are considered."""
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


def _make_router(rule: str = "Always include LLC suffix."):
    resp = MagicMock()
    resp.text = json.dumps({"rule": rule, "evidence_count": 3, "rationale": "ok"})
    resp.structured = None
    router = MagicMock()
    router.generate = AsyncMock(return_value=resp)
    return router


@pytest_asyncio.fixture
async def _seeded_old_edits(test_session_factory):
    """Seed 3 edits at v1, then bump to v3 to test version floor filtering."""
    from app.core.ids import new_uuid7
    from app.draft.templates.registry import TemplateRegistry
    from app.settings import settings

    registry = TemplateRegistry()
    registry.load_from_disk(settings.TEMPLATES_DIR)
    async with test_session_factory() as s:
        synced = await registry.sync_to_db(s)
        await s.commit()
    entry = next(e for e in synced if e["id"] == _TEMPLATE_ID)
    v1 = entry["version"]
    fp1 = entry["fingerprint"]

    draft_id = new_uuid7()
    async with test_session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO app.drafts (id, template_id, template_version, prompt_fingerprint, status) "
                "VALUES (:id, :tid, :ver, :fp, 'ready')"
            ),
            {"id": draft_id, "tid": _TEMPLATE_ID, "ver": v1, "fp": fp1},
        )
        # 3 edits at version 1
        for i in range(3):
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
                    "ver": v1,
                    "fp": fp1,
                    "ai": json.dumps({"value": f"Smith {i}"}),
                    "usr": json.dumps({"value": f"Smith {i}, LLC"}),
                },
            )
        # Insert v2, v3 into templates table (simulating 2 extra bumps)
        for extra_ver in [v1 + 1, v1 + 2]:
            await s.execute(
                text(
                    "INSERT INTO app.templates "
                    "(template_id, version, yaml_body, system_prompt, appended_rules, prompt_fingerprint) "
                    "SELECT template_id, :ver, yaml_body, system_prompt, appended_rules, :fp "
                    "FROM app.templates WHERE template_id = :tid AND version = :v1"
                ),
                {"ver": extra_ver, "fp": f"fp-v{extra_ver}", "tid": _TEMPLATE_ID, "v1": v1},
            )
        await s.commit()

    # Clear registry cache so it sees v3
    registry._cache.clear()
    yield v1, v1 + 2, registry  # (v1_int, current_version=v3)


@pytest.mark.asyncio
async def test_version_lookback_includes_edits(
    _seeded_old_edits, test_session_factory, cleanup_drafts_and_jobs
):
    """With lookback=2 and current_v=3, v_floor=max(1,3-2)=1 → edits at v1 ARE included."""
    v1, v_current, registry = _seeded_old_edits
    from app.edits import rule_extractor as _re
    from app.settings import settings

    router = _make_router()

    class _S:
        RULE_EXTRACTOR_INTERVAL_HOURS = 6
        RULE_EXTRACTOR_MIN_EDITS = 3
        RULE_EXTRACTOR_SIMILARITY_THRESHOLD = 0.88
        RULE_EXTRACTOR_MAX_EDITS_PER_GROUP = 12
        RULE_EXTRACTOR_FIRST_RUN_LOOKBACK_DAYS = 30
        RULE_EXTRACTOR_VERSION_LOOKBACK = 2
        RULE_EXTRACTOR_ADMIN_MAX_DURATION_S = 180
        RULE_EXTRACTOR_LOCK_ID = 9_010_001

    with patch("app.edits.rule_extractor._try_acquire", new=AsyncMock(return_value=True)), \
         patch("app.edits.rule_extractor._release", new=AsyncMock()):
        async with test_session_factory() as session:
            async with test_session_factory() as lock_session:
                extractor = _re.RuleExtractor(
                    session=session,
                    lock_session=lock_session,
                    registry=registry,
                    llm_router=router,
                    embedder=_UniformEmbedder(),
                    settings_obj=_S(),
                )
                result = await extractor.run(_TEMPLATE_ID, trace_id="t1")
                await session.commit()

    assert result.groups_evaluated == 1, f"Expected 1 group evaluated (edits at v1 included by lookback=2), got {result.groups_evaluated}"

    # Cleanup
    async with test_session_factory() as s:
        await s.execute(text("DELETE FROM app.template_extractor_state WHERE template_id = :tid"), {"tid": _TEMPLATE_ID})
        await s.execute(
            text("DELETE FROM app.templates WHERE template_id = :tid AND version > 1"), {"tid": _TEMPLATE_ID}
        )
        await s.commit()


@pytest.mark.asyncio
async def test_version_lookback_zero_excludes_edits(
    _seeded_old_edits, test_session_factory, cleanup_drafts_and_jobs
):
    """With lookback=0 and current_v=3, v_floor=3 → edits at v1 are excluded."""
    v1, v_current, registry = _seeded_old_edits
    from app.edits import rule_extractor as _re

    router = MagicMock()
    router.generate = AsyncMock()

    class _S:
        RULE_EXTRACTOR_INTERVAL_HOURS = 6
        RULE_EXTRACTOR_MIN_EDITS = 3
        RULE_EXTRACTOR_SIMILARITY_THRESHOLD = 0.88
        RULE_EXTRACTOR_MAX_EDITS_PER_GROUP = 12
        RULE_EXTRACTOR_FIRST_RUN_LOOKBACK_DAYS = 30
        RULE_EXTRACTOR_VERSION_LOOKBACK = 0
        RULE_EXTRACTOR_ADMIN_MAX_DURATION_S = 180
        RULE_EXTRACTOR_LOCK_ID = 9_010_001

    with patch("app.edits.rule_extractor._try_acquire", new=AsyncMock(return_value=True)), \
         patch("app.edits.rule_extractor._release", new=AsyncMock()):
        async with test_session_factory() as session:
            async with test_session_factory() as lock_session:
                extractor = _re.RuleExtractor(
                    session=session,
                    lock_session=lock_session,
                    registry=registry,
                    llm_router=router,
                    embedder=_UniformEmbedder(),
                    settings_obj=_S(),
                )
                result = await extractor.run(_TEMPLATE_ID, trace_id="t2")
                await session.commit()

    router.generate.assert_not_called()
    assert result.groups_evaluated == 0

    # Cleanup
    async with test_session_factory() as s:
        await s.execute(text("DELETE FROM app.template_extractor_state WHERE template_id = :tid"), {"tid": _TEMPLATE_ID})
        await s.execute(
            text("DELETE FROM app.templates WHERE template_id = :tid AND version > 1"), {"tid": _TEMPLATE_ID}
        )
        await s.commit()
