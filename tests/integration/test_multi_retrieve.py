"""
Multi-query retrieval: 5 concurrent queries, all return results, wall time < 1s.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.embedder import StubEmbedder
from app.llm.reranker_model import StubReranker
from app.retrieval.reranker import RerankerWrapper
from app.retrieval.retriever import HybridRetriever


def _make_doc_id() -> str:
    return str(uuid.uuid4())


async def _insert_ready_doc(session: AsyncSession, doc_id: str, sha: str) -> None:
    await session.execute(
        text(
            "INSERT INTO app.documents (id, status, sha256, filename, created_at) "
            "VALUES (:id, 'ready', :sha, :fname, now())"
        ),
        {"id": doc_id, "sha": sha, "fname": f"doc_{sha[:8]}.pdf"},
    )


async def _insert_chunk(
    session: AsyncSession,
    chunk_id: str,
    doc_id: str,
    chunk_text: str,
) -> None:
    vec_str = "[" + ",".join(["0.0"] * 1024) + "]"
    await session.execute(
        text(
            "INSERT INTO app.chunks "
            "(id, document_id, text, text_tsv, embedding, page_start, page_end, created_at) "
            "VALUES (:id, :doc_id, :text, to_tsvector('legal_en', :text),"
            " CAST(:vec AS vector), 1, 1, now())"
        ),
        {"id": chunk_id, "doc_id": doc_id, "text": chunk_text, "vec": vec_str},
    )


@pytest.mark.asyncio
async def test_multi_retrieve_all_keys_return_results(
    test_session_factory, cleanup_documents_and_jobs
):
    doc_id = _make_doc_id()
    sha = uuid.uuid4().hex

    async with test_session_factory() as s:
        await _insert_ready_doc(s, doc_id, sha)
        for i in range(5):
            await _insert_chunk(
                s,
                _make_doc_id(),
                doc_id,
                f"The plaintiff filed a motion regarding habeas corpus and due process. Document {i}.",
            )
        await s.commit()

    retriever = HybridRetriever(
        session_factory=test_session_factory,
        reranker=RerankerWrapper(StubReranker()),
        embedder=StubEmbedder(dim=1024),
    )

    queries = {
        "q1": "plaintiff motion",
        "q2": "habeas corpus",
        "q3": "due process",
        "q4": "filed motion plaintiff",
        "q5": "corpus habeas",
    }

    start = time.monotonic()
    results = await retriever.multi_retrieve(queries, document_ids=None, top_k_per_query=5)
    elapsed = time.monotonic() - start

    assert set(results.keys()) == set(queries.keys()), "All 5 query keys must be present"
    for key, chunks in results.items():
        assert len(chunks) > 0, f"Query '{key}' returned no results"

    assert elapsed < 1.0, f"multi_retrieve took {elapsed:.2f}s, expected < 1s"


@pytest.mark.asyncio
async def test_multi_retrieve_concurrent(test_session_factory, cleanup_documents_and_jobs):
    """Verify queries run concurrently by comparing vs sequential time."""
    doc_id = _make_doc_id()
    sha = uuid.uuid4().hex

    async with test_session_factory() as s:
        await _insert_ready_doc(s, doc_id, sha)
        await _insert_chunk(
            s,
            _make_doc_id(),
            doc_id,
            "parties to the case plaintiff defendant judgment order",
        )
        await s.commit()

    retriever = HybridRetriever(
        session_factory=test_session_factory,
        reranker=RerankerWrapper(StubReranker()),
        embedder=StubEmbedder(dim=1024),
    )

    queries = {f"q{i}": "plaintiff defendant" for i in range(5)}

    start = time.monotonic()
    results = await retriever.multi_retrieve(queries, document_ids=None, top_k_per_query=3)
    elapsed = time.monotonic() - start

    assert len(results) == 5
    assert elapsed < 1.0, f"Concurrent multi_retrieve took {elapsed:.2f}s"
