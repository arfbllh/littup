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


async def _insert_doc(session, status: str = "ready") -> str:
    doc_id = str(uuid.uuid4())
    sha = uuid.uuid4().hex
    await session.execute(
        text(
            "INSERT INTO app.documents (id, status, sha256, filename, created_at) "
            "VALUES (:id, :status, :sha, :fname, now())"
        ),
        {"id": doc_id, "status": status, "sha": sha, "fname": f"{sha}.pdf"},
    )
    return doc_id


async def _insert_chunk(session, doc_id: str, chunk_text: str) -> str:
    chunk_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO app.chunks (id, document_id, text, embedding, page_start, page_end, created_at) "
            "VALUES (:id, :doc_id, :text, CAST(:vec AS vector), 1, 1, now())"
        ),
        {"id": chunk_id, "doc_id": doc_id, "text": chunk_text, "vec": _vec_str()},
    )
    return chunk_id


@pytest.mark.asyncio
async def test_dense_retriever_returns_ready_chunks(db_session):
    from app.retrieval.dense import DenseRetriever

    doc1 = await _insert_doc(db_session)
    doc2 = await _insert_doc(db_session)
    doc3 = await _insert_doc(db_session)

    chunk1 = await _insert_chunk(db_session, doc1, "The plaintiff filed a complaint.")
    chunk2 = await _insert_chunk(db_session, doc2, "The defendant denied all allegations.")
    chunk3 = await _insert_chunk(db_session, doc3, "Parties to the case agreed to mediation.")

    await db_session.flush()

    retriever = DenseRetriever(db_session)
    results = await retriever.search([0.0] * 1024, None, 10)

    assert len(results) >= 3
    returned_ids = {chunk_id for chunk_id, _ in results}
    assert chunk1 in returned_ids
    assert chunk2 in returned_ids
    assert chunk3 in returned_ids
    for _, score in results:
        assert isinstance(score, float)


@pytest.mark.asyncio
async def test_dense_retriever_filters_by_document_ids(db_session):
    from app.retrieval.dense import DenseRetriever

    doc_in = await _insert_doc(db_session)
    doc_out = await _insert_doc(db_session)

    chunk_in = await _insert_chunk(db_session, doc_in, "plaintiff seeks damages")
    chunk_out = await _insert_chunk(db_session, doc_out, "defendant objects to jurisdiction")

    await db_session.flush()

    retriever = DenseRetriever(db_session)
    results = await retriever.search([0.0] * 1024, [doc_in], 10)

    returned_ids = {chunk_id for chunk_id, _ in results}
    assert chunk_in in returned_ids
    assert chunk_out not in returned_ids


@pytest.mark.asyncio
async def test_dense_retriever_excludes_non_ready_docs(db_session):
    from app.retrieval.dense import DenseRetriever

    doc_ready = await _insert_doc(db_session, status="ready")
    doc_pending = await _insert_doc(db_session, status="ocr_pending")

    chunk_ready = await _insert_chunk(db_session, doc_ready, "ready document chunk")
    chunk_pending = await _insert_chunk(db_session, doc_pending, "pending document chunk")

    await db_session.flush()

    retriever = DenseRetriever(db_session)
    results = await retriever.search([0.0] * 1024, None, 10)

    returned_ids = {chunk_id for chunk_id, _ in results}
    assert chunk_ready in returned_ids
    assert chunk_pending not in returned_ids


@pytest.mark.asyncio
async def test_dense_retriever_empty_embedding_returns_empty(db_session):
    from app.retrieval.dense import DenseRetriever

    retriever = DenseRetriever(db_session)
    results = await retriever.search([], None, 10)
    assert results == []


# ── HybridRetriever end-to-end ────────────────────────────────────────────────


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


