"""
Integration test: entity overlap bonus is wired through HybridRetriever.retrieve().

Two chunks share the same base text (so BM25 and dense give them equal scores).
One chunk has 'Pearson Specter Litt' in its entities column.
A query containing "Pearson Specter Litt" triggers extract_entities(), which produces
an entity bonus of +0.1 for the matching chunk.  Since 0.1 >> max RRF score (~0.016),
the entity chunk must rank first regardless of tie-breaking in BM25 or dense.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.embedder import StubEmbedder
from app.llm.reranker_model import StubReranker
from app.retrieval.reranker import RerankerWrapper
from app.retrieval.retriever import HybridRetriever


def _vec_str() -> str:
    return "[" + ",".join(["0.0"] * 1024) + "]"


async def _insert_ready_doc(session: AsyncSession) -> str:
    doc_id = str(uuid.uuid4())
    sha = uuid.uuid4().hex
    await session.execute(
        text(
            "INSERT INTO app.documents (id, status, sha256, filename, created_at) "
            "VALUES (:id, 'ready', :sha, :fname, now())"
        ),
        {"id": doc_id, "sha": sha, "fname": f"{sha}.pdf"},
    )
    return doc_id


@pytest.mark.asyncio
async def test_entity_bonus_promotes_matching_chunk(
    test_session_factory, cleanup_documents_and_jobs
):
    chunk_text = "A motion was filed in federal court seeking relief from the judgment."

    async with test_session_factory() as s:
        doc_id = await _insert_ready_doc(s)

        # chunk_entity: has matching entity in entities column
        chunk_entity_id = str(uuid.uuid4())
        await s.execute(
            text(
                "INSERT INTO app.chunks "
                "(id, document_id, text, text_tsv, embedding, entities, page_start, page_end, created_at) "
                "VALUES (:id, :doc_id, :text, to_tsvector('legal_en', :text),"
                " CAST(:vec AS vector), ARRAY['Pearson Specter Litt']::text[], 1, 1, now())"
            ),
            {"id": chunk_entity_id, "doc_id": doc_id, "text": chunk_text, "vec": _vec_str()},
        )

        # chunk_no_entity: identical text, no entities
        chunk_no_entity_id = str(uuid.uuid4())
        await s.execute(
            text(
                "INSERT INTO app.chunks "
                "(id, document_id, text, text_tsv, embedding, page_start, page_end, created_at) "
                "VALUES (:id, :doc_id, :text, to_tsvector('legal_en', :text),"
                " CAST(:vec AS vector), 2, 2, now())"
            ),
            {"id": chunk_no_entity_id, "doc_id": doc_id, "text": chunk_text, "vec": _vec_str()},
        )
        await s.commit()

    retriever = HybridRetriever(
        session_factory=test_session_factory,
        reranker=RerankerWrapper(StubReranker()),
        embedder=StubEmbedder(dim=1024),
    )

    # Query contains "Pearson Specter Litt" → extract_entities returns it → +0.1 bonus
    results = await retriever.retrieve(
        "Pearson Specter Litt motion federal court", document_ids=None, top_k=5
    )

    assert len(results) >= 1, "Expected at least one result"
    assert results[0].id == chunk_entity_id, (
        f"Entity-bearing chunk should rank first; got {results[0].id!r}"
    )


@pytest.mark.asyncio
async def test_entity_bonus_no_crash_with_null_entities(
    test_session_factory, cleanup_documents_and_jobs
):
    """Chunks with NULL entities column should not cause errors."""
    async with test_session_factory() as s:
        doc_id = await _insert_ready_doc(s)
        chunk_id = str(uuid.uuid4())
        await s.execute(
            text(
                "INSERT INTO app.chunks "
                "(id, document_id, text, text_tsv, embedding, page_start, page_end, created_at) "
                "VALUES (:id, :doc_id, :text, to_tsvector('legal_en', :text),"
                " CAST(:vec AS vector), 1, 1, now())"
            ),
            {
                "id": chunk_id,
                "doc_id": doc_id,
                "text": "The plaintiff filed a complaint.",
                "vec": _vec_str(),
            },
        )
        await s.commit()

    retriever = HybridRetriever(
        session_factory=test_session_factory,
        reranker=RerankerWrapper(StubReranker()),
        embedder=StubEmbedder(dim=1024),
    )
    results = await retriever.retrieve("plaintiff complaint", document_ids=None, top_k=5)
    assert len(results) >= 1
