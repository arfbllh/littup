"""Integration: clean scan PDF → PaddleOCR path, confidence > 0.5."""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_scan_uses_paddleocr(
    ocr_fixture_docs,
    ocr_ingest_service,
    db_session,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    monkeypatch,
):
    """scan_clean.pdf must go through PaddleOCR.

    Acceptance criteria:
    - status = ocr_done
    - all spans have source = paddleocr
    - no VLM calls (clean scan → PaddleOCR confidence should exceed threshold)
    """
    import app.ingest.ocr.paddle_ocr as paddle_module
    from io import BytesIO
    from fastapi import UploadFile

    class _MockPaddle:
        def ocr(self, image, cls=True):
            h, w = image.shape[:2]
            return [
                [
                    [[[10, 10], [w-10, 10], [w-10, 30], [10, 30]], ("sample text", 0.92)],
                    [[[10, 40], [w-10, 40], [w-10, 60], [10, 60]], ("another line", 0.88)],
                ]
            ]

    paddle_module.release_paddle()
    monkeypatch.setattr(paddle_module, "_paddle_instance", _MockPaddle())

    pdf_path = ocr_fixture_docs / "scan_clean.pdf"
    upload = UploadFile(filename="scan_clean.pdf", file=BytesIO(pdf_path.read_bytes()))

    result = await ocr_ingest_service.upload(upload)
    assert result.was_new
    doc_id = result.document_id

    ocr_result = await ocr_ingest_service.ocr_document(doc_id)

    # Document status
    row = await db_session.execute(
        text("SELECT status, vlm_pages_used FROM app.documents WHERE id = :id"),
        {"id": doc_id},
    )
    doc = row.fetchone()
    assert doc.status == "ocr_done", f"Expected ocr_done, got {doc.status}"
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
    assert len(spans) > 0, "Expected at least some PaddleOCR spans"

    sources = {s.source for s in spans}
    assert sources == {"paddleocr"}, f"Unexpected sources: {sources}"


@pytest.mark.asyncio
async def test_multi_column_scan(
    ocr_fixture_docs,
    ocr_ingest_service,
    db_session,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    monkeypatch,
):
    """multi_column.pdf should complete OCR without error."""
    import app.ingest.ocr.paddle_ocr as paddle_module
    from io import BytesIO
    from fastapi import UploadFile

    class _MockPaddle:
        def ocr(self, image, cls=True):
            h, w = image.shape[:2]
            return [
                [
                    [[[10, 10], [w-10, 10], [w-10, 30], [10, 30]], ("sample text", 0.92)],
                    [[[10, 40], [w-10, 40], [w-10, 60], [10, 60]], ("another line", 0.88)],
                ]
            ]

    paddle_module.release_paddle()
    monkeypatch.setattr(paddle_module, "_paddle_instance", _MockPaddle())

    pdf_path = ocr_fixture_docs / "multi_column.pdf"
    upload = UploadFile(filename="multi_column.pdf", file=BytesIO(pdf_path.read_bytes()))

    result = await ocr_ingest_service.upload(upload)
    doc_id = result.document_id

    ocr_result = await ocr_ingest_service.ocr_document(doc_id)

    row = await db_session.execute(
        text("SELECT status FROM app.documents WHERE id = :id"), {"id": doc_id}
    )
    assert row.scalar_one() == "ocr_done"

    spans_q = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM app.spans s JOIN app.pages p ON p.id = s.page_id WHERE p.document_id = :d"
        ),
        {"d": doc_id},
    )
    assert spans_q.scalar_one() > 0, "Expected spans to be persisted for multi-column scan"
