"""Integration: rule extractor admin endpoints."""
from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

_TEMPLATE_ID = "case_fact_summary"
_RULE = "When listing parties, always include LLC suffix."


class _UniformEmbedder:
    name = "uniform"
    dim = 1024

    async def embed(self, texts):
        val = 1.0 / math.sqrt(1024)
        return [[val] * 1024 for _ in texts]


def _make_router(rule: str = _RULE):
    resp = MagicMock()
    resp.text = json.dumps({"rule": rule, "evidence_count": 5, "rationale": "ok"})
    resp.structured = None
    router = MagicMock()
    router.generate = AsyncMock(return_value=resp)
    return router


@pytest_asyncio.fixture
async def _seeded_admin(test_session_factory):
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
async def test_admin_run_creates_rule(
    _seeded_admin, test_session_factory, cleanup_drafts_and_jobs
):
    entry, registry = _seeded_admin
    from httpx import ASGITransport, AsyncClient

    from app.api.deps import (
        get_embedder,
        get_llm_router,
        get_template_registry,
        reset_embedder_for_tests,
        reset_template_registry_for_tests,
    )
    from app.db import session as app_session
    from app.edits import rule_extractor as _re
    from app.main import create_app

    reset_template_registry_for_tests()
    reset_embedder_for_tests()

    app = create_app()

    mock_router_inst = _make_router()
    mock_embedder = _UniformEmbedder()

    app.dependency_overrides[get_llm_router] = lambda: mock_router_inst
    app.dependency_overrides[get_embedder] = lambda: mock_embedder
    app.dependency_overrides[get_template_registry] = lambda: registry

    async def _override_session():
        async with test_session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    from app.db.session import get_session
    app.dependency_overrides[get_session] = _override_session

    with patch("app.edits.rule_extractor._try_acquire", new=AsyncMock(return_value=True)), \
         patch("app.edits.rule_extractor._release", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # First POST: should create a rule
            resp = await client.post(
                "/admin/rule-extractor/run",
                json={"template_id": _TEMPLATE_ID},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert len(data["results"]) == 1
            row = data["results"][0]
            assert row["template_id"] == _TEMPLATE_ID
            assert row["new_rules"] == [_RULE]
            assert row["new_version"] == 2

            # Invalidate registry cache
            registry._cache.clear()

            # Second POST: same rule should be deduped
            resp2 = await client.post(
                "/admin/rule-extractor/run",
                json={"template_id": _TEMPLATE_ID},
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["results"][0]["new_rules"] == []

            # Unknown template → 404
            resp404 = await client.post(
                "/admin/rule-extractor/run",
                json={"template_id": "nonexistent_template_xyz"},
            )
            assert resp404.status_code == 404

            # GET /admin/templates/{id}/versions shows v2 with rule
            resp_v = await client.get(f"/admin/templates/{_TEMPLATE_ID}/versions")
            assert resp_v.status_code == 200
            vdata = resp_v.json()
            assert vdata["template_id"] == _TEMPLATE_ID
            v2 = next((v for v in vdata["versions"] if v["version"] == 2), None)
            assert v2 is not None
            assert _RULE in v2["resolved_system_prompt"]

            # GET /admin/templates lists all templates including current one
            resp_tpl = await client.get("/admin/templates")
            assert resp_tpl.status_code == 200
            tdata = resp_tpl.json()
            assert "templates" in tdata
            template_ids = [t["template_id"] for t in tdata["templates"]]
            assert _TEMPLATE_ID in template_ids
            trow = next(t for t in tdata["templates"] if t["template_id"] == _TEMPLATE_ID)
            assert trow["latest_version"] == 2
            assert trow["appended_rules_count"] == 1

    # Cleanup
    async with test_session_factory() as s:
        await s.execute(text("DELETE FROM app.template_extractor_state WHERE template_id = :tid"), {"tid": _TEMPLATE_ID})
        await s.execute(text("DELETE FROM app.templates WHERE template_id = :tid AND version > 1"), {"tid": _TEMPLATE_ID})
        await s.commit()


@pytest.mark.asyncio
async def test_admin_run_no_template_returns_404(
    _seeded_admin, test_session_factory, cleanup_drafts_and_jobs
):
    from httpx import ASGITransport, AsyncClient

    from app.api.deps import get_embedder, get_llm_router, get_template_registry
    from app.db.session import get_session
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_template_registry] = lambda: _seeded_admin[1]
    app.dependency_overrides[get_llm_router] = lambda: _make_router()
    app.dependency_overrides[get_embedder] = lambda: _UniformEmbedder()

    async def _override_session():
        async with test_session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = _override_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/admin/rule-extractor/run",
            json={"template_id": "does_not_exist_abc123"},
        )
        assert resp.status_code == 404
