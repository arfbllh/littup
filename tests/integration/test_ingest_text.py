"""Integration: .txt / .md uploads bypass OCR and reach chunking_done.

The text-extract path replaces the per-page OCR loop with a direct
read + paragraph split, then writes synthetic page + spans rows and
transitions straight to ocr_done. Layout + chunking then run unchanged.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import UploadFile
from sqlalchemy import text

_SAMPLE_TXT = (
    "Pearson Specter Litt.\n"
    "\n"
    "This is the first paragraph of a plain text upload that should bypass "
    "OCR entirely and still produce searchable chunks downstream.\n"
    "\n"
    "Second paragraph. Mike Ross signs here.\n"
)


_SAMPLE_MD = (
    "# Engagement Letter\n"
    "\n"
    "## Parties\n"
    "\n"
    "Pearson Specter Litt and the client agree as follows:\n"
    "\n"
    "1. The firm will provide legal services.\n"
    "2. The client agrees to pay invoices monthly.\n"
)


# ── .txt ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_txt_upload_bypasses_ocr_and_reaches_ocr_done(
    ocr_ingest_service, db_session, tmp_uploads_dir, cleanup_documents_and_jobs
):
    upload = UploadFile(filename="memo.txt", file=BytesIO(_SAMPLE_TXT.encode("utf-8")))
    result = await ocr_ingest_service.upload(upload)
    assert result.was_new
    doc_id = result.document_id

    ocr_result = await ocr_ingest_service.ocr_document(doc_id)

    row = await db_session.execute(
        text("SELECT status, mime_type, page_count, vlm_pages_used FROM app.documents WHERE id = :id"),
        {"id": doc_id},
    )
    doc = row.fetchone()
    assert doc.status == "ocr_done"
    assert doc.mime_type == "text/plain"
    assert doc.page_count == 1
    # no VLM spend on a text upload
    assert doc.vlm_pages_used == 0

    spans_q = await db_session.execute(
        text(
            """
            SELECT s.text, s.source, s.confidence
              FROM app.spans s
              JOIN app.pages p ON p.id = s.page_id
             WHERE p.document_id = :d
             ORDER BY s.bbox_y0
            """
        ),
        {"d": doc_id},
    )
    spans = spans_q.fetchall()
    # 3 paragraphs in the sample
    assert len(spans) == 3
    assert all(s.source == "text" for s in spans)
    assert all(s.confidence == pytest.approx(1.0) for s in spans)
    assert "Pearson Specter Litt" in spans[0].text
    assert "Mike Ross" in spans[-1].text

    # Job result shape mirrors ocr_document for PDFs
    assert ocr_result["page_count"] == 1
    assert ocr_result["total_spans"] == 3


# ── .md ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_markdown_upload_tags_spans_as_markdown(
    ocr_ingest_service, db_session, tmp_uploads_dir, cleanup_documents_and_jobs
):
    upload = UploadFile(filename="engagement.md", file=BytesIO(_SAMPLE_MD.encode("utf-8")))
    result = await ocr_ingest_service.upload(upload)
    doc_id = result.document_id

    await ocr_ingest_service.ocr_document(doc_id)

    row = await db_session.execute(
        text("SELECT mime_type, status FROM app.documents WHERE id = :id"),
        {"id": doc_id},
    )
    doc = row.fetchone()
    assert doc.mime_type == "text/markdown"
    assert doc.status == "ocr_done"

    src_q = await db_session.execute(
        text(
            """
            SELECT DISTINCT s.source FROM app.spans s
              JOIN app.pages p ON p.id = s.page_id
             WHERE p.document_id = :d
            """
        ),
        {"d": doc_id},
    )
    sources = {r.source for r in src_q.fetchall()}
    assert sources == {"markdown"}


# ── End-to-end through layout and chunking ──────────────────────────────────

@pytest.mark.asyncio
async def test_text_upload_progresses_through_layout_and_chunking(
    ocr_ingest_service, db_session, tmp_uploads_dir, cleanup_documents_and_jobs
):
    upload = UploadFile(filename="memo.txt", file=BytesIO(_SAMPLE_TXT.encode("utf-8")))
    result = await ocr_ingest_service.upload(upload)
    doc_id = result.document_id

    await ocr_ingest_service.ocr_document(doc_id)
    await ocr_ingest_service.parse_layout(doc_id)
    await ocr_ingest_service.chunk_document(doc_id)

    row = await db_session.execute(
        text("SELECT status FROM app.documents WHERE id = :id"), {"id": doc_id}
    )
    assert row.scalar_one() == "chunking_done"

    block_count = await db_session.execute(
        text("SELECT COUNT(*) FROM app.blocks WHERE document_id = :d"),
        {"d": doc_id},
    )
    assert block_count.scalar_one() > 0

    chunk_count = await db_session.execute(
        text("SELECT COUNT(*) FROM app.chunks WHERE document_id = :d"),
        {"d": doc_id},
    )
    assert chunk_count.scalar_one() > 0
