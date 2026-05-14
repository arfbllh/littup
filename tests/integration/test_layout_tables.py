"""Integration: table-heavy PDF → layout → table block detection.

When docling or unstructured is installed: asserts block_type='table' with cells.
When only span-grouper fallback is available: asserts blocks exist with table text.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


def _has_table_parser() -> bool:
    for lib in ("docling", "unstructured"):
        try:
            __import__(lib)
            return True
        except ImportError:
            pass
    return False


@pytest.mark.asyncio
async def test_table_heavy_layout_produces_blocks(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """table_heavy.pdf must produce blocks and reach layout_done."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    pdf_path = m5_fixture_docs / "table_heavy.pdf"
    upload = UploadFile(filename="table_heavy.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await service.upload(upload)
    doc_id = result.document_id

    await service.ocr_document(doc_id)
    await service.parse_layout(doc_id)

    row = await db_session.execute(
        text("SELECT status FROM app.documents WHERE id = :id"), {"id": doc_id}
    )
    assert row.scalar_one() == "layout_done"

    count_q = await db_session.execute(
        text("SELECT COUNT(*) FROM app.blocks WHERE document_id = :id"), {"id": doc_id}
    )
    assert count_q.scalar_one() > 0


@pytest.mark.asyncio
async def test_table_block_type_and_cells(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """With a table parser: block_type='table' with cells metadata.
    Without one: blocks contain recognizable table text content.
    """
    from io import BytesIO
    from fastapi import UploadFile
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    pdf_path = m5_fixture_docs / "table_heavy.pdf"
    upload = UploadFile(filename="table_heavy.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await service.upload(upload)
    doc_id = result.document_id

    await service.ocr_document(doc_id)
    await service.parse_layout(doc_id)

    blocks_q = await db_session.execute(
        text(
            "SELECT block_type, text, metadata FROM app.blocks "
            "WHERE document_id = :id ORDER BY reading_order"
        ),
        {"id": doc_id},
    )
    blocks = blocks_q.fetchall()

    if _has_table_parser():
        table_blocks = [b for b in blocks if b.block_type == "table"]
        assert len(table_blocks) >= 1, (
            f"Expected at least one table block; types found: {[b.block_type for b in blocks]}"
        )
        meta = table_blocks[0].metadata or {}
        assert "cells" in meta, f"Table block must have metadata['cells'], got: {meta}"
        cells = meta["cells"]
        assert isinstance(cells, list) and len(cells) > 0
        assert isinstance(cells[0], list), "cells must be 2D (list of lists)"
    else:
        # Span-grouper: no table type, but content must be present
        all_text = " ".join(b.text for b in blocks).lower()
        assert any(kw in all_text for kw in ("item", "description", "damages", "schedule")), (
            f"Expected table keywords in block text. Got: {all_text[:300]}"
        )


@pytest.mark.asyncio
async def test_table_text_contains_known_values(
    m5_fixture_docs,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    db_session,
):
    """Block text from table_heavy.pdf must contain fixture-known content."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.ingest.service import IngestService

    service = IngestService(db_session)
    pdf_path = m5_fixture_docs / "table_heavy.pdf"
    upload = UploadFile(filename="table_heavy.pdf", file=BytesIO(pdf_path.read_bytes()))
    result = await service.upload(upload)
    doc_id = result.document_id

    await service.ocr_document(doc_id)
    await service.parse_layout(doc_id)

    blocks_q = await db_session.execute(
        text("SELECT text FROM app.blocks WHERE document_id = :id"),
        {"id": doc_id},
    )
    all_text = " ".join(r.text for r in blocks_q.fetchall()).lower()
    assert "damages" in all_text or "schedule" in all_text, (
        f"Expected 'damages' or 'schedule'. Got: {all_text[:200]}"
    )
