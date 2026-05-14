"""Integration: Stage-1 improvement loop closes end-to-end.

Rubric acceptance test (§4.8 of PLAN.md):
  1. Seed draft A with parties = [{"name": "Smith"}].
  2. POST edit: parties → [{"name": "Smith, et al."}].
  3. Index the edit via FewShotStore.index.
  4. Generate draft B with a deterministic mock router.
  5. Assert the extraction prompt for 'parties' contained "Smith, et al."
     (proof that the few-shot block was injected).
  6. Assert draft B's ai_output["fields"]["parties"]["value"] reflects the
     corrected form (proof the mock echoed the injection end-to-end).
"""
from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

_TEMPLATE_ID = "case_fact_summary"
_CHUNK_ID = "c0ffee00-0000-0000-0000-000000000001"


# ── Helpers ───────────────────────────────────────────────────────────────────


class _UniformEmbedder:
    """1024-dim unit vector; every text maps to the same point → cosine-sim = 1."""
    name = "uniform"
    dim = 1024

    async def embed(self, texts):
        val = 1.0 / math.sqrt(1024)
        return [[val] * 1024 for _ in texts]

    async def health(self):
        return True


def _mock_chunk() -> MagicMock:
    c = MagicMock()
    c.id = _CHUNK_ID
    c.text = "Smith and associates v. Defendant Inc., filed in SDNY."
    return c


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def _seeded(test_session_factory):
    """
    Ensure template in DB; insert draft A (ready) + draft B (queued).
    Yields (draft_a_id, draft_b_id, template_entry, registry).
    """
    from app.core.ids import new_uuid7
    from app.draft.templates.registry import TemplateRegistry
    from app.settings import settings

    registry = TemplateRegistry()
    registry.load_from_disk(settings.TEMPLATES_DIR)
    async with test_session_factory() as s:
        synced = await registry.sync_to_db(s)
        await s.commit()
    entry = next(e for e in synced if e["id"] == _TEMPLATE_ID)

    draft_a_id = new_uuid7()
    draft_b_id = new_uuid7()

    ai_output_a = {
        "fields": {
            "parties": {
                "value": [{"name": "Smith", "role": "plaintiff"}],
                "supporting_chunk_ids": [],
                "confidence": 0.9,
                "error_code": None,
            },
            "jurisdiction": {"value": None, "supporting_chunk_ids": [], "confidence": 0.0, "error_code": None},
            "filing_date":  {"value": None, "supporting_chunk_ids": [], "confidence": 0.0, "error_code": None},
            "claims":        {"value": [],   "supporting_chunk_ids": [], "confidence": 0.0, "error_code": None},
            "damages_sought":{"value": None, "supporting_chunk_ids": [], "confidence": 0.0, "error_code": None},
        },
        "sections_text": {
            "procedural_history": "Filed January 2025.",
            "factual_background":  "Background facts here.",
            "key_issues":          "Main disputed issue.",
        },
        "validators": [],
        "retrieval_meta": {},
        "sections_groundedness": {},
    }

    async with test_session_factory() as s:
        await s.execute(
            text("""
                INSERT INTO app.drafts
                    (id, template_id, template_version, prompt_fingerprint,
                     status, ai_output, generated_at, model_used)
                VALUES (:id, :tid, :ver, :fp,
                        'ready', CAST(:ao AS jsonb), NOW(), 'mock')
            """),
            {
                "id": draft_a_id, "tid": _TEMPLATE_ID,
                "ver": entry["version"], "fp": entry["fingerprint"],
                "ao": json.dumps(ai_output_a),
            },
        )
        for sname in ["procedural_history", "factual_background", "key_issues"]:
            from app.core.ids import new_uuid7 as _uuid7
            await s.execute(
                text("""
                    INSERT INTO app.sections (id, draft_id, name, ai_text)
                    VALUES (gen_random_uuid(), :did, :n, :t)
                """),
                {"did": draft_a_id, "n": sname, "t": ai_output_a["sections_text"][sname]},
            )
        # Draft B starts as 'queued' so the engine can mark it generating.
        await s.execute(
            text("""
                INSERT INTO app.drafts
                    (id, template_id, template_version, prompt_fingerprint, status)
                VALUES (:id, :tid, 0, '', 'queued')
            """),
            {"id": draft_b_id, "tid": _TEMPLATE_ID},
        )
        await s.commit()

    yield draft_a_id, draft_b_id, entry, registry


