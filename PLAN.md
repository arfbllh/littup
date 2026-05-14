# M6 — Retrieval Layer — PLAN

## Context

M5 left the DB fully indexed and all chunks embedded. `app/retrieval/__init__.py` is empty. M6 wires five retrieval components (BM25, dense, trigram, RRF fusion, reranker) into a `HybridRetriever` that `DraftEngine` (M7) depends on. NN-4 (SET LOCAL work_mem/ef_search) and NN-8 (legal_en, trigram, entity overlap) are hard acceptance criteria.

---

## What already exists (do not re-implement)

| Artifact | Location |
|---|---|
| HNSW index (m=16, ef_construction=64) | `app/db/migrations/versions/0001_initial.py` |
| `legal_en` TSV trigger on `chunks.text_tsv` | `app/db/migrations/versions/0001_initial.py` |
| GIN trigram index `chunks_text_trgm_idx` | `app/db/migrations/versions/0001_initial.py` |
| GIN entities index `chunks_entities_gin_idx` | `app/db/migrations/versions/0001_initial.py` |
| `legal_en` dict overrides (simple, NN-8) | `app/db/migrations/versions/0003_legal_en_overrides.py` |
| `BGEEmbedder` + `OpenAIEmbedder` (Protocol) | `app/llm/embedder.py` |
| `BGEReranker` (lazy-load CrossEncoder) | `app/llm/reranker_model.py` |
| `get_embedder()` lazy singleton | `app/api/deps.py` |
| `extract_entities()` (regex, NN-8) | `app/ingest/chunker.py` |

No new migrations required — all indices and the `legal_en` config are already present.

---

## Settings additions (`app/settings.py`)

Six new fields (defaults work; no env file or migration needed):

```python
HNSW_EF_SEARCH: int = 100
RETRIEVAL_WORK_MEM: str = "64MB"
RETRIEVAL_STATEMENT_TIMEOUT: str = "5s"
MAX_CHUNKS_PER_DOC_PER_QUERY: int = 3
TRIGRAM_THRESHOLD: float = 0.15
RETRIEVER_ALWAYS_TRIGRAM: bool = False   # if False, only when proper nouns/§ detected
```

---

## Files to create

### `app/retrieval/bm25.py` — `BM25Retriever`

```python
async def search(self, query: str, document_ids: list[str] | None, k: int) -> list[tuple[str, float]]
```
- `plainto_tsquery('legal_en', :query)` against `text_tsv`; score via `ts_rank_cd`
- JOIN `app.documents` WHERE `status = 'ready'` (NN-1)
- Optional `document_id = ANY(:doc_ids)` filter

### `app/retrieval/dense.py` — `DenseRetriever`

```python
async def search(self, query_embedding: list[float], document_ids: list[str] | None, k: int) -> list[tuple[str, float]]
```
- Raw SQL: `SET LOCAL hnsw.ef_search`, `SET LOCAL work_mem`, `SET LOCAL statement_timeout` (NN-4)
- **`SET LOCAL` is transaction-scoped, not connection-scoped.** Wrap both SETs and the ORDER BY in a single `async with session.begin():` (or accept that pgbouncer in transaction-pool mode requires this). Since `SET LOCAL` cannot use bind params, use `text(f"SET LOCAL work_mem = '{val}'")` after validating settings strings match `^\d+[KMG]?B$` / `^\d+m?s$`.
- Cast the qvec parameter as `CAST(:qvec AS vector)` with the value formatted as `"[" + ",".join(str(f) for f in vec) + "]"` (same pattern as `service.py:743`).
- `ORDER BY embedding <=> :qvec LIMIT :k`; return score = `1 - distance`
- JOIN `app.documents` WHERE `status = 'ready'`

### `app/retrieval/trigram.py` — `TrigramRetriever`

```python
async def search(self, query: str, document_ids: list[str] | None, k: int) -> list[tuple[str, float]]
```
- `similarity(text, :query) > :threshold`; order by similarity desc; limit k
- JOIN `app.documents` WHERE `status = 'ready'`

### `app/retrieval/fusion.py` — pure functions, no DB

```python
def rrf_fuse(
    result_lists: list[list[tuple[str, float]]],
    k: int = 60,
    weights: list[float] | None = None,   # default [1.0, 1.0, 0.5]
) -> list[tuple[str, float]]

def entity_overlap_bonus(
    query_entities: list[str],
    chunk_entities_map: dict[str, list[str]],  # chunk_id -> entities
    bonus_per_match: float = 0.1,
) -> dict[str, float]   # chunk_id -> additive bonus
```
- RRF: `score = Σ weight_i / (k + rank_i)` across all lists
- Entity bonus added to fused score post-RRF

### `app/retrieval/types.py` — shared `RetrievedChunk` dataclass

```python
@dataclass
class RetrievedChunk:
    chunk: Chunk                 # ORM row from app.db.models.chunk
    score: float                 # fused/reranked score
    degraded_mode: bool = False  # set True when reranker times out
```

