"""Integration test: eval/run_edit_improvement.py smoke test.

Verifies that the edit-improvement eval script runs end-to-end without raising
exceptions and returns the expected keys, using a minimal in-memory EvalContext
with a mock DraftEngine.generate so no real LLM or GPU is needed.
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from tests.integration.conftest import TEST_DB_URL

pytestmark = pytest.mark.asyncio

_TEMPLATE_ID = "case_fact_summary"

# Fake ai_output that the mock generate() will write into the draft row.
_FAKE_AI_OUTPUT = {
    "fields": {
        "parties": {"value": "Acme Corp."},
        "jurisdiction": {"value": "S.D.N.Y."},
        "filing_date": {"value": "Jan 2026"},
        "claims": {"value": []},
        "damages_sought": {"value": None},
    }
}


# ── Helper: insert a minimal ready document ───────────────────────────────────

async def _insert_ready_doc(session_factory, filename: str) -> str:
    """Insert a synthetic ready document and return its UUID."""
    import hashlib
    import uuid

    doc_id = str(uuid.uuid4())
    sha = hashlib.sha256(filename.encode()).hexdigest()

    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO app.documents "
                "(id, sha256, filename, status) "
                "VALUES (:id, :sha, :fn, 'ready') "
                "ON CONFLICT (sha256) DO NOTHING"
            ),
            {"id": doc_id, "sha": sha, "fn": filename},
        )
        await s.commit()

        # Resolve the actual id (in case of ON CONFLICT DO NOTHING).
        row = (
            await s.execute(
                text("SELECT id FROM app.documents WHERE sha256 = :sha"),
                {"sha": sha},
            )
        ).fetchone()

    return str(row[0])


# ── Build minimal EvalContext with mock generate ──────────────────────────────

async def _build_test_ctx(test_session_factory) -> "EvalContext":  # noqa: F821
    """Bootstrap EvalContext using mock_llm=True and bind it to the test DB."""
    from eval.common import build_context

    ctx = await build_context(db_url=TEST_DB_URL, mock_llm=True)
    # Rebind session_factory to test engine so all DB access hits the test DB.
    ctx = _replace_session_factory(ctx, test_session_factory)
    return ctx


def _replace_session_factory(ctx, new_factory):
    """Return a new EvalContext with session_factory replaced."""
    from dataclasses import replace
    from eval.common import EvalContext
    from app.edits.rule_extractor import RuleExtractor
    from app.edits.service import EditService

    # Re-wrap factories so they use the test session factory.
    null_queue = ctx.edit_service_factory.__closure__  # keep reference alive

    def edit_service_factory(session):
        return EditService(session, _NullQueue(), ctx.registry)

    def rule_extractor_factory(session, lock_session):
        return RuleExtractor(
            session=session,
            lock_session=lock_session,
            registry=ctx.registry,
            llm_router=ctx.llm_router,
            embedder=ctx.embedder,
        )

    return EvalContext(
        session_factory=new_factory,
        llm_router=ctx.llm_router,
        retriever=ctx.retriever,
        draft_engine=ctx.draft_engine,
        registry=ctx.registry,
        embedder=ctx.embedder,
        edit_service_factory=edit_service_factory,
        rule_extractor_factory=rule_extractor_factory,
    )


class _NullQueue:
    async def enqueue(self, *args, **kwargs):
        pass


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def eval_ctx(test_session_factory, cleanup_eval_data):
    """Build a test EvalContext with a ready document inserted."""
    ctx = await _build_test_ctx(test_session_factory)

    # Sync template to DB using test session factory.
    from app.settings import settings

    ctx.registry.load_from_disk(settings.TEMPLATES_DIR)
    async with test_session_factory() as s:
        await ctx.registry.sync_to_db(s)
        await s.commit()

    # Insert a ready document whose filename contains "scan_clean".
    doc_id = await _insert_ready_doc(test_session_factory, "scan_clean.pdf")

    return ctx, doc_id


@pytest_asyncio.fixture
async def cleanup_eval_data(test_session_factory):
    """Clean up drafts, edits, documents, and template state inserted by the eval."""
    yield
    async with test_session_factory() as s:
        await s.execute(text("DELETE FROM app.template_extractor_state"))
        await s.execute(text("DELETE FROM app.edits"))
        await s.execute(text("DELETE FROM app.drafts"))
        await s.execute(text("DELETE FROM app.documents WHERE filename LIKE '%scan_clean%'"))
        # Remove template versions beyond v1 (added by rule extractor).
        await s.execute(
            text("DELETE FROM app.templates WHERE template_id = :tid AND version > 1"),
            {"tid": _TEMPLATE_ID},
        )
        await s.commit()


# ── Mock generate ─────────────────────────────────────────────────────────────

def _make_mock_generate(session_factory):
    """Return an AsyncMock that writes a fake ai_output into the draft row.

    The real DraftEngine.generate() transitions the draft from 'queued' →
    'generating' → 'ready'. Our mock short-circuits this by directly updating
    the draft row to status='ready' with fake ai_output.
    """

    async def _fake_generate(draft_id, template_id, document_ids, trace_id=None):
        from app.draft.templates.registry import TemplateRegistry
        from app.settings import settings

        # We need the real template version/fingerprint to write a valid row.
        registry = TemplateRegistry()
        registry.load_from_disk(settings.TEMPLATES_DIR)
        async with session_factory() as s:
            await registry.sync_to_db(s)
            await s.commit()
            template = await registry.get_latest(template_id, s)

        async with session_factory() as s:
            await s.execute(
                text(
                    "UPDATE app.drafts "
                    "SET status = 'ready', "
                    "    template_version = :ver, "
                    "    prompt_fingerprint = :fp, "
                    "    ai_output = CAST(:ai_output AS jsonb), "
                    "    generated_at = NOW() "
                    "WHERE id = :id"
                ),
                {
                    "id": draft_id,
                    "ver": template.version,
                    "fp": template.compute_fingerprint(),
                    "ai_output": json.dumps(_FAKE_AI_OUTPUT),
                },
            )
            await s.commit()

    return AsyncMock(side_effect=_fake_generate)


# ── The test ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_eval_edit_improvement_runs(eval_ctx, test_session_factory, cleanup_eval_data):
    """Smoke test: run_edit_improvement.run() completes without exceptions and
    returns the expected top-level keys."""
    import eval.run_edit_improvement as _script

    ctx, _doc_id = eval_ctx

    # Patch generate so it writes a deterministic ai_output instead of calling
    # the real LLM pipeline.
    ctx.draft_engine.generate = _make_mock_generate(test_session_factory)

    result = await _script.run(_ctx=ctx)

    # ── Structural assertions ─────────────────────────────────────────────
    assert isinstance(result, dict), "run() must return a dict"

    for key in ("improvement_ratio", "baseline_scores", "post_scores", "rules_extracted"):
        assert key in result, f"Missing key '{key}' in result dict"

    assert isinstance(result["improvement_ratio"], float)
    assert isinstance(result["baseline_scores"], dict)
    assert isinstance(result["post_scores"], dict)
    assert isinstance(result["rules_extracted"], int)
    assert result["rules_extracted"] >= 0

    # ── No baseline scores means the doc wasn't found or generate crashed.
    # We allow empty baseline only if the test DB had no ready documents
    # (e.g., ON CONFLICT silently lost). We still assert no exception was raised
    # by getting here at all. A fuller check:
    # If baseline_scores is non-empty, improvement_ratio must be a valid float.
    if result["baseline_scores"]:
        assert -10.0 <= result["improvement_ratio"] <= 10.0, (
            "improvement_ratio out of reasonable range"
        )

    # ── If scan_clean was resolved, baseline_scores must have case_fact_summary.
    # (The eval skips templates whose doc can't be found, so this only fires
    # when the fixture doc was actually inserted.)
    async with test_session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT id FROM app.documents"
                    " WHERE filename LIKE '%scan_clean%' AND status = 'ready'"
                    " LIMIT 1"
                )
            )
        ).fetchone()

    if row is not None:
        assert _TEMPLATE_ID in result["baseline_scores"], (
            f"Expected '{_TEMPLATE_ID}' in baseline_scores when scan_clean doc is present"
        )
        assert _TEMPLATE_ID in result["post_scores"], (
            f"Expected '{_TEMPLATE_ID}' in post_scores when scan_clean doc is present"
        )