# ── Test ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stage1_loop_closes(
    _seeded, test_session_factory, cleanup_drafts_and_jobs
):
    """
    Full end-to-end Stage-1 loop:
      edit saved → indexed → next generation prompt contains the correction.
    """
    draft_a_id, draft_b_id, entry, registry = _seeded

    from app.draft.engine import DraftEngine
    from app.edits.few_shot_store import FewShotStore
    from app.edits.service import EditService
    from app.jobs.queue import JobQueue

    # ── 1. Save edit on draft A (parties: Smith → Smith, et al.) ─────────────
    user_output = {
        "fields": {"parties": [{"name": "Smith, et al.", "role": "plaintiff"}]},
        "sections": [],
    }
    async with test_session_factory() as s:
        svc = EditService(session=s, queue=JobQueue(s), registry=registry)
        result = await svc.save_edit(draft_a_id, user_output, trace_id=None)
        await s.commit()

    assert len(result.edit_ids) == 1, "Expected exactly 1 Edit row (parties changed)"
    edit_id = result.edit_ids[0]

    # ── 2. Index the edit (simulate the FEW_SHOT_INDEX job worker step) ───────
    embedder = _UniformEmbedder()
    store = FewShotStore(embedder=embedder)
    async with test_session_factory() as s:
        await store.index(edit_id, session=s)
        await s.commit()

    # Verify the edit is now indexed
    async with test_session_factory() as s:
        row = (await s.execute(
            text("SELECT few_shot_indexed_at FROM app.edits WHERE id = :id"),
            {"id": edit_id},
        )).fetchone()
    assert row[0] is not None, "Edit must be indexed before generating draft B"

    # ── 3. Build mock router ──────────────────────────────────────────────────
    # Captures extraction prompts; echoes "Smith, et al." when it finds the
    # few-shot injection, proving the injection reached the model call.
    captured_extraction_contents: list[str] = []

    async def _mock_generate(messages, *, task, schema=None, sampling=None,
                              trace_id=None, cache=True, **_kw):
        resp = MagicMock()
        resp.tokens_in = 10
        resp.tokens_out = 20
        resp.cost_usd = 0.001
        resp.model_used = "mock"
        resp.latency_ms = 5
        resp.cached_hit = False

        # Concatenate all message content for inspection
        full_content = "".join(
            m.content if hasattr(m, "content") else m.get("content", "")
            for m in messages
        )

        if task == "extraction":
            captured_extraction_contents.append(full_content)
            # If the few-shot block was injected we'll see the corrected value.
            # Echo it back so the final ai_output contains it.
            if "Smith, et al." in full_content:
                resp.structured = {
                    "value": [{"name": "Smith, et al.", "role": "plaintiff"}],
                    "supporting_chunk_ids": [],
                    "confidence": 0.95,
                }
            else:
                resp.structured = {
                    "value": None,
                    "supporting_chunk_ids": [],
                    "confidence": 0.0,
                }
            resp.text = None

        elif task == "generation":
            # No citation references — the test only verifies few-shot injection, not citations.
            resp.text = "Relevant facts from the record."
            resp.structured = None

        elif task == "validation":
            # No citation pairs to validate when the generation has no [chunk:X] refs.
            payload = {"results": []}
            resp.text = json.dumps(payload)
            resp.structured = payload

        else:
            resp.text = ""
            resp.structured = None

        return resp

    router = MagicMock()
    router.generate = AsyncMock(side_effect=_mock_generate)

    # ── 4. Mock retriever — returns one chunk for every retrieval key ─────────
    chunk = _mock_chunk()
    retrieval_keys = [
        "parties", "jurisdiction", "filing_date", "claims", "damages_sought",
        "procedural_history", "factual_background", "key_issues",
    ]
    retriever = MagicMock()
    retriever.multi_retrieve = AsyncMock(
        return_value={k: [chunk] for k in retrieval_keys}
    )

    # ── 5. Generate draft B via the real engine (with real session_factory) ───
    # The engine opens a gen_session for field extraction + section generation,
    # which does real pgvector retrieval (the indexed edit above).
    engine = DraftEngine(
        retriever=retriever,
        llm_router=router,
        registry=registry,
        session_factory=test_session_factory,
        embedder=embedder,
    )
    await engine.generate(
        draft_id=draft_b_id,
        template_id=_TEMPLATE_ID,
        document_ids=["doc-placeholder"],
        trace_id=None,
    )

    # ── 6. Assert few-shot injection appeared in the parties extraction prompt ─
    assert len(captured_extraction_contents) > 0, "No extraction calls captured"

    # The parties extraction prompt contains "Extract 'parties'" in the user
    # message (added by FieldExtractor._extract_field).
    parties_prompt = next(
        (p for p in captured_extraction_contents if "'parties'" in p),
        None,
    )
    assert parties_prompt is not None, (
        "No extraction call for 'parties' was captured. "
        f"Captured prompts: {[p[:80] for p in captured_extraction_contents]}"
    )
    assert "Smith, et al." in parties_prompt, (
        "Expected 'Smith, et al.' (few-shot user_value) in the 'parties' "
        "extraction prompt, but it was absent. Injection did not happen.\n"
        f"Parties prompt excerpt: {parties_prompt[:500]}"
    )

    # ── 7. Assert draft B's finalized ai_output carries the corrected parties ─
    async with test_session_factory() as s:
        row = (await s.execute(
            text("SELECT status, ai_output FROM app.drafts WHERE id = :id"),
            {"id": draft_b_id},
        )).fetchone()

    assert row is not None, "Draft B not found in DB"
    assert row[0] == "ready", f"Draft B status should be 'ready', got: {row[0]!r}"

    ai_output_b = row[1]
    if isinstance(ai_output_b, str):
        ai_output_b = json.loads(ai_output_b)

    parties_value = ai_output_b["fields"]["parties"]["value"]
    assert parties_value is not None, "Draft B parties value should not be None"
    assert isinstance(parties_value, list), f"Expected list, got: {type(parties_value)}"
    assert any(
        isinstance(p, dict) and p.get("name") == "Smith, et al."
        for p in parties_value
    ), (
        f"Expected 'Smith, et al.' in draft B parties value, got: {parties_value}"
    )
