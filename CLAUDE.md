# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository. For an end-user view of the project, read `README.md`.

## Project

littup turns messy legal-style PDFs into grounded, template-driven first drafts with inspectable citations, then uses operator edits to improve subsequent drafts. Single-workspace, single-operator system.

Stack: Python 3.11 · FastAPI · Postgres 16 + pgvector · pdfplumber · PaddleOCR · docling · bge-large-en-v1.5 · bge-reranker-base · Ollama (local LLM, OpenAI-compatible) · Anthropic / OpenAI / Gemini SDKs · Next.js 15 (App Router).

## Commands

```bash
make up            # Start full Docker Compose stack (api, worker, postgres, pgbouncer)
make dev           # uvicorn app.main:app --reload
make worker        # python -m app.jobs.worker
make migrate       # alembic upgrade head
make reset-db      # Drop and recreate the database, then migrate
make seed          # Load fixture docs into a running stack
make test          # pytest tests/unit
make test-integration
make test-all      # unit + integration with coverage
make eval          # Run eval harness → eval/reports/
make lint          # ruff check
make fmt           # ruff format + --fix

pytest tests/unit/test_classifier.py    # Single file
pytest -k "test_ingest_idempotency"     # Single test
```

## Architecture

### Component map

| Module | Responsibility |
|--------|---------------|
| `app/ingest/` | Upload → classify → OCR → layout parse → block tree |
| `app/retrieval/` | Chunk, embed, BM25+dense index, hybrid retrieve, rerank |
| `app/draft/` | Template-driven field extraction + section generation + citation validation |
| `app/edits/` | Structured diff capture, few-shot store, offline rule extraction |
| `app/llm/` | Single LLM router over Ollama / Anthropic / OpenAI / Gemini |
| `app/jobs/` | Postgres-backed job queue + worker process + APScheduler |
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

pgbouncer sits in front (transaction-pooling mode). Alembic uses `DATABASE_URL_DIRECT` to bypass the pooler for DDL. Heavy queries set `work_mem = '64MB'` and `statement_timeout = '5s'` locally.

### LLM Router tiers (`app/llm/router.py`)

Routes by task, not vendor. Local-first, hosted as fallback. Config in `config/router.yaml`.

| Tier | Local default (Ollama) | Hosted fallback |
|------|-------------|----------------|
| `extraction` | `nemotron-3-super:cloud` (JSON mode) | claude-haiku-4-5 |
| `generation` | `nemotron-3-super:cloud` | claude-sonnet-4-5 |
| `validation` | `nemotron-3-super:cloud` | claude-haiku-4-5 |
| `vision` | — (none locally) | claude-sonnet-4-5 vision |
| `analysis` | `nemotron-3-super:cloud` | claude-sonnet-4-5 |

`app/llm/` is the **only** place that imports LLM SDK libraries. All other modules call `LLMRouter`.

**Local LLM = Ollama.** The `ollama` provider type uses Ollama's native `/api/generate` endpoint. Point `OLLAMA_BASE_URL` at the daemon root (no `/v1` suffix). The `:cloud` tag tells Ollama to run inference on its hosted infrastructure (free, rate-limited) rather than locally; for fully-local inference, swap the tag for one you've `ollama pull`ed (e.g. `qwen2.5:14b-instruct-q4_K_M`).

```bash
brew install ollama
ollama serve &                                # listens on :11434
ollama signin                                 # required once for :cloud models
# in .env
OLLAMA_BASE_URL=http://localhost:11434
```

**Disable a provider by clearing its env.** The router skips any provider whose required env is absent or empty:

| Env | When absent/empty | Effect |
|---|---|---|
| `OLLAMA_BASE_URL` | not set in env | every `type: ollama` entry skipped → Anthropic handles every call |
| `ANTHROPIC_API_KEY=""` | Anthropic | every `type: anthropic` entry skipped → only Ollama/OpenAI remain |
| `OPENAI_API_KEY=""` | OpenAI | every `type: openai` entry skipped |
| `GEMINI_API_KEY=""` | Gemini | every `type: gemini` entry skipped |

