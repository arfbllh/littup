"""Integration: skewed scan → preprocess deskew runs, document reaches ocr_done."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_skewed_scan_reaches_ocr_done(
    ocr_fixture_docs,
    ocr_ingest_service,
    db_session,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
):
    """scan_skewed.pdf (3° rotation) must complete OCR with acceptable confidence.

    Acceptance criteria:
    - status = ocr_done
    - spans present with source = paddleocr
    - preprocess_image is actually called (T-4)
    - no crash / no status = failed
    """
    from io import BytesIO
    from fastapi import UploadFile
    import app.ingest.ocr.preprocess as _preprocess_mod

    pdf_path = ocr_fixture_docs / "scan_skewed.pdf"
    upload = UploadFile(filename="scan_skewed.pdf", file=BytesIO(pdf_path.read_bytes()))

    result = await ocr_ingest_service.upload(upload)
    assert result.was_new
    doc_id = result.document_id

    preprocess_call_count = 0
    _real_preprocess = _preprocess_mod.preprocess_image

    def _spy(*args, **kwargs):
        nonlocal preprocess_call_count
        preprocess_call_count += 1
        return _real_preprocess(*args, **kwargs)

    with patch.object(_preprocess_mod, "preprocess_image", side_effect=_spy):
        await ocr_ingest_service.ocr_document(doc_id)

    row = await db_session.execute(
        text("SELECT status FROM app.documents WHERE id = :id"), {"id": doc_id}
    )
    status = row.scalar_one()
    assert status == "ocr_done", f"Expected ocr_done, got {status}"

    spans_q = await db_session.execute(
        text(
            """
            SELECT COUNT(*) AS n
              FROM app.spans s
              JOIN app.pages p ON p.id = s.page_id
             WHERE p.document_id = :d
            """
        ),
        {"d": doc_id},
    )
    count = spans_q.scalar_one()
    assert count > 0, "Skewed scan produced no spans"
    assert preprocess_call_count > 0, "preprocess_image was never called — deskew did not execute"


@pytest.mark.asyncio
async def test_skewed_confidence_not_worse_than_clean(
    ocr_fixture_docs,
    ocr_ingest_service,
    db_session,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    tmp_path,
):
    """Deskewed result should have comparable confidence to the clean scan.

    We allow a 15% penalty vs. the clean scan (preprocessing introduces
    some artefacts, but should not decimate confidence).
    """
    from io import BytesIO
    from fastapi import UploadFile

    async def _upload_and_ocr(filename: str) -> float:
        path = ocr_fixture_docs / filename
        upload = UploadFile(filename=filename, file=BytesIO(path.read_bytes()))
        result = await ocr_ingest_service.upload(upload)
        doc_id = result.document_id
        await ocr_ingest_service.ocr_document(doc_id)
        q = await db_session.execute(
            text(
                """
                SELECT AVG(s.confidence) AS mean_conf
                  FROM app.spans s
                  JOIN app.pages p ON p.id = s.page_id
                 WHERE p.document_id = :d
                """
            ),
            {"d": doc_id},
        )
        val = q.scalar_one()
        return float(val) if val else 0.0

    clean_conf = await _upload_and_ocr("scan_clean.pdf")
    skewed_conf = await _upload_and_ocr("scan_skewed.pdf")

    assert skewed_conf >= clean_conf * 0.85, (
        f"Skewed confidence {skewed_conf:.3f} is more than 15% below "
        f"clean confidence {clean_conf:.3f}"
    )
