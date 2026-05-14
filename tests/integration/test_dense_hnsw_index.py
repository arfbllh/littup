from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


def _vec_str() -> str:
    return "[" + ",".join(["0.0"] * 1024) + "]"


@pytest.mark.asyncio
async def test_hnsw_index_used(db_session):
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
            "VALUES (:id, :doc_id, :text, CAST(:vec AS vector), 1, 1, now())"
        ),
        {"id": chunk_id, "doc_id": doc_id, "text": "test chunk for hnsw index", "vec": _vec_str()},
    )

    await db_session.flush()

    # Query without JOIN so the planner must choose between seq-scan (disabled)
    # and the HNSW index. With enable_seqscan=off it has no choice but to use HNSW.
    async with db_session.begin_nested():
        await db_session.execute(text("SET LOCAL enable_seqscan = off"))
        result = await db_session.execute(
            text(
                "EXPLAIN ANALYZE SELECT id FROM app.chunks "
                "ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT 5"
            ),
            {"qvec": _vec_str()},
        )
        rows = result.fetchall()

    explain_text = "\n".join(str(r[0]) for r in rows)
    assert "chunks_embedding_hnsw_idx" in explain_text