If every provider in a tier is disabled, calls in that tier raise `LLMUnavailableError`. Worker boot logs `llm.providers_active` and `llm.providers_skipped_missing_env` so the operator can see at a glance what's wired up.

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

## Invariants

These are load-bearing system properties. Preserve them when editing.

1. **Recoverable ingestion.** Background work is a Postgres `jobs` row, not `BackgroundTasks`. Workers claim via `SELECT ... FOR UPDATE SKIP LOCKED` and write heartbeats every 10 s. A startup reconciler reclaims jobs whose heartbeat is older than `JOB_STALE_TIMEOUT` and re-queues partial documents at their missing stage. A periodic sweep runs every 5 minutes.
2. **Atomic hash idempotency.** `POST /api/documents` does a single `INSERT ... ON CONFLICT (sha256) DO UPDATE ... RETURNING (xmax = 0) AS inserted`. No application-side read-then-write race.
3. **Bounded concurrency, explicit backpressure.** Worker concurrency is an `asyncio.Semaphore` per job kind. When pending+running jobs exceed `JOB_QUEUE_MAX_PENDING` (default 100), `POST /api/documents` returns `429` with `Retry-After`.
4. **Three Postgres schemas + pgbouncer.** `app`, `jobs`, `llm_log`. HNSW index has explicit `m=16, ef_construction=64`; `ef_search` is set per-query. `llm_log.llm_requests` is partitioned monthly.
5. **Template snapshot is immutable per draft.** `DraftEngine.generate()` calls `registry.get_latest()` **once** and threads the snapshot through every sub-step. The `prompt_fingerprint = sha256(system_prompt + appended_rules + extraction_schema + sections)` — not the integer version — is the cache and edit-log identity key.
6. **VLM spend is capped.** Per-document `MAX_VLM_PAGES_PER_DOC` and global rolling-window `LLM_HOURLY_BUDGET_USD`. Over → typed `BudgetExceededError`; cheap local path unaffected.
7. **LLM cache is content-addressed.** Key = `sha256(model_id + messages + schema + sampling_params)`. Fully-resolved messages contain the appended rules, so any prompt change busts the cache automatically.
8. **BM25 is tuned for legal text.** Custom `legal_en` Postgres text-search config (not default English); `pg_trgm` proper-noun fallback; separate entity GIN index folded into RRF.
9. **Rule extractor is triggered, not hopeful.** APScheduler in the worker runs it every `RULE_EXTRACTOR_INTERVAL_HOURS`; `POST /admin/rule-extractor/run` is the manual trigger. Idempotent — re-running on the same edit set produces the same rule set.
10. **SSE has a polling fallback.** Every SSE event carries an `id`; the endpoint honours `Last-Event-ID`; the UI falls back to `GET /api/documents/{id}` polling at 5 s intervals after a 30 s disconnect. The status endpoint is the source of truth.
11. **Few-shot embeddings have a safety net.** Edits have `few_shot_indexed_at`; a reconciliation sweep retries any row with `few_shot_indexed_at IS NULL AND created_at < NOW() - INTERVAL '1 minute'` with exponential backoff.
12. **Observability is built in.** structlog JSON to stdout; typed `AppError` hierarchy with codes; `X-Request-ID` middleware on every request/response; every LLM call writes to `llm_log.llm_requests`.

## Key code rules

- Routes in `app/api/routes/` parse input, call service modules, return responses. Nothing else.
- `app/db/models/` is one file per aggregate (`document.py`, `chunk.py`, `draft.py`, `edit.py`, `template.py`, `job.py`, `llm_log.py`).
- `app/llm/` is the only place that imports vendor LLM SDKs. Everything else uses `LLMRouter`.
- `config/templates/*.yaml` is config, not code. Adding a new draft type = new YAML file, no new Python.
- Retrieval never returns chunks from a document whose `status != 'ready'`.
- The `prompt_fingerprint` (not the integer `template.version`) is what edit logs and the LLM cache use as the template identity key.
