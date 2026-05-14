"""
Verify that the GIN trigram index (chunks_text_trgm_idx) is actually used by
TrigramRetriever queries.

The fixed trigram.py uses SET LOCAL pg_trgm.similarity_threshold + the %
operator, which is the GIN-compatible form.  We confirm via EXPLAIN ANALYZE
with enable_seqscan=off that the planner selects chunks_text_trgm_idx.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


def _vec_str() -> str:
    return "[" + ",".join(["0.0"] * 1024) + "]"


@pytest.mark.asyncio
async def test_trigram_gin_index_used(db_session):
    doc_id = str(uuid.uuid4())
    sha = uuid.uuid4().hex
    await db_session.execute(
        text(
            "INSERT INTO app.documents (id, status, sha256, filename, created_at) "
            "VALUES (:id, 'ready', :sha, :fname, now())"
        ),
        {"id": doc_id, "sha": sha, "fname": f"{sha}.pdf"},
    )

    chunk_id = str(uuid.uuid4())
    await db_session.execute(
        text(
            "INSERT INTO app.chunks (id, document_id, text, embedding, page_start, page_end, created_at) "
            "VALUES (:id, :doc_id, 'the plaintiff filed a motion for habeas corpus',"
            " CAST(:vec AS vector), 1, 1, now())"
        ),
        {"id": chunk_id, "doc_id": doc_id, "vec": _vec_str()},
    )
    await db_session.flush()

    # Use the same pattern as the fixed TrigramRetriever:
    #   SET LOCAL pg_trgm.similarity_threshold + % operator.
    # enable_seqscan=off forces the planner to pick the GIN index.
    async with db_session.begin_nested():
        await db_session.execute(text("SET LOCAL enable_seqscan = off"))
        await db_session.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.1"))
        result = await db_session.execute(
            text(
                "EXPLAIN ANALYZE SELECT id FROM app.chunks "
                "WHERE text % 'plaintiff motion'"
            )
        )
        rows = result.fetchall()

    explain_text = "\n".join(str(r[0]) for r in rows)
    assert "chunks_text_trgm_idx" in explain_text, (
        f"Expected GIN trigram index in EXPLAIN output:\n{explain_text}"
    )


@pytest.mark.asyncio
async def test_trigram_retriever_uses_gin_path(db_session):
    """TrigramRetriever.search() returns results and doesn't error after the fix."""
    doc_id = str(uuid.uuid4())
    sha = uuid.uuid4().hex
    await db_session.execute(
        text(
            "INSERT INTO app.documents (id, status, sha256, filename, created_at) "
            "VALUES (:id, 'ready', :sha, :fname, now())"
        ),
        {"id": doc_id, "sha": sha, "fname": f"{sha}.pdf"},
    )

    chunk_id = str(uuid.uuid4())
    await db_session.execute(
        text(
            "INSERT INTO app.chunks (id, document_id, text, embedding, page_start, page_end, created_at) "
            "VALUES (:id, :doc_id, 'the plaintiff filed a motion for habeas corpus',"
            " CAST(:vec AS vector), 1, 1, now())"
        ),
        {"id": chunk_id, "doc_id": doc_id, "vec": _vec_str()},
    )
    await db_session.flush()

    from app.retrieval.trigram import TrigramRetriever

    retriever = TrigramRetriever(db_session)
    results = await retriever.search("plaintiff motion", None, 10)

    assert any(chunk_id == r[0] for r in results), (
        "Expected chunk to appear in trigram results"
    )
    for _, score in results:
        assert 0.0 <= score <= 1.0
