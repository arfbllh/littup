"""Regression: layout must fail loudly when OCR produced no spans.

WS-A.3: a doc with `page_count > 0` and zero spans used to silently transition
to `ready` with an empty block tree. We now mark it `failed` with
`error_code='EMPTY_OCR_OUTPUT'` so the retry path is reachable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.ids import new_uuid7
from app.ingest.layout import parse_layout


@pytest.mark.asyncio
async def test_layout_fails_on_empty_spans_with_pages(
    cleanup_documents_and_jobs,
    db_session,
):
    """parse_layout marks a `ocr_done` doc with 0 spans but >0 pages as failed."""
    doc_id = await db_session.execute(
        text(
            """
            INSERT INTO app.documents (sha256, filename, mime_type, size_bytes, status, page_count)
            VALUES (:sha, 'empty.pdf', 'application/pdf', 1, 'ocr_done', 3)
            RETURNING id
            """
        ),
        {"sha": new_uuid7()},
    )
    document_id = str(doc_id.scalar_one())

    # Insert 3 page rows, no spans
    for n in range(1, 4):
        await db_session.execute(
            text(
                """
                INSERT INTO app.pages (document_id, page_number, width, height, status)
                VALUES (:d, :n, 612, 792, 'ocr_done')
                """
            ),
            {"d": document_id, "n": n},
        )
    await db_session.commit()

    result = await parse_layout(document_id, db_session)
    assert result.get("failed") is True
    assert result.get("error_code") == "EMPTY_OCR_OUTPUT"
    assert result.get("block_count") == 0

    row = await db_session.execute(
        text(
            "SELECT status, error_code FROM app.documents WHERE id = :id"
        ),
        {"id": document_id},
    )
    status, error_code = row.fetchone()
    assert status == "failed"
    assert error_code == "EMPTY_OCR_OUTPUT"

    # No blocks should have been inserted.
    blocks_row = await db_session.execute(
        text("SELECT COUNT(*) FROM app.blocks WHERE document_id = :id"),
        {"id": document_id},
    )
    assert blocks_row.scalar_one() == 0
