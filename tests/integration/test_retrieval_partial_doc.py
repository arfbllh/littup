"""Trigram retrieval integration tests — status filter."""

import uuid

import pytest
from sqlalchemy import text

from app.retrieval.trigram import TrigramRetriever


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_trigram_excludes_non_ready_document_chunks(db_session):
    ready_doc_id = _uid()
    ocr_doc_id = _uid()
    ready_chunk_id = _uid()
    ocr_chunk_id = _uid()

    await db_session.execute(
        text(
            """
            INSERT INTO app.documents (id, status, sha256, filename, created_at, updated_at)
            VALUES (:id, 'ready', :sha, 'trigram_ready.pdf', now(), now())
            """
        ),
        {"id": ready_doc_id, "sha": _uid()},
    )

    await db_session.execute(
        text(
            """
            INSERT INTO app.documents (id, status, sha256, filename, created_at, updated_at)
            VALUES (:id, 'ocr_running', :sha, 'trigram_ocr.pdf', now(), now())
            """
        ),
        {"id": ocr_doc_id, "sha": _uid()},
    )

    await db_session.execute(
        text(
            """
            INSERT INTO app.chunks (id, document_id, text, text_tsv, page_start, page_end, created_at)
            VALUES (
                :id, :doc_id,
                'the plaintiff filed a motion',
                to_tsvector('legal_en', 'the plaintiff filed a motion'),
                1, 1, now()
            )
            """
        ),
        {"id": ready_chunk_id, "doc_id": ready_doc_id},
    )

    await db_session.execute(
        text(
            """
            INSERT INTO app.chunks (id, document_id, text, text_tsv, page_start, page_end, created_at)
            VALUES (
                :id, :doc_id,
                'the plaintiff filed a motion',
                to_tsvector('legal_en', 'the plaintiff filed a motion'),
                1, 1, now()
            )
            """
        ),
        {"id": ocr_chunk_id, "doc_id": ocr_doc_id},
    )

    await db_session.flush()

    retriever = TrigramRetriever(db_session)
    results = await retriever.search("plaintiff motion", None, 10)

    result_ids = [r[0] for r in results]
    assert ready_chunk_id in result_ids, "ready doc chunk must appear in results"
    assert ocr_chunk_id not in result_ids, "ocr_running doc chunk must NOT appear in results"
