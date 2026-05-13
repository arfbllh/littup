# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Pearson Specter Litt ("littup") turns messy legal-style PDFs into grounded, template-driven first drafts with inspectable citations, then uses operator edits to improve subsequent drafts. Single-workspace, single-operator demo.

Stack: Python 3.11 · FastAPI · Postgres 16 + pgvector · pdfplumber · PaddleOCR · docling · bge-large-en-v1.5 · bge-reranker-base · vLLM (Qwen 2.5) · Anthropic / OpenAI / Gemini SDKs · Next.js 15 (App Router).

## Commands

```bash
make up            # Start full Docker Compose stack (api, worker, postgres, pgbouncer, vllm, ui)
make test          # Run pytest
make eval          # Run eval harness (requires running stack + seed data)
make seed          # Load fixture docs into a running stack
make reset-db      # Drop and recreate the database

# Individual services
docker compose up api
docker compose up worker
python -m app.jobs.worker      # Worker process directly
uvicorn app.main:app --reload  # FastAPI dev mode

# Tests
pytest tests/unit/
pytest tests/integration/
pytest tests/unit/test_classifier.py  # Single file
pytest -k "test_ingest_idempotency"   # Single test

# Eval
python eval/run_all.py                # All eval scripts → eval/reports/
python eval/run_retrieval.py
python eval/run_citation_validity.py
python eval/run_edit_improvement.py
```

## Architecture

### Component map

| Module | Responsibility |
|--------|---------------|
| `app/ingest/` | Upload → classify → OCR → layout parse → block tree |
| `app/retrieval/` | Chunk, embed, BM25+dense index, hybrid retrieve, rerank |
| `app/draft/` | Template-driven field extraction + section generation + citation validation |
| `app/edits/` | Structured diff capture, few-shot store, offline rule extraction |
| `app/llm/` | Single LLM router over vLLM / Anthropic / OpenAI / Gemini |
| `app/jobs/` | Postgres-backed job queue + worker process |
| `app/api/` | Thin FastAPI routes — no business logic |
| `app/core/` | structlog logging, typed AppError hierarchy, request-ID middleware |
| `app/db/` | ORM models (one file per aggregate) + Alembic migrations |
| `ui/` | Next.js 15 (App Router) operator UI |
| `config/templates/` | DraftTemplate YAML definitions |
| `config/router.yaml` | LLM tier config — swap providers without code changes |

### Data plane

One Postgres 16 + pgvector instance, three schemas:
- `app` — documents, chunks, drafts, edits, templates (OLTP)
- `jobs` — job queue and history (append-heavy, frequent UPDATE)
- `llm_log` — LLM request log + response cache (append-only, partitioned monthly)

pgbouncer sits in front (transaction-pooling mode). Heavy queries set `work_mem = '64MB'` and `statement_timeout = '5s'` locally.

### LLM Router tiers (`app/llm/router.py`)

Routes by task, not vendor. Local-first, hosted as fallback. Config in `config/router.yaml`.

| Tier | Local default | Hosted fallback |
|------|-------------|----------------|
| `extraction` | Qwen 2.5 14B (JSON mode) | claude-haiku-4-5 |
| `generation` | Qwen 2.5 32B | claude-sonnet-4-5 |
| `validation` | Qwen 2.5 7B | claude-haiku-4-5 |
| `vision` | — (none in v1) | claude-sonnet-4-5 vision |
| `analysis` | Qwen 2.5 32B | claude-sonnet-4-5 |

`app/llm/` is the **only** place that imports LLM SDK libraries. All other modules call `LLMRouter`.

### Draft engine flow (`app/draft/engine.py`)

1. Load and snapshot template once at `generate()` entry (never re-query registry mid-call).
2. Multi-query retrieval per field/section in parallel.
3. Pass 1 — Field extraction: small model, JSON schema, structured output.
4. Pass 2 — Section generation: large model, prose with `[chunk:CHUNK_ID]` citations; injects 2–3 few-shot examples from the edit store.
5. Pass 3 — Citation validation: each cited chunk is checked to actually support its claim.

### Edit loop (`app/edits/`)

- **Stage 1 (immediate):** Each saved edit is embedded and indexed; next draft for the same template+field retrieves similar past edits as in-context examples.
- **Stage 2 (periodic/triggered):** `RuleExtractor` clusters recent edits, appends durable rules to the template system prompt, bumps `prompt_fingerprint`.

