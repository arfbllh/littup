# M6 — Retrieval Layer

**Estimated time:** 2 hours
**Dependencies:** M2, M5
**Rubric impact:** Retrieval and Grounding — front half of 25 pts

## Goal

A working hybrid retriever: BM25 (over `legal_en` config) + dense (pgvector HNSW) + tri-gram fallback for proper nouns, fused via Reciprocal Rank Fusion with an entity-overlap bonus, reranked by a bge-reranker-base cross-encoder. Multi-query retrieval supports the template's `retrieval_queries` dict. After this milestone, `Retriever.multi_retrieve(...)` returns sane chunks for every field of every template.

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-4, NN-8**
2. `docs/architecture/03-components/retrieval.md` — full spec
3. `docs/architecture/03-components/draft-engine.md` (multi-query interface)

## Non-Negotiables that apply

- **NN-4** — vector queries set `SET LOCAL work_mem`, `statement_timeout`, `hnsw.ef_search`
- **NN-8** — `legal_en` text search config; entity overlap folded into RRF; tri-gram fallback present

## Files to create

### Retrievers

- `app/retrieval/__init__.py`
- `app/retrieval/bm25.py` — `BM25Retriever`:
  - `async search(query, document_ids, k) -> list[(chunk_id, score)]`
  - Uses `to_tsvector('legal_en', text)` (already stored as `text_tsv`) and `plainto_tsquery('legal_en', query)`
  - Scores via `ts_rank_cd`
  - Filters by `document.status = 'ready'` (NN-1 — retrieval refuses partial docs)
- `app/retrieval/dense.py` — `DenseRetriever`:
  - `async search(query_embedding, document_ids, k) -> list[(chunk_id, score)]`
  - `SET LOCAL hnsw.ef_search = 100`, `work_mem = '64MB'`, `statement_timeout = '5s'`
  - `ORDER BY embedding <=> :query_embedding LIMIT :k`
  - Returns cosine distance; convert to similarity = `1 - distance`
- `app/retrieval/trigram.py` — `TrigramRetriever`:
  - `async search(query, document_ids, k) -> list[(chunk_id, score)]`
  - Uses `similarity(chunks.text, query) > threshold` with GIN-pg_trgm
  - Used specifically when the query has unusual proper nouns (detected by a regex pre-pass) — but always runnable
- `app/retrieval/fusion.py` — `rrf_fuse(result_lists, k=60, weights=None) -> list[(chunk_id, score)]`:
  - Standard RRF: `score = sum(weight_i / (k + rank_i))` for each chunk across lists
  - Default weights: BM25=1.0, dense=1.0, trigram=0.5
  - Plus `entity_overlap_bonus(query_entities, chunk_entities) -> float`:
    - For each chunk, count entities in `chunk.entities` that appear in the query (extracted with same regex from chunker)
    - Bonus: `0.1 * matched_count`, added to the fused score
- `app/retrieval/reranker.py` — `RerankerWrapper`:
  - `async rerank(query, chunks, top_k) -> list[Chunk]`
  - Uses `BGEReranker` from `app/llm/reranker_model.py`
  - Times out at 2s — on timeout, returns the input order unchanged + logs a degraded-mode marker
- `app/retrieval/retriever.py` — `HybridRetriever`:
  - `__init__(bm25, dense, trigram, reranker, embedder)`
  - `async retrieve(query, document_ids, top_k=8, fetch_k=50)`:
    1. Parallel-run BM25, dense (after embedding), trigram (only if query has proper nouns or always — config flag)
    2. RRF fuse with entity overlap bonus
    3. Apply per-document cap (`MAX_CHUNKS_PER_DOC_PER_QUERY=3`)
    4. Take top fetch_k → rerank → top top_k
  - `async multi_retrieve(queries: dict[str, str], document_ids, top_k_per_query=5) -> dict[str, list[Chunk]]`:
    - Runs queries concurrently; shares a single reranker batch where possible

### Wiring

- `app/api/deps.py` — `get_retriever()` lazy singleton

### Tests

- `tests/unit/test_rrf.py` — known input lists; verify scores match expected RRF math
- `tests/unit/test_entity_bonus.py` — a chunk with "Pearson Specter Litt" in `entities` gets a higher fused score for a query containing it
- `tests/integration/test_retrieval_baseline.py` — index 3 fixture documents; query "parties to the case"; assert top result contains "plaintiff" or "defendant"
- `tests/integration/test_retrieval_legal_terms.py` (NN-8):
  - Build a chunk containing "42 U.S.C. § 1983" and one containing "civil rights"
  - Query: "§ 1983" — assert the citation chunk is ranked first (BM25 + tri-gram beat the noise)
  - Query: "habeas corpus" — assert chunks containing the Latin phrase win over chunks containing "release from custody" (no over-stemming)
- `tests/integration/test_retrieval_partial_doc.py` (NN-1) — index one document; insert a second doc with `status='ocr_running'`; assert retrieval never returns chunks from the in-progress doc
- `tests/integration/test_reranker_timeout.py` — mock reranker to sleep 5s; assert retrieval returns un-reranked results with `degraded_mode=true` in metadata
- `tests/integration/test_multi_retrieve.py` — pass a 5-query template; assert all 5 queries return results concurrently in < 1s p95

## Acceptance criteria

- [ ] BM25, dense, trigram, RRF, reranker all wired
- [ ] Legal-terms test passes (NN-8 working in practice, not just in DDL)
- [ ] Retrieval ignores partial documents
- [ ] Multi-query retrieval is concurrent (verify via timing)
- [ ] HNSW query plan uses the index (`EXPLAIN ANALYZE` in a test)
- [ ] Per-doc cap of 3 chunks observable in test output

## Out of scope

- Cross-encoder fine-tuning — v1.1
- Query rewriting / decomposition — v1.1 (single-shot is fine for the templates we ship)
- Embedding model swap path — already pluggable via `Embedder` Protocol; just don't switch in this milestone

## Definition of done

Eval harness in M12 will be able to plug into this. For now, `make retrieval-bench` runs 10 hand-built queries against the fixture corpus and prints rank-of-expected-chunk. `M6-DONE.md` written.

## Sub-agent delegation

After the protocols are merged:

- Sub-agent A: BM25 + tests
- Sub-agent B: dense + HNSW tuning + tests
- Sub-agent C: trigram + tests
- Sub-agent D: fusion + entity bonus + rrf math tests
- Sub-agent E: reranker wrapper + timeout test

`HybridRetriever` integration sequential after the five are merged.
