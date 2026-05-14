# PLAN — M5: Layout Parsing, Chunking, Embedding

## What this milestone does

Wires the three pipeline stages that follow OCR — LAYOUT → CHUNKING → EMBEDDING — so every
ingested document reaches `status='ready'` with `app.blocks`, `app.chunks`, and 1024-d pgvector
embeddings populated. The OCR handler already enqueues a LAYOUT job on completion (M4); M5
implements the handlers and service methods that drain those jobs.

**NN rules in scope:** NN-1 (crash-recoverable stages), NN-8 (entity extraction for legal-text BM25).

---

## What already exists (do not recreate)

| Artifact | Location | Verified |
|---|---|---|
| `app.blocks` / `app.chunks` DDL + HNSW + GIN indexes | `0001_initial.py` | ✓ |
| `legal_en` FTS config (`COPY = english`, no dict overrides yet) | `0001_initial.py:28-42` | ✓ |
| `chunks_tsv_trigger` — populates `text_tsv` via `to_tsvector('legal_en', ...)` on INSERT/UPDATE | `0001_initial.py:168-183` | ✓ |
| `JobKind.LAYOUT / CHUNKING / EMBEDDING` | `app/jobs/kinds.py` | ✓ |
| `Reconciler._NEXT_STAGE` mapping — covers `ocr_done→layout`, `layout_done→chunking`, `chunking_done→embedding`, `embedding_running→embedding` | `app/jobs/reconciler.py:11-21` | ✓ |
| OCR handler enqueues LAYOUT on completion | `app/jobs/handlers/ocr.py:35-40` | ✓ |
| `Embedder` Protocol: `name`, `dim`, `async embed(texts) -> list[list[float]]`, `async health()` | `app/llm/embedder.py` | ✓ |
| `Reranker` Protocol: `name`, `async rerank(query, docs) -> list[int]`, `async health()` | `app/llm/reranker_model.py` | ✓ |
| `settings.EMBEDDING_MODEL`, `EMBEDDING_BATCH_SIZE`, `RERANKER_MODEL` | `app/settings.py:34-36` | ✓ |
| `documents.embedded_at`, `documents.last_event_seq` | `app/db/models/document.py:24-25` | ✓ |
| `Block` ORM — `block_type`, `bbox_x0/y0/x1/y1`, `metadata_` (→ `metadata` column), `reading_order` | `app/db/models/document.py:73-97` | ✓ |
| `Chunk` ORM — `embedding` (VECTOR), `entities` (TEXT[]), `block_ids` (TEXT[]), `char_start/end`, `token_count` | `app/db/models/chunk.py` | ✓ |
| `new_uuid7()` | `app/core/ids.py` | ✓ |
| `DocumentEventBus.emit()`, `_set_doc_status()`, `_fail_document()` helpers | `app/ingest/service.py` | ✓ |

**Critical schema detail:** `Span` has a `block_id FK → app.blocks(id)`. Block has NO `source_span_ids`
array. After inserting blocks, update `spans.block_id` (not the other way around).

**`metadata_` / `metadata` naming:** both `Block` and `Chunk` ORM models use Python attribute
`metadata_` mapped to the DB column `metadata`. Raw SQL must use `metadata`; ORM attribute is
`metadata_`.

**`text_tsv` auto-population:** the trigger `chunks_tsv_trigger` fires on every `INSERT OR UPDATE OF
text`. No code needs to set `text_tsv` manually; just ensure `text` is correct at insert time.

---

## Files touched

### New files

