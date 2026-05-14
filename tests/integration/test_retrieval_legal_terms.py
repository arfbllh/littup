"""BM25 retrieval integration tests — legal term ranking and NN-1 status filter."""

import uuid

import pytest
from sqlalchemy import text

from app.retrieval.bm25 import BM25Retriever


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_bm25_ranks_habeas_corpus_chunk(db_session):
    ready_doc_id = _uid()
    citation_chunk_id = _uid()
    habeas_chunk_id = _uid()

    await db_session.execute(
        text(
            """
            INSERT INTO app.documents (id, status, sha256, filename, created_at, updated_at)
            VALUES (:id, 'ready', :sha, 'test_bm25.pdf', now(), now())
            """
        ),
        {"id": ready_doc_id, "sha": _uid()},
    )

    await db_session.execute(
        text(
            """
            INSERT INTO app.chunks (id, document_id, text, text_tsv, page_start, page_end, created_at)
            VALUES (
                :id, :doc_id,
                '42 U.S.C. § 1983 provides a cause of action for civil rights violations',
                to_tsvector('legal_en', '42 U.S.C. § 1983 provides a cause of action for civil rights violations'),
                1, 1, now()
            )
            """
        ),
        {"id": citation_chunk_id, "doc_id": ready_doc_id},
    )

    await db_session.execute(
        text(
            """
            INSERT INTO app.chunks (id, document_id, text, text_tsv, page_start, page_end, created_at)
            VALUES (
                :id, :doc_id,
                'The petitioner filed a writ of habeas corpus seeking immediate release from unlawful detention',
                to_tsvector('legal_en', 'The petitioner filed a writ of habeas corpus seeking immediate release from unlawful detention'),
                2, 2, now()
            )
            """
        ),
        {"id": habeas_chunk_id, "doc_id": ready_doc_id},
    )

    await db_session.flush()

    retriever = BM25Retriever(db_session)
    results = await retriever.search("habeas corpus", None, 5)

    assert len(results) >= 1
    result_ids = [r[0] for r in results]
    assert habeas_chunk_id in result_ids

    top_id = result_ids[0]
    assert top_id == habeas_chunk_id or (
        len(result_ids) >= 2 and habeas_chunk_id in result_ids[:2]
    ), f"habeas_chunk_id not in top 2; got {result_ids}"


@pytest.mark.asyncio
async def test_bm25_excludes_non_ready_document_chunks(db_session):
    ready_doc_id = _uid()
    ocr_doc_id = _uid()
    ready_chunk_id = _uid()
    ocr_chunk_id = _uid()

    await db_session.execute(
        text(
            """
            INSERT INTO app.documents (id, status, sha256, filename, created_at, updated_at)
            VALUES (:id, 'ready', :sha, 'ready_doc.pdf', now(), now())
            """
        ),
        {"id": ready_doc_id, "sha": _uid()},
    )

    await db_session.execute(
        text(
            """
            INSERT INTO app.documents (id, status, sha256, filename, created_at, updated_at)
            VALUES (:id, 'ocr_running', :sha, 'ocr_doc.pdf', now(), now())
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
                'The doctrine of habeas corpus protects against unlawful imprisonment',
                to_tsvector('legal_en', 'The doctrine of habeas corpus protects against unlawful imprisonment'),
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
                'habeas corpus motion filed in federal court seeking relief',
                to_tsvector('legal_en', 'habeas corpus motion filed in federal court seeking relief'),
                1, 1, now()
            )
            """
        ),
        {"id": ocr_chunk_id, "doc_id": ocr_doc_id},
    )

    await db_session.flush()

    retriever = BM25Retriever(db_session)
    results = await retriever.search("habeas corpus", None, 10)

    result_ids = [r[0] for r in results]
    assert ready_chunk_id in result_ids, "ready doc chunk must appear in results"
    assert ocr_chunk_id not in result_ids, "ocr_running doc chunk must NOT appear in results (NN-1)"


@pytest.mark.asyncio
async def test_bm25_document_id_filter(db_session):
    doc_a_id = _uid()
    doc_b_id = _uid()
    chunk_a_id = _uid()
    chunk_b_id = _uid()

    for doc_id, sha in [(doc_a_id, _uid()), (doc_b_id, _uid())]:
        await db_session.execute(
            text(
                """
                INSERT INTO app.documents (id, status, sha256, filename, created_at, updated_at)
                VALUES (:id, 'ready', :sha, 'filter_test.pdf', now(), now())
                """
            ),
            {"id": doc_id, "sha": sha},
        )

    for chunk_id, doc_id, pg in [(chunk_a_id, doc_a_id, 1), (chunk_b_id, doc_b_id, 2)]:
        await db_session.execute(
            text(
                """
                INSERT INTO app.chunks (id, document_id, text, text_tsv, page_start, page_end, created_at)
                VALUES (
                    :id, :doc_id,
                    'habeas corpus is a fundamental legal remedy',
                    to_tsvector('legal_en', 'habeas corpus is a fundamental legal remedy'),
                    :pg, :pg, now()
                )
                """
            ),
            {"id": chunk_id, "doc_id": doc_id, "pg": pg},
        )

    await db_session.flush()

    retriever = BM25Retriever(db_session)
    results = await retriever.search("habeas corpus", [doc_a_id], 10)

    result_ids = [r[0] for r in results]
    assert chunk_a_id in result_ids
    assert chunk_b_id not in result_ids


@pytest.mark.asyncio
async def test_bm25_empty_query_returns_empty(db_session):
    retriever = BM25Retriever(db_session)
    assert await retriever.search("", None, 5) == []
    assert await retriever.search("   ", None, 5) == []
