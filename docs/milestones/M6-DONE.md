# M6 — Retrieval Layer — DONE

## What shipped

- **`app/retrieval/bm25.py`** — `BM25Retriever`: `plainto_tsquery('legal_en', ...)` against `text_tsv`, `ts_rank_cd` scoring, `ready`-only filter (NN-1, NN-8).
- **`app/retrieval/dense.py`** — `DenseRetriever`: pgvector `<=>` operator with `SET LOCAL hnsw.ef_search`, `work_mem`, `statement_timeout` inside a single transaction scope (NN-4). Settings validated by regex before f-string interpolation. Uses `begin_nested()` when already in a transaction.
- **`app/retrieval/trigram.py`** — `TrigramRetriever`: `similarity(text, query) > threshold`, GIN-backed trigram index, `ready`-only filter (NN-1, NN-8).
- **`app/retrieval/fusion.py`** — `rrf_fuse` (Reciprocal Rank Fusion, default weights `[1.0, 1.0, 0.5]`) + `entity_overlap_bonus` (case-insensitive entity intersection, additive bonus).
- **`app/retrieval/types.py`** — `RetrievedChunk` dataclass wrapping ORM `Chunk` with `score` and `degraded_mode` fields. Avoids `Chunk.metadata` collision.
- **`app/retrieval/reranker.py`** — `RerankerWrapper`: wraps `BGEReranker`, `asyncio.wait_for(..., timeout=2.0)`, degrades gracefully with `degraded_mode=True` on timeout.
- **`app/retrieval/retriever.py`** — `HybridRetriever`: takes `session_factory` (not pre-built sub-retrievers), creates one session per sub-retriever for safe concurrent `asyncio.gather`, one combined chunk fetch, entity-bonus re-sort, per-doc cap (`MAX_CHUNKS_PER_DOC_PER_QUERY`), rerank. `multi_retrieve` runs all queries via `asyncio.gather`.
- **`app/retrieval/__init__.py`** — exports all public names.
- **`app/api/deps.py`** — `get_retriever()` lazy singleton + `reset_retriever_for_tests()` mirroring the embedder pattern.
- **`app/settings.py`** — 6 new retrieval settings: `HNSW_EF_SEARCH`, `RETRIEVAL_WORK_MEM`, `RETRIEVAL_STATEMENT_TIMEOUT`, `MAX_CHUNKS_PER_DOC_PER_QUERY`, `TRIGRAM_THRESHOLD`, `RETRIEVER_ALWAYS_TRIGRAM`.

## Tests (36 passing)

| File | Count | Notes |
|---|---|---|
| `tests/unit/test_rrf.py` | 6 | |
| `tests/unit/test_entity_bonus.py` | 4 | |
| `tests/unit/test_trigram_activation.py` | 10 | `_should_run_trigram` + `RETRIEVER_ALWAYS_TRIGRAM` flag |
| `tests/unit/test_dense_settings_validation.py` | 4 | ValueError paths for invalid settings strings |
| `tests/integration/test_retrieval_baseline.py` | 7 | 4 DenseRetriever + 3 HybridRetriever end-to-end (incl. per-doc cap) |
| `tests/integration/test_retrieval_legal_terms.py` | 4 | |
| `tests/integration/test_retrieval_partial_doc.py` | 1 | |
| `tests/integration/test_reranker_timeout.py` | 3 | |
| `tests/integration/test_multi_retrieve.py` | 2 | |
| `tests/integration/test_dense_hnsw_index.py` | 1 | |
| `tests/integration/test_entity_bonus_integration.py` | 2 | Entity bonus wiring through HybridRetriever |
| `tests/integration/test_trigram_gin_index.py` | 2 | EXPLAIN ANALYZE confirms `chunks_text_trgm_idx` used |

## Deviations from spec

- **`HybridRetriever` constructor** takes `session_factory` instead of pre-built `bm25`, `dense`, `trigram` instances. Spec sketch showed `BM25Retriever()` with no args but the sub-retrievers require a session. The factory approach is correct: each concurrent search gets its own session (SQLAlchemy `AsyncSession` is not safe for concurrent coroutine use on a single connection).
- **HNSW EXPLAIN test** queries `app.chunks` without the `JOIN app.documents` filter. With the JOIN, the planner uses a nested-loop via `ix_chunks_document_id` even with `enable_seqscan=off` (only 1 fixture document). Removing the JOIN forces the HNSW path.

## Bug fixes (post-audit)

- **`trigram.py` GIN index** — original `WHERE similarity(c.text, :query) > :threshold` bind-param form forced a sequential scan. Fixed to `SET LOCAL pg_trgm.similarity_threshold = {threshold}` + `WHERE c.text % :query`, which engages `chunks_text_trgm_idx`. Wrapped in the same `begin_nested()`/`begin()` pattern as `dense.py`. Verified via `EXPLAIN ANALYZE` in `test_trigram_gin_index.py`.

## Follow-ups for v1.1

- Batch reranking across `multi_retrieve` queries (currently each query has an independent reranker call).
- `HybridRetriever.retrieve` could accept an already-open `AsyncSession` as an override for callers that manage their own transaction lifetime.
- Trigram threshold (`TRIGRAM_THRESHOLD=0.15`) may need tuning against real legal document corpora.