| File | Purpose |
|---|---|
| `app/ingest/layout.py` | `parse_layout(document_id, session)` — loads spans, runs docling/unstructured, post-processes, bulk-inserts `app.blocks`, updates `spans.block_id` |
| `app/ingest/chunker.py` | `chunk_blocks(document_id, blocks, session)` — semantic chunker; `extract_entities(text)` regex extractor |
| `app/jobs/handlers/layout.py` | LAYOUT job handler → `service.parse_layout()` → enqueues CHUNKING |
| `app/jobs/handlers/chunking.py` | CHUNKING job handler → `service.chunk_document()` → enqueues EMBEDDING |
| `app/jobs/handlers/embedding.py` | EMBEDDING job handler → `service.embed_chunks()` → marks `ready` |
| `app/db/migrations/versions/0003_legal_en_overrides.py` | Adds `ALTER MAPPING FOR asciiword, word, numword WITH simple` to `legal_en` config |
| `tests/fixtures/conftest.py` | Generates minimal synthetic PDFs (`scan_clean.pdf`, `multi_column.pdf`, `table_heavy.pdf`, `corrupt.pdf`) via `fpdf2` if not already present |
| `tests/unit/test_chunker.py` | section_path threading, long-para split with overlap, token cap |
| `tests/unit/test_entities.py` | statute cite, case name, proper-noun run, money, date patterns |
| `tests/integration/test_layout.py` | multi-column PDF → blocks in reading order (col-1 entirely before col-2) |
| `tests/integration/test_layout_tables.py` | table-heavy PDF → at least one `block_type='table'` with `metadata["cells"]` 2D array |
| `tests/integration/test_pipeline_end_to_end.py` | upload `scan_clean.pdf` → wait `status='ready'` → assert chunks + non-null embeddings |
| `tests/integration/test_embedding_resume.py` | embedder raises after 10 chunks → reconciler re-enqueues → all chunks embedded → `ready` |

### Modified files

| File | Change |
|---|---|
| `app/llm/embedder.py` | Add `BGEEmbedder` (sentence-transformers lazy singleton, async via executor) + `OpenAIEmbedder` — both satisfy `Embedder` Protocol |
| `app/llm/reranker_model.py` | Add `BGEReranker` (CrossEncoder lazy singleton, async via executor) — satisfies `Reranker` Protocol |
| `app/api/deps.py` | Add `get_embedder()` lazy-singleton FastAPI dependency |
| `app/settings.py` | Add `EMBEDDER_PROVIDER: str = "bge"` |
| `app/ingest/service.py` | Add `parse_layout()`, `chunk_document()`, `embed_chunks()` + three `_claim_*_running()` atomic helpers |
| `app/jobs/handlers/__init__.py` | Add imports of `layout`, `chunking`, `embedding` handler modules for registration side-effects |

---

## Key design decisions

### Handler / service responsibility split

**Rule**: the *handler* enqueues the next stage job. The *service method* handles status transitions
and business logic only. This matches the existing OCR pattern:
- `handle_ocr` calls `service.ocr_document()`, then enqueues `JobKind.LAYOUT`
- `handle_layout` calls `service.parse_layout()`, then enqueues `JobKind.CHUNKING`
- `handle_chunking` calls `service.chunk_document()`, then enqueues `JobKind.EMBEDDING`
- `handle_embedding` calls `service.embed_chunks()` — no next stage to enqueue

Each handler registers itself: `HANDLERS[JobKind.LAYOUT.value] = handle_layout`.

### `layout.py` — `parse_layout(document_id, session)`

1. Atomic status claim: `_claim_layout_running(document_id)` — `UPDATE ... WHERE status='ocr_done'
   RETURNING id`. Returns `False` if another worker already owns it.
2. Load spans: `SELECT s.*, p.page_number FROM app.spans s JOIN app.pages p ON s.page_id = p.id
   WHERE p.document_id = :doc_id ORDER BY p.page_number, s.bbox_y0, s.bbox_x0`.
3. Concatenate span text into page groups; feed to `docling`. On any exception fall back to
   `unstructured`.
4. Post-process the block list:
   - Drop empty blocks (`block.text.strip() == ""`).
   - Merge orphan single-line paragraphs into prior block.
   - Reading-order reconstruction for multi-column pages: bucket blocks by x-midpoint
     (left vs right column) then re-sort by `(page, column_bucket, bbox_y0)`.
   - Tables: `metadata["cells"]` 2D array from docling table output (list of lists of strings).
   - Figures: `metadata["bbox"]` + `metadata["caption"]`.
5. Bulk-INSERT into `app.blocks` (`block_type`, `bbox_x0/y0/x1/y1`, `text`, `metadata`,
   `reading_order`, `page_start`, `page_end`, `document_id`). Use a single `executemany` call.
6. After block insert, update `spans.block_id` where the span's page and bbox falls within the
   block's page range and bbox bounds. SQL:
   ```sql
   UPDATE app.spans s SET block_id = :block_id
   FROM app.pages p
   WHERE s.page_id = p.id
     AND p.page_number BETWEEN :page_start AND :page_end
     AND s.bbox_x0 >= :bx0 AND s.bbox_y0 >= :by0
     AND s.bbox_x1 <= :bx1 AND s.bbox_y1 <= :by1
   ```
   Run as batch after all blocks are inserted.