async def _insert_chunk_with_tsv(
    session: AsyncSession, doc_id: str, chunk_text: str
) -> str:
    chunk_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO app.chunks "
            "(id, document_id, text, text_tsv, embedding, page_start, page_end, created_at) "
            "VALUES (:id, :doc_id, :text, to_tsvector('legal_en', :text),"
            " CAST(:vec AS vector), 1, 1, now())"
        ),
        {"id": chunk_id, "doc_id": doc_id, "text": chunk_text, "vec": _vec_str()},
    )
    return chunk_id


@pytest.mark.asyncio
async def test_hybrid_retriever_returns_plaintiff_defendant_chunks(
    test_session_factory, cleanup_documents_and_jobs
):
    """Full pipeline: BM25 + dense + RRF + rerank returns legally relevant chunks."""
    async with test_session_factory() as s:
        doc1 = await _insert_ready_doc(s)
        doc2 = await _insert_ready_doc(s)
        doc3 = await _insert_ready_doc(s)

        await _insert_chunk_with_tsv(
            s, doc1, "The plaintiff filed a complaint seeking damages from the defendant."
        )
        await _insert_chunk_with_tsv(
            s, doc2, "Both parties to the case appeared before the court for the hearing."
        )
        await _insert_chunk_with_tsv(
            s, doc3, "The defendant denied all allegations raised by the plaintiff."
        )
        await s.commit()

    retriever = HybridRetriever(
        session_factory=test_session_factory,
        reranker=RerankerWrapper(StubReranker()),
        embedder=StubEmbedder(dim=1024),
    )
    results = await retriever.retrieve("parties to the case", document_ids=None, top_k=5)

    assert len(results) > 0
    all_text = " ".join(c.text for c in results).lower()
    assert "plaintiff" in all_text or "defendant" in all_text or "parties" in all_text


@pytest.mark.asyncio
async def test_hybrid_retriever_per_doc_cap(
    test_session_factory, cleanup_documents_and_jobs, monkeypatch
):
    """At most MAX_CHUNKS_PER_DOC_PER_QUERY chunks are returned from a single document."""
    from app.settings import settings

    cap = 3
    monkeypatch.setattr(settings, "MAX_CHUNKS_PER_DOC_PER_QUERY", cap)

    async with test_session_factory() as s:
        doc_id = await _insert_ready_doc(s)
        for i in range(5):
            await _insert_chunk_with_tsv(
                s,
                doc_id,
                f"The plaintiff filed a motion for habeas corpus relief. Document {i}.",
            )
        await s.commit()

    retriever = HybridRetriever(
        session_factory=test_session_factory,
        reranker=RerankerWrapper(StubReranker()),
        embedder=StubEmbedder(dim=1024),
    )
    results = await retriever.retrieve(
        "plaintiff habeas corpus motion", document_ids=None, top_k=10
    )

    doc_ids_returned = [c.document_id for c in results]
    count_from_doc = doc_ids_returned.count(doc_id)
    assert count_from_doc <= cap, (
        f"Expected at most {cap} chunks from one doc, got {count_from_doc}"
    )


@pytest.mark.asyncio
async def test_hybrid_retriever_document_id_filter(
    test_session_factory, cleanup_documents_and_jobs
):
    """document_ids filter restricts results to specified documents."""
    async with test_session_factory() as s:
        doc_in = await _insert_ready_doc(s)
        doc_out = await _insert_ready_doc(s)

        chunk_in = await _insert_chunk_with_tsv(
            s, doc_in, "The plaintiff filed a motion in federal court."
        )
        await _insert_chunk_with_tsv(
            s, doc_out, "The plaintiff filed a motion in state court."
        )
        await s.commit()

    retriever = HybridRetriever(
        session_factory=test_session_factory,
        reranker=RerankerWrapper(StubReranker()),
        embedder=StubEmbedder(dim=1024),
    )
    results = await retriever.retrieve(
        "plaintiff motion", document_ids=[doc_in], top_k=5
    )

    returned_doc_ids = {c.document_id for c in results}
    assert doc_out not in returned_doc_ids
    assert chunk_in in {c.id for c in results}