### Ingestion state machine

Documents progress through: `uploaded → ocr_pending → ocr_running → ocr_done → layout_running → layout_done → chunking_running → chunking_done → embedding_running → ready` (or `failed`). Retrieval only queries `status = 'ready'` documents.

## Non-Negotiables

Read `docs/architecture/10-fixes-and-non-negotiables.md` before any milestone. Every rule is an acceptance criterion.

| ID | Rule |
|----|------|
| NN-1 | Ingestion uses Postgres job queue (not BackgroundTasks); granular state machine; startup reconciler re-queues stuck jobs |
| NN-2 | Hash idempotency via `INSERT ... ON CONFLICT (sha256) DO UPDATE ... RETURNING (xmax = 0) AS inserted` — atomic, no race |
| NN-3 | Worker concurrency bounded by `asyncio.Semaphore`; `POST /documents` returns 429 when >100 jobs queued |
| NN-4 | Three Postgres schemas; pgbouncer in Compose; `SET LOCAL work_mem` for heavy queries; explicit HNSW index params |
| NN-5 | Template snapshotted once at `generate()` entry; `prompt_fingerprint = sha256(system_prompt + rules + schema + sections)` is the cache/edit-log key |
| NN-6 | VLM has per-document page cap (`MAX_VLM_PAGES_PER_DOC`) and global hourly USD spend cap |
| NN-7 | LLM cache key = `sha256(model_id + messages + schema + sampling_params)` — content-addressed |
| NN-8 | BM25 uses custom `legal_en` Postgres text search config (not default English); tri-gram pass for proper nouns; entity GIN index |
| NN-9 | Rule extractor has `POST /admin/rule-extractor/run` endpoint + APScheduler cron (every 6h); idempotent |
| NN-10 | SSE events carry IDs; endpoint honors `Last-Event-ID`; UI falls back to polling every 5s after 30s disconnect |
| NN-11 | Edits have `few_shot_indexed_at`; reconciliation sweep retries failed embeddings with exponential backoff |
| NN-12 | structlog JSON to stdout; typed `AppError` hierarchy; `X-Request-ID` header in every request/response; LLM calls logged to `llm_log.llm_requests` |

## Milestone workflow

See `docs/milestones/00-roadmap.md` for the dependency graph and schedule.

For each milestone:
1. Read: `docs/architecture/00-summary.md`, `docs/architecture/10-fixes-and-non-negotiables.md`, `docs/architecture/project-skeleton.md`, `docs/architecture/02-architecture.md`, the relevant component doc(s), the milestone spec.
2. Write `PLAN.md` — files touched, acceptance checks, risks. **Do not write code before plan is approved.**
3. Implement after plan approval.
4. Write tests listed in the spec.
5. Write `docs/milestones/M<N>-DONE.md` — what shipped, deviations, follow-ups.

### Sub-agent opportunities

The following milestones have naturally parallel sub-tasks suited for parallel agents:
- **M2**: one sub-agent per provider (`vllm.py`, `anthropic.py`, `openai.py`, `gemini.py`) — stub configs in `.claude/subagents/m2-llm-providers/`
- **M4**: one sub-agent per OCR engine (`pdfplumber_ocr.py`, `paddle_ocr.py`, `vlm_ocr.py`, `preprocess.py`) — `.claude/subagents/m4-ocr-engines/`
- **M11**: one sub-agent per UI screen after `ui/lib/api.ts` is shared — `.claude/subagents/m11-ui-screens/`
- **M12**: one sub-agent per eval script — `.claude/subagents/m12-eval-scripts/`

## Key invariants

- Routes in `app/api/routes/` parse input, call into service modules, return responses. Nothing else.
- `app/db/models/` is one file per aggregate (`document.py`, `chunk.py`, `draft.py`, `edit.py`, `template.py`, `job.py`, `llm_log.py`).
- `config/templates/*.yaml` is config, not code. Adding a new draft type = new YAML file, no new Python.
- Retrieval never returns chunks from a document whose `status != 'ready'`.
- The `prompt_fingerprint` (not the integer `template.version`) is what edit logs and the LLM cache use as the template identity key.
