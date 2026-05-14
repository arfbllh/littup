"""Integration test for eval/run_retrieval.py.

Inserts a minimal ready document + chunks into the test DB, patches
HybridRetriever.retrieve to return mock chunks containing the expected
keywords, then asserts the eval script returns well-formed metrics and
writes the report file.

No real LLM, embedder, or BM25 index is exercised.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tests.integration.conftest import TEST_DB_URL

# ── Inline query fixtures (3 rows covering 3 tags) ────────────────────────────

_QUERIES = [
    {
        "id": "lt1",
        "query": "section 1983 civil rights liability color of state law",
        "document_id": "scan_clean",
        "tag": "legal_term",
        "expected_chunk_keywords": ["§ 1983"],
    },
    {
        "id": "pn1",
        "query": "Harvey Specter attorney plaintiff submits brief",
        "document_id": "scan_clean",
        "tag": "proper_noun",
        "expected_chunk_keywords": ["Harvey Specter"],
    },
    {
        "id": "pr1",
        "query": "summary judgment motion plaintiff constitutional rights court",
        "document_id": "scan_clean",
        "tag": "prose",
        "expected_chunk_keywords": ["summary judgment"],
    },
]

# Text for each mock chunk — must contain the keyword for that query.
_CHUNK_TEXTS = [
    "Plaintiff brings this action under 42 U.S.C. § 1983 alleging deprivation of rights.",
    "Attorney Harvey Specter filed the motion on behalf of the plaintiff.",
    "The court grants summary judgment where no genuine issue of material fact exists.",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_mock_chunk(text: str, doc_uuid: str) -> Any:
    """Return a lightweight object with a .text attribute (like app.db.models.chunk.Chunk)."""
    from app.db.models.chunk import Chunk

    c = Chunk()
    from app.core.ids import new_uuid7
    c.id = new_uuid7()
    c.document_id = doc_uuid
    c.text = text
    c.section_path = []
    c.page_start = 1
    c.page_end = 1
    c.chunk_type = "paragraph"
    c.token_count = len(text.split())
    c.block_ids = []
    c.entities = []
    c.char_start = 0
    c.char_end = len(text)
    c.prompt_fingerprint = None
    c.metadata_ = {}
    return c


async def _insert_ready_document(session_factory, doc_uuid: str) -> None:
    """Insert a minimal document row with status='ready' and filename='scan_clean'."""
    from sqlalchemy import text as sqlt

    async with session_factory() as s:
        await s.execute(
            sqlt(
                """
                INSERT INTO app.documents
                    (id, sha256, filename, status, created_at, updated_at)
                VALUES
                    (:id, :sha256, :filename, 'ready', now(), now())
                ON CONFLICT (sha256) DO NOTHING
                """
            ),
            {
                "id": doc_uuid,
                "sha256": f"test-sha256-{doc_uuid}",
                "filename": "scan_clean",
            },
        )
        await s.commit()


async def _cleanup(session_factory, doc_uuid: str) -> None:
    from sqlalchemy import text as sqlt

    async with session_factory() as s:
        # chunks cascade-delete when document is deleted
        await s.execute(
            sqlt("DELETE FROM app.documents WHERE id = :id"),
            {"id": doc_uuid},
        )
        await s.commit()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def retrieval_eval_setup(test_session_factory, tmp_path):
    """Write query JSONL and insert a ready document; yield (tmp_queries_path, doc_uuid).
    Cleans up after the test.
    """
    from app.core.ids import new_uuid7

    doc_uuid = new_uuid7()

    # Write queries JSONL
    queries_path = tmp_path / "queries.jsonl"
    with queries_path.open("w") as fh:
        for row in _QUERIES:
            fh.write(json.dumps(row) + "\n")

    await _insert_ready_document(test_session_factory, doc_uuid)

    yield queries_path, doc_uuid

    await _cleanup(test_session_factory, doc_uuid)


# ── Test ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_eval_retrieval_runs(
    retrieval_eval_setup,
    test_session_factory,
    monkeypatch,
    tmp_path,
):
    """eval/run_retrieval.run() returns well-formed metrics and writes the report."""
    from eval.common import EvalContext
    from eval.run_retrieval import run as run_retrieval
    from app.llm.embedder import StubEmbedder
    from app.llm.reranker_model import StubReranker
    from app.retrieval.reranker import RerankerWrapper
    from app.retrieval.retriever import HybridRetriever

    queries_path, doc_uuid = retrieval_eval_setup

    # Build mock chunks — one per query, each containing the expected keyword.
    mock_chunks = [_make_mock_chunk(text, doc_uuid) for text in _CHUNK_TEXTS]

    # Patch HybridRetriever.retrieve to return the mock chunks regardless of query.
    async def _mock_retrieve(self, query, document_ids, top_k=8, fetch_k=50):
        return mock_chunks

    monkeypatch.setattr(HybridRetriever, "retrieve", _mock_retrieve)

    # Patch eval.run_retrieval.QUERIES_PATH to point at our 3-row fixture.
    import eval.run_retrieval as _module
    monkeypatch.setattr(_module, "QUERIES_PATH", queries_path)

    # Redirect reports dir to tmp_path so we don't pollute the real reports/
    import eval.common as _common_module
    original_reports_dir = _common_module.REPORTS_DIR
    monkeypatch.setattr(_common_module, "REPORTS_DIR", tmp_path / "reports")

    # Run with mock_llm=True so build_context uses StubEmbedder (no GPU needed).
    result = await run_retrieval(
        db_url=TEST_DB_URL,
        mock_llm=True,
        query_limit=3,
    )

    # ── Assertions: return value structure ────────────────────────────────────
    assert isinstance(result, dict), "run() must return a dict"

    for key in ("recall_at_5", "recall_at_10", "mrr", "per_tag"):
        assert key in result, f"Missing key '{key}' in result"

    assert isinstance(result["recall_at_5"], float), "recall_at_5 must be a float"
    assert isinstance(result["recall_at_10"], float), "recall_at_10 must be a float"
    assert isinstance(result["mrr"], float), "mrr must be a float"
    assert isinstance(result["per_tag"], dict), "per_tag must be a dict"

    assert 0.0 <= result["recall_at_5"] <= 1.0, "recall_at_5 out of range [0, 1]"
    assert 0.0 <= result["recall_at_10"] <= 1.0, "recall_at_10 out of range [0, 1]"
    assert 0.0 <= result["mrr"] <= 1.0, "mrr out of range [0, 1]"

    assert len(result["per_tag"]) >= 1, "per_tag must have at least one tag"

    # All three queries should hit (mock chunks contain the keywords).
    assert result["recall_at_5"] == 1.0, "All 3 mock queries should hit at rank <= 5"
    assert result["recall_at_10"] == 1.0, "All 3 mock queries should hit at rank <= 10"

    # Each per_tag entry must contain the three metric keys.
    for tag, tag_metrics in result["per_tag"].items():
        for sub_key in ("recall_at_5", "recall_at_10", "mrr"):
            assert sub_key in tag_metrics, f"per_tag[{tag!r}] missing '{sub_key}'"

    # ── Assertions: report files written ──────────────────────────────────────
    report_json_path = tmp_path / "reports" / "retrieval.json"
    assert report_json_path.exists(), "retrieval.json report file was not created"

    with report_json_path.open() as fh:
        report_data = json.load(fh)

    for key in ("recall_at_5", "recall_at_10", "mrr", "per_tag"):
        assert key in report_data, f"Report JSON missing key '{key}'"

    report_md_path = tmp_path / "reports" / "retrieval.md"
    assert report_md_path.exists(), "retrieval.md report file was not created"
    md_content = report_md_path.read_text()
    assert "Recall@5" in md_content, "Markdown report should contain 'Recall@5'"
    assert "Recall@10" in md_content, "Markdown report should contain 'Recall@10'"
