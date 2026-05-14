"""Integration: native PDF → pdfplumber path, confidence 1.0, text intact."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text


@pytest.mark.asyncio
async def test_native_uses_pdfplumber(
    ocr_fixture_docs,
    ocr_ingest_service,
    db_session,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
):
    """native_clean.pdf must go through pdfplumber (no rasterisation).

    Acceptance criteria:
    - status = ocr_done
    - all spans have source = pdfplumber and confidence = 1.0
    - known text ("Pearson", "Specter") appears in extracted spans
    - vlm_pages_used = 0 (no VLM call for native PDFs — cost discipline)
    """
    from io import BytesIO
    from fastapi import UploadFile

    pdf_path = ocr_fixture_docs / "native_clean.pdf"
    upload = UploadFile(filename="native_clean.pdf", file=BytesIO(pdf_path.read_bytes()))

    result = await ocr_ingest_service.upload(upload)
    assert result.was_new, "Document should be new"
    doc_id = result.document_id

    ocr_result = await ocr_ingest_service.ocr_document(doc_id)

    # Status
    row = await db_session.execute(
        text("SELECT status, vlm_pages_used FROM app.documents WHERE id = :id"),
        {"id": doc_id},
    )
    doc = row.fetchone()
    assert doc.status == "ocr_done"
    assert doc.vlm_pages_used == 0

    # Spans
    spans_q = await db_session.execute(
        text(
            """
            SELECT s.text, s.source, s.confidence
              FROM app.spans s
              JOIN app.pages p ON p.id = s.page_id
             WHERE p.document_id = :d
            """
        ),
        {"d": doc_id},
    )
    spans = spans_q.fetchall()
    assert len(spans) > 5, "Expected multiple word spans from a native PDF"

    for s in spans:
        assert s.source == "pdfplumber", f"Unexpected source: {s.source}"
        assert float(s.confidence) == 1.0, f"pdfplumber span confidence must be 1.0, got {s.confidence}"

    all_text = " ".join(s.text for s in spans).lower()
    assert "pearson" in all_text, f"'Pearson' not found in extracted text: {all_text[:200]}"
    assert "specter" in all_text, f"'Specter' not found in extracted text: {all_text[:200]}"


@pytest.mark.asyncio
async def test_native_is_idempotent(
    ocr_fixture_docs,
    ocr_ingest_service,
    db_session,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
):
    """Re-running ocr_document on an already-done document is a no-op."""
    from io import BytesIO
    from fastapi import UploadFile

    pdf_path = ocr_fixture_docs / "native_clean.pdf"
    upload = UploadFile(filename="native_clean.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await ocr_ingest_service.upload(upload)
    doc_id = result.document_id

    await ocr_ingest_service.ocr_document(doc_id)

    # Run again — should skip
    second = await ocr_ingest_service.ocr_document(doc_id)
    assert second.get("skipped") is True
