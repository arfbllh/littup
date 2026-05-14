"""Integration: embedding resume after simulated mid-document crash (NN-1).

Verifies:
1. _claim_embedding_running accepts status='embedding_running' (crash resume fix)
2. embed_chunks SELECTs WHERE embedding IS NULL (picks up unfinished work)
3. Final state reaches 'ready' even after partial embedding
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


async def _setup_chunked_doc(service, m5_fixture_docs) -> str:
    """Upload scan_clean.pdf and advance to chunking_done. Returns document_id."""
    from io import BytesIO
    from fastapi import UploadFile

    pdf_path = m5_fixture_docs / "scan_clean.pdf"
    upload = UploadFile(filename="scan_clean.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await service.upload(upload)
    doc_id = result.document_id
    await service.ocr_document(doc_id)
    await service.parse_layout(doc_id)
    await service.chunk_document(doc_id)
    return doc_id


@pytest.mark.asyncio
async def test_embedding_resume_after_partial_crash(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """Partial embedding + crash simulation → resume → all embedded → ready.

    Simulates a crash by manually:
      1. Setting status='embedding_running' (as if a worker claimed it)
      2. Embedding only the first chunk
      3. Calling embed_chunks again — should resume from remaining unembedded chunks
    """
    from app.llm.embedder import StubEmbedder
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    doc_id = await _setup_chunked_doc(service, m5_fixture_docs)

    # Fetch all chunks
    chunks_q = await db_session.execute(
        text("SELECT id FROM app.chunks WHERE document_id = :id ORDER BY id"),
        {"id": doc_id},
    )
    all_chunk_ids = [str(r.id) for r in chunks_q.fetchall()]
    assert len(all_chunk_ids) > 0, "Expected chunks after chunking_done"

    # Simulate crash: flip status + embed only the first chunk
    await db_session.execute(
        text(
            "UPDATE app.documents SET status='embedding_running', updated_at=NOW() "
            "WHERE id=:id"
        ),
        {"id": doc_id},
    )
    stub_vec = "[" + ",".join("0.0" for _ in range(1024)) + "]"
    await db_session.execute(
        text("UPDATE app.chunks SET embedding = CAST(:v AS vector) WHERE id = :id"),
        {"v": stub_vec, "id": all_chunk_ids[0]},
    )
    await db_session.commit()

    # Verify pre-resume state
    before_q = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM app.chunks "
            "WHERE document_id=:id AND embedding IS NOT NULL"
        ),
        {"id": doc_id},
    )
    assert before_q.scalar_one() == 1, "Only the first chunk should be embedded before resume"

    # Resume — must NOT skip (NN-1 fix: claim accepts embedding_running)
    result = await service.embed_chunks(doc_id, StubEmbedder())
    assert result.get("skipped") is not True, (
        "embed_chunks must not skip an embedding_running document"
    )

    # All chunks embedded
    after_q = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM app.chunks "
            "WHERE document_id=:id AND embedding IS NOT NULL"
        ),
        {"id": doc_id},
    )
    total_q = await db_session.execute(
        text("SELECT COUNT(*) FROM app.chunks WHERE document_id=:id"),
        {"id": doc_id},
    )
    embedded = after_q.scalar_one()
    total = total_q.scalar_one()
    assert embedded == total, f"All {total} chunks must be embedded; only {embedded} are"

    # Status is ready
    status_q = await db_session.execute(
        text("SELECT status, embedded_at FROM app.documents WHERE id=:id"),
        {"id": doc_id},
    )
    doc = status_q.fetchone()
    assert doc.status == "ready"
    assert doc.embedded_at is not None


@pytest.mark.asyncio
async def test_embedding_from_zero_after_crash(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """Crash immediately after claim (zero chunks embedded) → resume embeds all."""
    from app.llm.embedder import StubEmbedder
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    doc_id = await _setup_chunked_doc(service, m5_fixture_docs)

    # Simulate crash: just flip status, no chunks embedded
    await db_session.execute(
        text(
            "UPDATE app.documents SET status='embedding_running', updated_at=NOW() "
            "WHERE id=:id"
        ),
        {"id": doc_id},
    )
    await db_session.commit()

    result = await service.embed_chunks(doc_id, StubEmbedder())
    assert result.get("skipped") is not True

    status_q = await db_session.execute(
        text("SELECT status FROM app.documents WHERE id=:id"), {"id": doc_id}
    )
    assert status_q.scalar_one() == "ready"

    null_q = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM app.chunks "
            "WHERE document_id=:id AND embedding IS NULL"
        ),
        {"id": doc_id},
    )
    assert null_q.scalar_one() == 0, "No unembedded chunks should remain after resume"


@pytest.mark.asyncio
async def test_embed_chunks_idempotent_on_ready(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """A second embed_chunks call on an already-ready document must skip gracefully."""
    from app.llm.embedder import StubEmbedder
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    doc_id = await _setup_chunked_doc(service, m5_fixture_docs)

    await service.embed_chunks(doc_id, StubEmbedder())

    # Second call — status is 'ready', not 'chunking_done' or 'embedding_running'
    second = await service.embed_chunks(doc_id, StubEmbedder())
    assert second.get("skipped") is True, (
        "embed_chunks on an already-ready document must be skipped"
    )
