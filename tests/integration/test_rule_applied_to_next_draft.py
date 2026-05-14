"""Integration: rubric demo — extracted rule appears in the next draft's system prompt."""
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


def _make_extraction_router(rule_text: str):
    """Router that returns a valid extraction response and captures generation prompts."""
    captured_generation_prompts: list[str] = []

    async def _generate(messages, *, task, schema=None, sampling=None, trace_id=None, cache=True, **kw):
        resp = MagicMock()
        resp.tokens_in = 10
        resp.tokens_out = 20
        resp.cost_usd = 0.001
        resp.model_used = "mock"
        resp.latency_ms = 5
        resp.cached_hit = False

        full = "".join(
            m.content if hasattr(m, "content") else m.get("content", "")
            for m in messages
        )

        if task == "extraction":
            resp.structured = {
                "value": [{"name": "Smith", "role": "plaintiff"}],
                "supporting_chunk_ids": [],
                "confidence": 0.9,
            }
            resp.text = None
        elif task == "generation":
            captured_generation_prompts.append(full)
            resp.text = "Parties: Smith (plaintiff)."
            resp.structured = None
        elif task == "validation":
            resp.text = json.dumps({"results": []})
            resp.structured = {"results": []}
        else:
            resp.text = ""
            resp.structured = None
        return resp

    router = MagicMock()
    router.generate = AsyncMock(side_effect=_generate)
    router._captured = captured_generation_prompts
    return router


@pytest_asyncio.fixture
async def _seeded_rubric(test_session_factory):
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
                    "usr": json.dumps({"value": f"Smith {i} (plaintiff)"}),
                },
            )
        await s.commit()

    draft_b_id = new_uuid7()
    async with test_session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO app.drafts (id, template_id, template_version, prompt_fingerprint, status) "
                "VALUES (:id, :tid, 0, '', 'queued')"
            ),
            {"id": draft_b_id, "tid": _TEMPLATE_ID},
        )
        await s.commit()

    yield entry, registry, draft_b_id


@pytest.mark.asyncio
async def test_rule_appears_in_next_draft_system_prompt(
    _seeded_rubric, test_session_factory, cleanup_drafts_and_jobs
):
    entry, registry, draft_b_id = _seeded_rubric

    from app.edits import rule_extractor as _re

    # Step 1: Run extractor to create v2 with the rule
    rule_router_resp = MagicMock()
    rule_router_resp.text = json.dumps({"rule": _RULE, "evidence_count": 5, "rationale": "ok"})
    rule_router_resp.structured = None
    rule_router = MagicMock()
    rule_router.generate = AsyncMock(return_value=rule_router_resp)

    with patch("app.edits.rule_extractor._try_acquire", new=AsyncMock(return_value=True)), \
         patch("app.edits.rule_extractor._release", new=AsyncMock()):
        async with test_session_factory() as session:
            async with test_session_factory() as lock_session:
                extractor = _re.RuleExtractor(
                    session=session,
                    lock_session=lock_session,
                    registry=registry,
                    llm_router=rule_router,
                    embedder=_UniformEmbedder(),
                )
                result = await extractor.run(_TEMPLATE_ID, trace_id="t1")
                await session.commit()

    assert result.new_version == 2, f"Expected v2, got {result.new_version}"
    assert _RULE in result.new_rules

    # Invalidate registry cache so draft B picks up v2
    registry._cache.clear()

    # Step 2: Generate draft B — spy on generation prompts
    from app.draft.engine import DraftEngine

    draft_router = _make_extraction_router(_RULE)

    chunk = MagicMock()
    chunk.id = "c0ffee00-0000-0000-0000-000000000001"
    chunk.text = "Smith filed suit."
    retriever = MagicMock()
    retrieval_keys = [
        "parties", "jurisdiction", "filing_date", "claims", "damages_sought",
        "procedural_history", "factual_background", "key_issues",
    ]
    retriever.multi_retrieve = AsyncMock(return_value={k: [chunk] for k in retrieval_keys})

    engine = DraftEngine(
        retriever=retriever,
        llm_router=draft_router,
        registry=registry,
        session_factory=test_session_factory,
        embedder=_UniformEmbedder(),
    )
    await engine.generate(
        draft_id=draft_b_id,
        template_id=_TEMPLATE_ID,
        document_ids=["doc-placeholder"],
        trace_id=None,
    )

    # Step 3: Assert draft B's template_version == 2
    async with test_session_factory() as s:
        row = (
            await s.execute(
                text("SELECT template_version, status FROM app.drafts WHERE id = :id"),
                {"id": draft_b_id},
            )
        ).fetchone()
    assert row is not None
    assert row[0] == 2, f"Draft B should use template v2, got {row[0]}"
    assert row[1] == "ready"

    # Step 4: Verify the rule appears in the resolved system prompt of v2
    async with test_session_factory() as s:
        trow = (
            await s.execute(
                text("SELECT system_prompt, appended_rules FROM app.templates WHERE template_id = :tid AND version = 2"),
                {"tid": _TEMPLATE_ID},
            )
        ).fetchone()
    assert trow is not None
    rules = trow[1] if isinstance(trow[1], list) else json.loads(trow[1])
    assert _RULE in rules

    resolved = trow[0] + "\n\n" + "\n".join(rules)
    assert _RULE in resolved

    # Step 5: Assert the generation-tier call contains the rule verbatim
    gen_prompts = draft_router._captured
    assert len(gen_prompts) > 0, "No generation-tier calls were captured"
    any_contains_rule = any(_RULE in p for p in gen_prompts)
    assert any_contains_rule, (
        f"Rule '{_RULE}' not found in any generation prompt.\n"
        f"Captured prompt excerpt: {gen_prompts[0][:400] if gen_prompts else 'none'}"
    )

    # Cleanup
    async with test_session_factory() as s:
        await s.execute(text("DELETE FROM app.template_extractor_state WHERE template_id = :tid"), {"tid": _TEMPLATE_ID})
        await s.execute(text("DELETE FROM app.templates WHERE template_id = :tid AND version > 1"), {"tid": _TEMPLATE_ID})
        await s.commit()