7. Transition `layout_running → layout_done`; emit SSE event; commit.

### `chunker.py` — `chunk_blocks(document_id, blocks) -> list[dict]`

Returns a list of dicts matching the `app.chunks` column names (not ORM instances) — easier for
bulk insert via `executemany`.

Required fields per chunk dict:
- `id`: `new_uuid7()`
- `document_id`
- `text`, `token_count` (via `tiktoken.get_encoding("cl100k_base")`)
- `chunk_type`: `"paragraph"` | `"table"` | `"table_rows"` | `"list"` | `"header_region"`
- `section_path`: `list[str]` — inherited from the active section header stack
- `block_ids`: `list[str]` — UUIDs of source blocks
- `page_start`, `page_end`
- `char_start`, `char_end` — byte offsets into the concatenated document text (track a running
  cursor as blocks are consumed in reading order)
- `entities`: output of `extract_entities(text)` — deduplicated list
- `metadata`: for tables, `{"cells": [[...]]}` from `block.metadata_["cells"]`

Chunking strategy:
- Section/header blocks → hard boundary; push to section-path stack; emit a `"header_region"` chunk
  containing the header text.
- Paragraph blocks → individual chunks. If `token_count > 800`, split at sentence boundaries
  (scan for `. ` followed by a capital) with 50-token overlap. Each split chunk shares the same
  `section_path`.
- Table blocks ≤ 800 tokens → single `"table"` chunk. Larger → split into row groups of ≈10 rows
  (`"table_rows"` chunk type), each row group is one chunk.
- List blocks < 10 items → one `"list"` chunk. ≥ 10 items → windows of ≈10 (`"list"` type).

### `extract_entities(text) -> list[str]`

Regex patterns applied in order, output deduplicated:

```python
PATTERNS = [
    r'\b\d+\s+U\.?S\.?C\.?\s*§?\s*\d+(?:\([a-z]\))?\b',       # statute cites
    r'\b[A-Z]\w+\s+v\.\s+[A-Z]\w+\b',                           # case cites
    r'(?<!\.\s)\b(?:[A-Z][a-z]+\s){1,3}[A-Z][a-z]+\b',         # proper-noun runs
    r'\$[\d,]+(?:\.\d{2})?',                                      # monetary amounts
    r'\b(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+\d{1,2},?\s+\d{4}\b',  # dates
]
```

### `BGEEmbedder` (satisfies `Embedder` Protocol)

```python
class BGEEmbedder:
    name = "bge"
    dim = 1024

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def health(self) -> bool: ...
```

- Process-level `SentenceTransformer` singleton; lazy-load on first call; log `model_loaded` once
  at INFO with load-time.
- `embed()` runs `model.encode(batch, batch_size=settings.EMBEDDING_BATCH_SIZE, normalize_embeddings=True)`
  in `asyncio.get_running_loop().run_in_executor(None, ...)`.
- Returns `list[list[float]]` (dim=1024).

### `OpenAIEmbedder` (satisfies `Embedder` Protocol)

```python
class OpenAIEmbedder:
    name = "openai"
    dim = 1536
    _model = "text-embedding-3-small"

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def health(self) -> bool: ...
```

Selected when `settings.EMBEDDER_PROVIDER == "openai"`.

### `BGEReranker` (satisfies `Reranker` Protocol)

```python
class BGEReranker:
    name = "bge"

    async def rerank(self, query: str, docs: list[str]) -> list[int]: ...
    async def health(self) -> bool: ...
```

- Process-level `CrossEncoder("BAAI/bge-reranker-base")` singleton; lazy-load; log once.
- `rerank()` runs `model.predict([(query, d) for d in docs])` in executor; returns indices sorted
  by descending score.

### `get_embedder()` in `deps.py`

```python
_embedder: Embedder | None = None

async def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        if settings.EMBEDDER_PROVIDER == "openai":
            _embedder = OpenAIEmbedder()
        else:
            _embedder = BGEEmbedder()
    return _embedder
```

### `embed_chunks` — idempotent resume (NN-1)

1. `_claim_embedding_running(document_id)` — `UPDATE ... WHERE status='chunking_done' RETURNING id`.
   Returns `False` if already claimed; handler exits early (no duplicate work).
