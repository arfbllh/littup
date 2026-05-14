"""Integration: full ingestion pipeline — upload → OCR → layout → chunk → embed → ready.

Uses StubEmbedder (zero vectors) so no model download is required in CI.
M5 acceptance criteria:
  - status reaches 'ready'
  - all chunks have non-null embeddings (1024-d)
  - at least one chunk has non-empty entities array
  - get_blocks() returns blocks after layout
  - each intermediate status is observed in sequence
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


async def _run_full_pipeline(service, doc_id: str) -> None:
    from app.llm.embedder import StubEmbedder
    await service.ocr_document(doc_id)
    await service.parse_layout(doc_id)
    await service.chunk_document(doc_id)
    await service.embed_chunks(doc_id, StubEmbedder())


@pytest.mark.asyncio
async def test_full_pipeline_reaches_ready(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """scan_clean.pdf → all stages → status='ready' with embedded_at set."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    pdf_path = m5_fixture_docs / "scan_clean.pdf"
    upload = UploadFile(filename="scan_clean.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await service.upload(upload)
    doc_id = result.document_id

    await _run_full_pipeline(service, doc_id)

    row = await db_session.execute(
        text("SELECT status, embedded_at FROM app.documents WHERE id = :id"), {"id": doc_id}
    )
    doc = row.fetchone()
    assert doc.status == "ready", f"Expected status='ready', got '{doc.status}'"
    assert doc.embedded_at is not None, "embedded_at must be set when status='ready'"


@pytest.mark.asyncio
async def test_chunks_have_non_null_embeddings(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """Every chunk for a fully-ingested document must have a non-null embedding."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    pdf_path = m5_fixture_docs / "scan_clean.pdf"
    upload = UploadFile(filename="scan_clean.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await service.upload(upload)
    doc_id = result.document_id
    await _run_full_pipeline(service, doc_id)

    total_q = await db_session.execute(
        text("SELECT COUNT(*) FROM app.chunks WHERE document_id = :id"), {"id": doc_id}
    )
    embedded_q = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM app.chunks "
            "WHERE document_id = :id AND embedding IS NOT NULL"
        ),
        {"id": doc_id},
    )
    total = total_q.scalar_one()
    embedded = embedded_q.scalar_one()
    assert total > 0, "Expected at least one chunk"
    assert total == embedded, f"{total} chunks total but only {embedded} embedded"


@pytest.mark.asyncio
async def test_chunks_have_non_empty_entities(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """At least one chunk must have a non-empty entities array.

    scan_clean.pdf contains '42 U.S.C. § 1983', 'Brown v. Board',
    'Harvey Specter', '$75,000', 'January 15, 2026'.
    """
    from io import BytesIO
    from fastapi import UploadFile
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    pdf_path = m5_fixture_docs / "scan_clean.pdf"
    upload = UploadFile(filename="scan_clean.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await service.upload(upload)
    doc_id = result.document_id
    await _run_full_pipeline(service, doc_id)

    entity_q = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM app.chunks "
            "WHERE document_id = :id AND cardinality(entities) > 0"
        ),
        {"id": doc_id},
    )
    count = entity_q.scalar_one()
    assert count > 0, "Expected at least one chunk with non-empty entities"


@pytest.mark.asyncio
async def test_get_blocks_returns_data_after_layout(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """get_blocks() must return actual blocks after layout completes."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    pdf_path = m5_fixture_docs / "scan_clean.pdf"
    upload = UploadFile(filename="scan_clean.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await service.upload(upload)
    doc_id = result.document_id

    await service.ocr_document(doc_id)
    await service.parse_layout(doc_id)

    payload = await service.get_blocks(doc_id)
    assert payload["status"] == "layout_done"
    assert len(payload["blocks"]) > 0, "get_blocks must return blocks after layout"
    first = payload["blocks"][0]
    for key in ("id", "block_type", "text"):
        assert key in first, f"Missing key '{key}' in block: {first}"


@pytest.mark.asyncio
async def test_pipeline_state_sequence(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """Each pipeline stage must set the expected intermediate status."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.llm.embedder import StubEmbedder
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    pdf_path = m5_fixture_docs / "scan_clean.pdf"
    upload = UploadFile(filename="scan_clean.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await service.upload(upload)
    doc_id = result.document_id

    async def _status() -> str:
        r = await db_session.execute(
            text("SELECT status FROM app.documents WHERE id = :id"), {"id": doc_id}
        )
        return r.scalar_one()

    await service.ocr_document(doc_id)
    assert await _status() == "ocr_done"

    await service.parse_layout(doc_id)
    assert await _status() == "layout_done"

    await service.chunk_document(doc_id)
    assert await _status() == "chunking_done"

    await service.embed_chunks(doc_id, StubEmbedder())
    assert await _status() == "ready"