**Rationale:** the ORM `Chunk` cannot carry per-query metadata. Its JSONB column is named `metadata_` in Python (mapped to SQL `"metadata"`) because `metadata` is reserved on SQLAlchemy `DeclarativeBase` — writing to `.metadata` collides with the Base attribute. A dataclass wrapper also gives M7 a clean place to read scores.

### `app/retrieval/reranker.py` — `RerankerWrapper`

```python
async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]
```
- Wraps `BGEReranker` from `app/llm/reranker_model.py` (`async def rerank(query, docs: list[str]) -> list[int]` — returns sorted indices)
- **Interface translation required**: extract `c.chunk.text` from each `RetrievedChunk`, call BGEReranker, then reorder the wrappers by the returned index list.
- `asyncio.wait_for(..., timeout=2.0)`
- On timeout: log `degraded_mode=True`, return input order (sliced to `top_k`) with `degraded_mode = True` on each `RetrievedChunk` (NOT `chunk.metadata` — that's a Base attribute collision).

### `app/retrieval/retriever.py` — `HybridRetriever`

```python
class HybridRetriever:
    def __init__(self, bm25, dense, trigram, reranker, embedder): ...

    async def retrieve(
        self, query: str, document_ids, top_k=8, fetch_k=50
    ) -> list[Chunk]: ...

    async def multi_retrieve(
        self, queries: dict[str, str], document_ids, top_k_per_query=5
    ) -> dict[str, list[Chunk]]: ...
```

`retrieve` steps:
1. Detect proper nouns (`\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b` or `§`) → `run_trigram` flag (or always if `RETRIEVER_ALWAYS_TRIGRAM`)
2. **Embed query (single-vector unwrap):** `Embedder.embed` (per `app/llm/embedder.py:20`) takes `list[str]` and returns `list[list[float]]`. Call once as `qvec = (await embedder.embed([query]))[0]`, then pass `qvec` to `dense.search`. Do NOT pass the nested list — it will break the `:qvec` bind.
3. `asyncio.gather(bm25.search, dense.search(qvec, ...), trigram.search if run_trigram)`
4. `rrf_fuse([bm25, dense, trigram or []], weights=[1.0, 1.0, 0.5])` — note the default weight vector is sized for exactly 3 lists; if fewer lists are passed, caller must supply weights explicitly.
5. **Combined fetch (one DB roundtrip):** SELECT all needed columns (`id`, `text`, `document_id`, `entities`, `section_path`, `page_start`, `page_end`, `metadata`, …) for the candidate IDs in a single query. Build `chunk_entities_map` from the same result set rather than issuing a separate `entities`-only query in step 4 + a full-row fetch in step 7.
6. Apply `entity_overlap_bonus(extract_entities(query), chunk_entities_map)` → add to fused scores, re-sort. **Caveat:** `extract_entities` (`app/ingest/chunker.py:51`) is regex over capitalized runs / statute citations / dollar amounts / case captions — lowercase queries like "who are the parties" will return `[]` and the bonus is a no-op. This is acceptable; the bonus mainly fires when users paste statute citations or case captions into the query.
7. Per-doc cap: keep at most `MAX_CHUNKS_PER_DOC_PER_QUERY` per `document_id` from the fused+bonus-adjusted list; take top `fetch_k`.
8. Wrap each row as `RetrievedChunk(chunk=..., score=..., degraded_mode=False)`.
9. `reranker.rerank(query, retrieved_chunks, top_k)` → return top `top_k`.

`multi_retrieve`: `asyncio.gather(*[retrieve(q, ...) for q in queries.values()])` → zip back to field keys. Each query runs its own reranker call independently (batching across queries is v1.1).

---

## Files to modify

### `app/retrieval/__init__.py`
Export all public names:
```python
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.dense import DenseRetriever
from app.retrieval.trigram import TrigramRetriever
from app.retrieval.fusion import rrf_fuse, entity_overlap_bonus
from app.retrieval.reranker import RerankerWrapper
from app.retrieval.retriever import HybridRetriever

__all__ = [
    "BM25Retriever", "DenseRetriever", "TrigramRetriever",
    "rrf_fuse", "entity_overlap_bonus",
    "RerankerWrapper", "HybridRetriever",
]
```

### `app/api/deps.py`
Add `get_retriever()` lazy singleton and `reset_retriever_for_tests()`. Mirror the existing `_embedder` pattern (`app/api/deps.py:15,30-32`). Both names added to `__all__`:

```python
_retriever: HybridRetriever | None = None

async def get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        embedder = await get_embedder()
        _retriever = HybridRetriever(
            bm25=BM25Retriever(),
            dense=DenseRetriever(),
            trigram=TrigramRetriever(),
            reranker=RerankerWrapper(BGEReranker()),
            embedder=embedder,
        )
    return _retriever

def reset_retriever_for_tests() -> None:
    global _retriever
    _retriever = None
```

### `app/settings.py`
Add the six retrieval settings above.

---

## Tests to create (8 files)

Test scaffolding facts (verified): `pytest-asyncio` with `@pytest.mark.asyncio`; integration tests use `db_session` fixture from `tests/integration/conftest.py:102` (autouse Alembic migrations from `:85-90`, `NullPool` engine from `:93`); root `tests/conftest.py` exposes a `client` fixture only.

| File | What it covers |
|---|---|
| `tests/unit/test_rrf.py` | Known 3-list input → verify RRF math and weight application exactly |
| `tests/unit/test_entity_bonus.py` | Chunk with "Pearson Specter Litt" in entities + matching query → higher fused score |
| `tests/integration/test_retrieval_baseline.py` | 3 fixture docs; query "parties to the case"; top result contains "plaintiff" or "defendant" |
| `tests/integration/test_retrieval_legal_terms.py` | Chunks with "42 U.S.C. § 1983" + "habeas corpus"; citation chunk ranked first (NN-8) |
| `tests/integration/test_retrieval_partial_doc.py` | One ready doc, one `ocr_running` doc; retrieval never returns in-progress chunks (NN-1) |
| `tests/integration/test_reranker_timeout.py` | Mock BGEReranker to sleep 5s; assert un-reranked results + `degraded_mode=True` |
| `tests/integration/test_multi_retrieve.py` | 5-query dict; all 5 keys return results; wall time < 1s (concurrent) |
| `tests/integration/test_dense_hnsw_index.py` | `EXPLAIN ANALYZE` on a dense query; assert output contains `Index Scan` on `chunks_embedding_hnsw_idx`. **Flake-proofing:** the planner will pick seq-scan over HNSW on small fixture sets. Either (a) seed ≥1k synthetic embedded chunks, or (b) `SET LOCAL enable_seqscan = off` for the assertion query only. Plan picks (b) to keep fixtures cheap. |

---

## Acceptance checklist

- [ ] BM25 uses `plainto_tsquery('legal_en', ...)` — not 'english'
- [ ] Dense query sets `SET LOCAL hnsw.ef_search`, `work_mem`, `statement_timeout` (NN-4)
- [ ] Trigram fires when proper nouns / § detected (or always if config flag)
- [ ] Entity overlap bonus wired into RRF
- [ ] Per-doc cap of `MAX_CHUNKS_PER_DOC_PER_QUERY=3` applied before rerank
- [ ] Reranker 2s timeout degrades gracefully with `degraded_mode` marker on `RetrievedChunk` (not on `Chunk.metadata`)
- [ ] `multi_retrieve` is fully concurrent (`asyncio.gather`)
- [ ] Retrieval never returns chunks from non-ready documents (NN-1)
- [ ] HNSW index actually used — `EXPLAIN ANALYZE` in `test_dense_hnsw_index.py` shows `Index Scan on chunks_embedding_hnsw_idx`
- [ ] All 8 tests pass

---

## Risks

| Risk | Mitigation |
|---|---|
| BGEEmbedder/BGEReranker model not downloaded in CI | Tests mock the model; integration tests use `StubEmbedder` / mock reranker |
| `SET LOCAL` in async SQLAlchemy | `SET LOCAL` is **transaction-scoped** (not connection-scoped). Run SETs and the ORDER BY inside one `async with session.begin():`. Values must be SQL literals — bind params don't work for `SET LOCAL` — so validate settings strings before f-string interpolation. |
| `Chunk.metadata` attr collision | The ORM JSONB col is `metadata_` (Python) → `"metadata"` (SQL); `.metadata` resolves to `DeclarativeBase.metadata`. Use the `RetrievedChunk` wrapper for in-flight per-query state (`score`, `degraded_mode`) instead of touching the ORM JSONB. |
| HNSW planner picks seq-scan on small fixtures | Use `SET LOCAL enable_seqscan = off` in the EXPLAIN-ANALYZE test only. |
| Trigram index requires `pg_trgm` extension | Already enabled in `0001_initial.py:20` |
| `extract_entities(query)` returns `[]` on lowercase queries | Documented: entity bonus only fires when the query contains capitalized runs / statute citations. Not a bug, but tests must use realistic capitalized fixtures. |

---

## Definition of done

After all tests pass, write `docs/milestones/M6-DONE.md` (what shipped, deviations from spec, follow-ups for v1.1).

---

## Sub-agent delegation (implementation phase)

Per M6 spec, after protocols are agreed:
- Sub-agent A: `bm25.py` + `test_retrieval_legal_terms.py` (integration — NN-8 legal_en)
- Sub-agent B: `dense.py` + `test_retrieval_baseline.py` + `test_dense_hnsw_index.py`
- Sub-agent C: `trigram.py` + `test_retrieval_partial_doc.py` (NN-1)
- Sub-agent D: `fusion.py` + `types.py` (RetrievedChunk) + `test_rrf.py` + `test_entity_bonus.py`
- Sub-agent E: `reranker.py` + `test_reranker_timeout.py`
- Sequential: `retriever.py` + `deps.py` wiring + `test_multi_retrieve.py`