2. `SELECT id, text FROM app.chunks WHERE document_id = :doc_id AND embedding IS NULL` —
   re-run after crash picks up exactly where it left off.
3. Embed in batches of `settings.EMBEDDING_BATCH_SIZE`. For each batch:
   ```sql
   UPDATE app.chunks SET embedding = CAST(:v AS vector) WHERE id = :id
   ```
   where `:v` is `'[f1,f2,…,f1024]'` (pgvector literal format). Commit after each batch.
4. After all chunks are embedded:
   ```sql
   UPDATE app.documents
      SET status = 'ready', embedded_at = NOW(), updated_at = NOW()
    WHERE id = :doc_id
   ```
5. Emit SSE event via `DocumentEventBus.emit(session, doc_id, "status_changed", {"to": "ready"})`.
   Increment `last_event_seq` (NN-10).

### Migration 0003 — `legal_en` dict overrides (NN-8)

```sql
ALTER TEXT SEARCH CONFIGURATION legal_en
  ALTER MAPPING FOR asciiword, word, numword
  WITH simple;
```

This overrides the mapping that 0001 left at the `english_stem` default. After this migration,
tokens like "Specter", "§ 1983", "U.S.C." are indexed unstemmed. The trigger installed in 0001
already uses `to_tsvector('legal_en', ...)` so it will benefit automatically on next INSERT/UPDATE.

---

## Acceptance criteria

- [ ] All fixture docs (except `corrupt.pdf`) reach `status='ready'`
- [ ] Every chunk row has `embedding IS NOT NULL` (1024-d)
- [ ] `entities` array non-empty for ≥1 chunk per fixture doc
- [ ] Reading order correct on multi-column fixture (column-1 blocks precede column-2 blocks)
- [ ] Tables retain `metadata["cells"]` 2D array in chunk rows
- [ ] `model_loaded` log line appears exactly once per worker process start
- [ ] `SELECT to_tsvector('legal_en', 'habeas corpus')` returns a non-empty tsvector (migration 0003)
- [ ] `test_embedding_resume`: partial run → reconciler → `ready`
- [ ] `pytest tests/unit/ -v` passes with no DB required

---

## Risks

| Risk | Mitigation |
|---|---|
| `docling` heavy dependency / not installed | Fallback to `unstructured`; add both to `requirements.txt`; catch `ImportError` |
| bge-large-en-v1.5 model download (~1.3 GB) slows CI | Unit tests mock `BGEEmbedder`; integration tests `pytest.mark.skipif` when `sentence_transformers` absent |
| pgvector format mismatch on INSERT | Explicit `CAST(:v AS vector)` with `'[f1,f2,…]'` string — no ambiguity |
| Fixture PDFs missing from `tests/fixtures/docs/` | `conftest.py` generates minimal synthetic PDFs via `fpdf2` at session scope if files absent |
| docling column-order failure | Fallback: sort blocks by `(page_number, x_midpoint_bucket, bbox_y0)` using span midpoints |
| Double-enqueue from reconciler + handler completing at same time | `dedup_key=f"chunking:{doc_id}"` on every enqueue; idempotent status-claim with `WHERE status='layout_done'` |

---

## Sub-agent delegation (implementation phase)

After `app/ingest/layout.py` and `tests/fixtures/conftest.py` are written:

| Agent | Scope |
|---|---|
| A | `app/ingest/chunker.py` + `tests/unit/test_chunker.py` + `tests/unit/test_entities.py` (pure Python, no DB) |
| B | Real `app/llm/embedder.py` + `app/llm/reranker_model.py` + unit tests mocking sentence-transformers |
| C | `service.py` additions + all three handlers + `app/api/deps.py` + `app/settings.py` + integration tests |

All three agents are independent once `layout.py` and `conftest.py` exist.

---

## Definition of done

`M5-DONE.md` written covering: what shipped, deviations from plan, and known follow-ups.

---

## Verification

```bash
# Unit (no DB, no model weights)
pytest tests/unit/test_chunker.py tests/unit/test_entities.py -v

# Integration (requires running Postgres + migration 0003 applied)
pytest tests/integration/test_layout.py tests/integration/test_layout_tables.py -v
pytest tests/integration/test_pipeline_end_to_end.py -v
pytest tests/integration/test_embedding_resume.py -v

# Full suite
make test

# Manual smoke
make seed && make pipeline-status   # every fixture shows status=ready, chunk count, entity count
```
