# M5 — Layout Parsing, Chunking, Embedding

**Estimated time:** 2 hours
**Dependencies:** M1, M2, M4
**Rubric impact:** Document Processing (back half of 25 pts) + Retrieval (sets up the index)

## Goal

Turn OCR output into a block tree, chunk that tree semantically, extract entities, and embed everything. After this milestone, `app.blocks` and `app.chunks` are populated with embeddings, and `documents.status` reaches `ready`. The retrieval layer (M6) builds on top of this.

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-1, NN-8**
2. `docs/architecture/03-components/ingestion-ocr.md` — block tree shape
3. `docs/architecture/03-components/retrieval.md` — chunking and entities

## Non-Negotiables that apply

- **NN-1** — `documents.status` transitions only after embedding completes
- **NN-8** — entities extracted during chunking, stored in `chunks.entities`, used by retrieval

## Files to create / modify

### Layout

- `app/ingest/layout.py`:
  - `parse_to_block_tree(doc: Document, spans: list[Span]) -> list[Block]`
  - Uses `docling` as primary; falls back to `unstructured` if docling errors
  - Post-processing: drop empty blocks; merge orphan single-line paragraphs into prior; reading-order reconstruction; tables stored with `metadata["cells"]` 2D array; figures stored with bbox + caption text
  - Output blocks reference their source spans (`block.source_span_ids`)

### Chunking

- `app/ingest/chunker.py`:
  - `chunk_blocks(blocks: list[Block]) -> list[Chunk]`
  - Strategy from `03-components/retrieval.md`: paragraphs become chunks; long paragraphs split at sentence boundaries with overlap; tables as whole or by row group; section hierarchy threaded into `chunk.section_path`
  - For each chunk, extract entities via `extract_entities(text) -> list[str]`:
    - Regex: statute citations (`\b\d+\s+U\.?S\.?C\.?\s*§?\s*\d+(?:\([a-z]\))?\b` and variants), case citations (`\b\w+\s+v\.\s+\w+\b`), proper-noun runs (Title-Cased 2-4 word sequences not at sentence start), monetary amounts, dates
    - Returns a deduplicated list
  - Computes `token_count` via tiktoken (`cl100k_base` is fine; consistent across providers)

### Embedder (real)

- `app/llm/embedder.py` — replace M2 stub:
  - `BGEEmbedder` — uses `sentence-transformers` to load `BAAI/bge-large-en-v1.5` once per process; supports batching (default 32); `async encode(texts) -> list[np.ndarray]`
  - `OpenAIEmbedder` — uses OpenAI's embedding API (used if `EMBEDDER_PROVIDER=openai`)
  - `Embedder` Protocol unchanged
- `app/api/deps.py` — `get_embedder()` dependency; lazy single-instance

### Reranker (real, used in M6 but loaded now since the same SKILL applies)

- `app/llm/reranker_model.py` — `BGEReranker` (`BAAI/bge-reranker-base`), `cross_encode(query, docs) -> list[float]`. Lazy-load.

### Orchestration

- `app/ingest/service.py` — extend with:
  - `async parse_layout(document_id)` — block tree from spans; transitions `layout_running → layout_done`; enqueues `CHUNKING`
  - `async chunk_document(document_id)` — chunks + entities; transitions `chunking_running → chunking_done`; enqueues `EMBEDDING`
  - `async embed_chunks(document_id)` — batch-embed unembedded chunks; updates rows; sets `embedded_at`, transitions to `ready`; bumps `last_event_seq`

### Worker handlers

- `app/jobs/kinds.py` — register LAYOUT, CHUNKING, EMBEDDING handlers calling the corresponding `IngestService` methods.

### Reconciler integration

- `app/jobs/reconciler.py` — `find_partial_documents()` now correctly re-enqueues whichever stage is stuck (was set up in M3; verify it covers `embedding_running` and that re-enqueuing `EMBEDDING` is idempotent — i.e., the handler picks up where it left off by selecting `WHERE embedding_id IS NULL`).

### Tests

- `tests/integration/test_layout.py` — feed the `multi_column.pdf` fixture through full pipeline; assert blocks are in reading order (column 1 entirely before column 2)
- `tests/integration/test_layout_tables.py` — feed `table_heavy.pdf` fixture; assert at least one block of type `table` with cell-structured metadata
- `tests/unit/test_chunker.py` — feed synthetic blocks; assert section paths threaded; assert long paragraphs split with overlap; assert chunk token count below cap
- `tests/unit/test_entities.py` — text containing "42 U.S.C. § 1983", "Pearson Specter Litt", "Marbury v. Madison", "$1,250.00", "March 14, 2024" → all returned in `entities`
- `tests/integration/test_pipeline_end_to_end.py` — upload `scan_clean.pdf`; wait for `status='ready'`; assert `app.chunks` has rows with non-null `embedding`; assert `entities` arrays are non-empty for at least one chunk
- `tests/integration/test_embedding_resume.py` — kill the embedding job mid-document (mock the embedder to raise after 10 chunks); run reconciler; assert remaining chunks get embedded on retry; final state is `ready`

## Acceptance criteria

- [ ] All fixture documents (except `corrupt.pdf`) reach `status='ready'`
- [ ] Each chunk has a 1024-d embedding and a populated `entities` array where applicable
- [ ] Reading order is correct on multi-column samples
- [ ] Tables retain cell structure
- [ ] Embedder is loaded once per worker process (check by logging model-load time)
- [ ] `legal_en` text search config returns hits for "habeas corpus" against a chunk containing it (proves NN-8 is wired even if minimally)

## Out of scope

- Real retrieval — M6
- Per-template chunking strategies — v1.1
- Entity linking / NER beyond regex — v1.1

## Definition of done

A `make seed && make pipeline-status` shows every fixture in `ready` state with chunk counts and entity counts per document. `M5-DONE.md` written.

## Sub-agent delegation

After `app/ingest/layout.py` is in:

- Sub-agent A: `app/ingest/chunker.py` + chunker tests
- Sub-agent B: `app/llm/embedder.py` real implementation + embedder tests
- Sub-agent C: extending `app/ingest/service.py` orchestration + integration tests

These are independent. Embedder is the long-pole (model download + first run).
