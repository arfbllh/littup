"""
Verifies that 0001_initial.py applied correctly:
- pgvector, pg_trgm, pgcrypto extensions installed
- legal_en text search config exists and legal_en_test() returns non-empty tsvector
- HNSW index on app.chunks.embedding exists with correct params (m=16, ef_construction=64)
- llm_log.llm_requests partitioned root + first partition exists
"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_extensions_installed(db_session):
    result = await db_session.execute(
        text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm', 'pgcrypto')")
    )
    installed = {row.extname for row in result}
    assert "vector" in installed, "pgvector extension missing"
    assert "pg_trgm" in installed, "pg_trgm extension missing"
    assert "pgcrypto" in installed, "pgcrypto extension missing"


@pytest.mark.asyncio
async def test_schemas_exist(db_session):
    result = await db_session.execute(
        text("SELECT schema_name FROM information_schema.schemata WHERE schema_name IN ('app', 'jobs', 'llm_log')")
    )
    schemas = {row.schema_name for row in result}
    assert schemas == {"app", "jobs", "llm_log"}


@pytest.mark.asyncio
async def test_legal_en_config_exists(db_session):
    result = await db_session.execute(
        text("SELECT cfgname FROM pg_ts_config WHERE cfgname = 'legal_en'")
    )
    assert result.fetchone() is not None, "legal_en text search config missing"


@pytest.mark.asyncio
async def test_legal_en_test_function_returns_tsvector(db_session):
    result = await db_session.execute(text("SELECT legal_en_test()"))
    row = result.fetchone()
    assert row is not None
    assert row[0] is not None
    # tsvector should be non-empty
    assert str(row[0]).strip() != ""


@pytest.mark.asyncio
async def test_legal_en_tsvector_works(db_session):
    result = await db_session.execute(
        text("SELECT to_tsvector('legal_en', 'plaintiff Pearson Specter Litt filed') AS tsv")
    )
    row = result.fetchone()
    assert row is not None
    assert row.tsv is not None
    tsv_str = str(row.tsv)
    assert len(tsv_str) > 0


@pytest.mark.asyncio
async def test_hnsw_index_exists_with_params(db_session):
    result = await db_session.execute(
        text("""
            SELECT
                i.relname AS index_name,
                ix.reloptions
            FROM pg_index pi
            JOIN pg_class i  ON i.oid = pi.indexrelid
            JOIN pg_class t  ON t.oid = pi.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_class ix ON ix.oid = pi.indexrelid
            WHERE n.nspname = 'app'
              AND t.relname = 'chunks'
              AND i.relname = 'chunks_embedding_hnsw_idx'
        """)
    )
    row = result.fetchone()
    assert row is not None, "HNSW index chunks_embedding_hnsw_idx not found"
    # reloptions contains m=16 and ef_construction=64
    options = row.reloptions or []
    options_str = str(options)
    assert "m=16" in options_str, f"Expected m=16 in index options, got: {options_str}"
    assert "ef_construction=64" in options_str, f"Expected ef_construction=64, got: {options_str}"


@pytest.mark.asyncio
async def test_llm_requests_partition_exists(db_session):
    result = await db_session.execute(
        text("""
            SELECT relname FROM pg_class
            WHERE relname IN ('llm_requests', 'llm_requests_2026_05')
              AND relkind IN ('p', 'r')
        """)
    )
    names = {row.relname for row in result}
    assert "llm_requests" in names, "llm_requests root table missing"
    assert "llm_requests_2026_05" in names, "llm_requests_2026_05 partition missing"


@pytest.mark.asyncio
async def test_chunks_tsvector_trigger(db_session):
    """Inserting a chunk should auto-populate text_tsv via the trigger."""
    import uuid

    doc_result = await db_session.execute(
        text("""
            INSERT INTO app.documents (sha256, filename, status)
            VALUES (:sha, :fn, 'ready')
            RETURNING id
        """),
        {"sha": f"sha_{uuid.uuid4().hex}", "fn": "test.pdf"},
    )
    doc_id = doc_result.fetchone().id

    chunk_id = str(uuid.uuid4())
    await db_session.execute(
        text("""
            INSERT INTO app.chunks (id, document_id, text)
            VALUES (:id, :doc_id, 'plaintiff Pearson Specter Litt filed a complaint')
        """),
        {"id": chunk_id, "doc_id": doc_id},
    )

    result = await db_session.execute(
        text("SELECT text_tsv FROM app.chunks WHERE id = :id"),
        {"id": chunk_id},
    )
    row = result.fetchone()
    assert row is not None
    assert row.text_tsv is not None
    assert "pearson" in str(row.text_tsv).lower() or "plaintiff" in str(row.text_tsv).lower()
