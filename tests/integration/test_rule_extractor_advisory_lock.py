"""Integration: advisory lock prevents concurrent runs; no lock leak after run."""
from __future__ import annotations

import json
import math
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.asyncio

_TEMPLATE_ID = "case_fact_summary"
_LOCK_ID = 9_010_001

# Use direct URL to bypass pgbouncer for advisory lock assertions
_DIRECT_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://littup:littup@localhost:5432/littup_test",
)


class _UniformEmbedder:
    name = "uniform"
    dim = 1024

    async def embed(self, texts):
        val = 1.0 / math.sqrt(1024)
        return [[val] * 1024 for _ in texts]


def _make_router():
    resp = MagicMock()
    resp.text = json.dumps({"rule": "Always include LLC suffix.", "evidence_count": 5, "rationale": "ok"})
    resp.structured = None
    router = MagicMock()
    router.generate = AsyncMock(return_value=resp)
    return router


@pytest_asyncio.fixture
async def direct_engine():
    eng = create_async_engine(_DIRECT_URL, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def direct_sf(direct_engine):
    return async_sessionmaker(direct_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def _seeded_lock(test_session_factory):
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
                    "ai": json.dumps({"value": f"AI {i}"}),
                    "usr": json.dumps({"value": f"User {i}"}),
                },
            )
        await s.commit()
    yield entry, registry


@pytest.mark.asyncio
async def test_lock_held_externally_causes_skip(
    _seeded_lock, test_session_factory, direct_sf, cleanup_drafts_and_jobs, caplog
):
    import logging

    entry, registry = _seeded_lock
    from app.edits.rule_extractor import RuleExtractor

    class _S:
        RULE_EXTRACTOR_INTERVAL_HOURS = 6
        RULE_EXTRACTOR_MIN_EDITS = 3
        RULE_EXTRACTOR_SIMILARITY_THRESHOLD = 0.88
        RULE_EXTRACTOR_MAX_EDITS_PER_GROUP = 12
        RULE_EXTRACTOR_FIRST_RUN_LOOKBACK_DAYS = 30
        RULE_EXTRACTOR_VERSION_LOOKBACK = 2
        RULE_EXTRACTOR_ADMIN_MAX_DURATION_S = 180
        RULE_EXTRACTOR_LOCK_ID = _LOCK_ID

    # Competing session holds the lock
    competing = direct_sf()
    await competing.__aenter__()
    await competing.execute(text(f"SELECT pg_advisory_lock({_LOCK_ID})"))

    try:
        # Extractor should see lock busy
        with caplog.at_level(logging.INFO):
            async with test_session_factory() as session:
                async with direct_sf() as lock_session:
                    extractor = RuleExtractor(
                        session=session,
                        lock_session=lock_session,
                        registry=registry,
                        llm_router=_make_router(),
                        embedder=_UniformEmbedder(),
                        settings_obj=_S(),
                    )
                    result = await extractor.run(_TEMPLATE_ID, trace_id="t-lock")
                    await session.commit()

        assert result.skipped_reason == "locked"
        assert result.new_rules == []
        busy_events = [r for r in caplog.records if "rule_extractor.lock_busy" in r.message]
        assert len(busy_events) == 1
    finally:
        await competing.execute(text(f"SELECT pg_advisory_unlock({_LOCK_ID})"))
        await competing.__aexit__(None, None, None)

    # After releasing, extractor should succeed
    registry._cache.clear()
    async with test_session_factory() as session:
        async with direct_sf() as lock_session:
            extractor = RuleExtractor(
                session=session,
                lock_session=lock_session,
                registry=registry,
                llm_router=_make_router(),
                embedder=_UniformEmbedder(),
                settings_obj=_S(),
            )
            result2 = await extractor.run(_TEMPLATE_ID, trace_id="t-lock2")
            await session.commit()

    assert result2.skipped_reason is None
    assert len(result2.new_rules) == 1

    # Verify no lock leak via direct URL.
    # pg_advisory_lock(bigint) stores classid = key >> 32, objid = key & 0xFFFFFFFF, objsubid = 1.
    async with direct_sf() as s:
        row = (
            await s.execute(
                text(
                    f"SELECT count(*) FROM pg_locks "
                    f"WHERE locktype='advisory' AND classid=0 AND objid={_LOCK_ID} AND objsubid=1"
                )
            )
        ).fetchone()
    assert row[0] == 0, f"Advisory lock leaked! count={row[0]}"

    # Cleanup
    async with test_session_factory() as s:
        await s.execute(text("DELETE FROM app.template_extractor_state WHERE template_id = :tid"), {"tid": _TEMPLATE_ID})
        await s.execute(text("DELETE FROM app.templates WHERE template_id = :tid AND version > 1"), {"tid": _TEMPLATE_ID})
        await s.commit()
