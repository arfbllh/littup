# littup

Turn messy legal-style PDFs into grounded, template-driven first drafts with inspectable citations, then use operator edits to make subsequent drafts better. Single-workspace, single-operator system.

---

## Table of contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Project layout](#project-layout)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [LLM router — how to build, configure, and extend](#llm-router--how-to-build-configure-and-extend)
- [Running services individually](#running-services-individually)
- [HTTP API](#http-api)
- [Templates](#templates)
- [Database](#database)
- [Tests and evaluation](#tests-and-evaluation)
- [Operational notes](#operational-notes)

---

## Overview

littup ingests folders of mixed legal-style documents (clean PDFs, rotated scans, handwritten margins), routes each file through the right OCR path, indexes the result into a hybrid BM25 + dense corpus, and uses a `DraftTemplate` to generate a structured first draft. Every claim in the draft carries a `[chunk:CHUNK_ID]` citation that a validator checks against the source. When the operator edits the draft, the system captures a structured field-level diff and reuses it two ways:

- **Few-shot retrieval** — embeds the edit, looks up similar past edits at next-draft time, injects them as in-context examples.
- **Rule extraction** — periodically clusters recent edits per `(template, field)`, asks an LLM to extract a durable house-style rule, appends it to the template's system prompt.

The LLM stack is pluggable. A single `LLMRouter` abstracts vLLM (local), Anthropic, OpenAI, and Gemini behind one interface, with task-tier routing (`extraction`, `generation`, `validation`, `vision`, `analysis`) and automatic failover.

## Features

- Idempotent multi-file upload (PDF, PNG, JPG, TIFF, DOCX) with SHA-256 deduplication.
- Per-page classification → routed OCR (native `pdfplumber` → PaddleOCR → VLM escalation).
- Layout parsing into a block tree (sections, paragraphs, tables, lists) with `(page, bbox, confidence)` per span.
- Semantic chunking on the block tree (not naive sliding windows).
- Hybrid retrieval: Postgres FTS (custom `legal_en` config) + `pg_trgm` proper-noun fallback + pgvector HNSW dense, fused with RRF and reranked by `bge-reranker-base`.
- Template-driven draft engine with three passes: field extraction → section generation with citations → citation validation.
- Structured edit capture, few-shot store, offline rule extractor with template version bump.
- SSE document-status stream with `Last-Event-ID` replay and polling fallback.
- Operator UI (Next.js 15, App Router) for upload, draft, edit, and admin/stats.
- Postgres-backed job queue and worker process (no Redis, no Celery).
- Per-document and global rolling spend caps on hosted LLM calls.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        Next.js operator UI (ui/)                         │
└──────────────────────────────────────────────────────────────────────────┘
                                   │ HTTP / SSE
┌──────────────────────────────────────────────────────────────────────────┐
│                              FastAPI (app/api)                           │
└──────────────────────────────────────────────────────────────────────────┘
        │              │                  │                 │
        ▼              ▼                  ▼                 ▼
   ingest+OCR      retrieval          draft engine      edit loop
   (app/ingest)   (app/retrieval)    (app/draft)       (app/edits)
        │              │                  │                 │
        └──────────────┴────────┬─────────┴─────────────────┘
                                ▼
                         LLM router (app/llm)
                   vLLM | Anthropic | OpenAI | Gemini
                                │
                                ▼
              Postgres 16 + pgvector + pg_trgm (app/db)
                  schemas: app · jobs · llm_log
                                ▲
                                │ (queue)
                       Worker (app.jobs.worker)
```

Four pipelines, one data plane, one model router.

1. **Ingest → OCR → Parse.** Native PDFs go through `pdfplumber`; scans through PaddleOCR with preprocessing; handwriting and busted layouts escalate to a vision LLM. `docling` builds the block tree.
2. **Index → Retrieve.** Semantic chunking → embed with `bge-large-en-v1.5` into pgvector + Postgres FTS. Hybrid retrieve with RRF, then rerank with `bge-reranker-base`. Top 5–8 chunks survive.
3. **Draft via templates.** A `DraftTemplate` (YAML) declares extraction fields, retrieval queries, prompt skeleton, citation enforcement, output schema, validators.
4. **Edit loop.** Each operator edit becomes a structured diff; embedded into the few-shot store; offline `RuleExtractor` distills durable rules into the template's appended system prompt.

The ingestion state machine is granular and crash-recoverable:
```
uploaded → ocr_pending → ocr_running → ocr_done →
layout_running → layout_done →
chunking_running → chunking_done →
embedding_running → ready    (or failed)
```
Retrieval only queries documents with `status = 'ready'`.

## Tech stack

| Layer | Tooling |
|---|---|
| Language | Python 3.11, TypeScript 5 |
| API | FastAPI + uvicorn |
| Database | Postgres 16, `pgvector`, `pg_trgm`, `pgcrypto` |
| Pooler | pgbouncer (transaction-pooling mode) |
| ORM | SQLAlchemy 2 (async) + Alembic |
| Queue | Postgres `jobs` table + `SELECT ... FOR UPDATE SKIP LOCKED` |
| Scheduler | APScheduler (in worker process) |
| LLM | vLLM (Qwen 2.5 7B/14B/32B Instruct), Anthropic, OpenAI, Gemini |
| Embeddings | `BAAI/bge-large-en-v1.5` via `sentence-transformers` |
| Reranker | `BAAI/bge-reranker-base` (CPU) |
| Document parsing | `pdfplumber`, `pypdf`, PaddleOCR, `docling`, `opencv-python-headless`, `Pillow`, `pdf2image` |
| UI | Next.js 15 (App Router), React 19, Tailwind CSS, SWR, `@microsoft/fetch-event-source` |
| Container | Docker Compose (`api`, `worker`, `postgres`, `pgbouncer`, optional `vllm`, optional `ui`) |
| Logging | `structlog` JSON to stdout |

## Project layout

```
littup/
├── app/                  # Python package
│   ├── api/              # HTTP routes (thin) + Pydantic schemas + SSE helpers
│   ├── core/             # structlog setup, AppError hierarchy, request-ID middleware
│   ├── db/               # SQLAlchemy models, Alembic migrations, session factories
│   ├── draft/            # Template registry, field extractor, section generator, citation validator
│   ├── edits/            # Structured diff, few-shot store, offline rule extractor
│   ├── ingest/           # Classifier, OCR engines, layout parser, chunker, reconciler
│   ├── jobs/             # Postgres job queue, worker, handlers, APScheduler tasks
│   ├── llm/              # LLMRouter + providers + response cache + budget tracker + embedder + reranker
│   ├── retrieval/        # BM25, dense, tri-gram, RRF fusion, reranker
│   ├── main.py           # FastAPI factory
│   └── settings.py       # Pydantic settings
├── config/
│   ├── router.yaml       # LLM tier and provider config
│   ├── ocr.yaml          # OCR thresholds and VLM caps
│   └── templates/        # *.yaml DraftTemplate files
├── docker/postgres/      # Postgres init scripts (extensions, custom FTS config)
├── eval/                 # Offline harness (retrieval recall, citation validity, edit improvement)
├── scripts/              # seed, reset_db, bench_ocr, fixture generators
├── tests/                # pytest (unit + integration)
└── ui/                   # Next.js 15 operator UI
```

## Requirements

- Docker + Docker Compose (preferred path)
- Python 3.11 if you want to run services directly
- Node.js 20+ if you want to run the UI directly
- Optional: a CUDA-capable GPU (24 GB+, e.g. RTX 4090 / L4 / A10) for local vLLM. Without it, the router transparently uses hosted models for generation while keeping embeddings and the reranker on CPU.

## Quick start

```bash
# 1. Configure environment
cp .env.example .env
# Open .env and set: ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY
# (at least one is needed unless vLLM is available locally).

# 2. Bring up the stack
make up                      # docker compose up --build -d

# 3. Apply migrations
make migrate                 # alembic upgrade head

# 4. Seed sample documents (optional)
make seed                    # loads tests/fixtures/docs into the running stack

# 5. Open the UI
open http://localhost:3000   # if the ui service is running
# or hit the API directly:
curl http://localhost:8000/healthz
```

The default Compose stack runs `api`, `worker`, `postgres`, and `pgbouncer`. The `vllm` and `ui` services are optional — see [Running services individually](#running-services-individually).

## Configuration

Configuration is environment-driven (`.env`, loaded by `pydantic-settings`). Every knob has a sane default in `app/settings.py`. The most important variables:

### Application

| Variable | Default | Purpose |
|---|---|---|
| `ENV` | `development` | Affects logging format and migration safety checks. |
| `LOG_LEVEL` | `INFO` | structlog level. |

### Database

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://littup:littup@pgbouncer:5432/littup` | Primary connection (through pgbouncer). |
| `DATABASE_URL_DIRECT` | `…@postgres:5432/littup` | Bypasses pgbouncer (used by Alembic and advisory-lock paths). |
| `TEST_DATABASE_URL` | `…@localhost:5432/littup_test` | Test database. |

### Worker / job queue

| Variable | Default | Purpose |
|---|---|---|
| `WORKER_CONCURRENCY_OCR` | `2` | `asyncio.Semaphore` for OCR-bound jobs. |
| `WORKER_CONCURRENCY_EMBEDDING` | `4` | Semaphore for embedding-bound jobs. |
| `WORKER_HEARTBEAT_INTERVAL` | `10` | Seconds between job heartbeats. |
| `JOB_STALE_TIMEOUT` | `120` | Seconds before a running job is reclaimed by the reconciler. |
| `JOB_QUEUE_MAX_PENDING` | `100` | Threshold above which `POST /api/documents` returns `429 Too Many Requests`. |

### LLM

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | _empty_ | Required if any Anthropic provider is in a tier and no local fallback exists. |
| `OPENAI_API_KEY` | _empty_ | Same for OpenAI. |
| `GEMINI_API_KEY` | _empty_ | Same for Gemini. |
| `VLLM_BASE_URL` | `http://vllm:8001/v1` | OpenAI-compatible endpoint of the local vLLM service. |
| `LLM_HOURLY_BUDGET_USD` | `5.00` | Global rolling-window spend cap on hosted providers. |
| `MAX_VLM_PAGES_PER_DOC` | `20` | Per-document cap on VLM OCR pages. |
| `ROUTER_CONFIG_PATH` | `config/router.yaml` | Tier and provider definitions. |

### Embedding / retrieval

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-large-en-v1.5` | Dense embedder. |
| `EMBEDDING_BATCH_SIZE` | `32` | Embed N chunks per call. |
| `RERANKER_MODEL` | `BAAI/bge-reranker-base` | Cross-encoder reranker. |
| `EMBEDDER_PROVIDER` | `bge` | `bge` (local) or `openai` (hosted). |
| `HNSW_EF_SEARCH` | `100` | Set with `SET LOCAL` per retrieval query. |
| `RETRIEVAL_WORK_MEM` | `64MB` | Per-statement `work_mem`. |
| `RETRIEVAL_STATEMENT_TIMEOUT` | `5s` | Per-statement timeout for retrieval. |
| `MAX_CHUNKS_PER_DOC_PER_QUERY` | `3` | Per-document cap during retrieval to keep evidence mixed. |
| `TRIGRAM_THRESHOLD` | `0.15` | `pg_trgm` similarity floor for proper-noun fallback. |

### Ingestion / OCR

| Variable | Default | Purpose |
|---|---|---|
| `MAX_UPLOAD_BYTES` | `50 MiB` | Per-file upload cap. |
| `OCR_USE_GPU` | `false` | PaddleOCR GPU mode. |
| `OCR_PAGE_WORKERS` | `4` | Thread-pool size for per-page OCR within a document. |
| `OCR_RASTER_DPI` | `300` | DPI used when rasterising PDF pages for OCR. |
| `SSE_KEEPALIVE_SECONDS` | `15` | SSE heartbeat. |

### Draft / edits / rule extractor

| Variable | Default | Purpose |
|---|---|---|
| `TEMPLATES_DIR` | `config/templates` | Directory loaded into the template registry on startup. |
| `DRAFT_EXTRACTION_TOP_K` | `5` | Chunks per field for extraction. |
| `DRAFT_SECTION_TOP_K` | `8` | Chunks per section for generation. |
| `DRAFT_SECTION_MAX_TOKENS` | `1200` | Section generation cap. |
| `FEW_SHOT_TOP_K` | `3` | Few-shot examples injected per field/section. |
| `RULE_EXTRACTOR_INTERVAL_HOURS` | `6` | APScheduler cadence for the rule extractor. |
| `RULE_EXTRACTOR_MIN_EDITS` | `3` | Minimum edits in a group before a rule is attempted. |
| `RULE_EXTRACTOR_SIMILARITY_THRESHOLD` | `0.88` | Cosine threshold for "this rule already exists". |

See `app/settings.py` for the full list.

---

## LLM router — how to build, configure, and extend

`app/llm/` is the **only** place in the codebase that imports vendor SDKs (`anthropic`, `openai`, `google-genai`, vLLM via HTTP). Every other module — ingest, retrieval, draft, edits — calls `LLMRouter` and never touches a provider directly. Treat this as a hard invariant.

### Capability tiers, not vendor tiers

The router routes by **task** — what the call needs — not by provider. Each tier names an ordered list of providers; failure cascades through them.

| Tier | Used for | Default local model | Default hosted fallback |
|---|---|---|---|
| `extraction` | Structured JSON, field extraction, classification | Qwen 2.5 14B Instruct (4-bit) | `claude-haiku-4-5` |
| `generation` | Section drafting, prose with citations | Qwen 2.5 32B Instruct (AWQ) | `claude-sonnet-4-5` |
| `validation` | Claim ↔ source support classification | Qwen 2.5 7B Instruct | `claude-haiku-4-5` |
| `vision` | Last-resort OCR / handwritten pages | _(no local default)_ | `claude-sonnet-4-5` vision |
| `analysis` | Offline edit-pattern rule extraction | Qwen 2.5 32B Instruct | `claude-sonnet-4-5` |

A `task` tier ties to a list of providers in priority order. The eval harness or any call site can override the model for a single call via `model_override=`.

### Provider interface

Every provider implements the same protocol (`app/llm/providers/base.py`):

```python
class LLMProvider(Protocol):
    name: str
    capabilities: set[Literal["text", "json", "tools", "vision", "streaming"]]

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse: ...

    async def health(self) -> bool: ...

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float: ...
```

Shipped implementations: `VLLMProvider`, `AnthropicProvider`, `OpenAIProvider`, `GeminiProvider`, `MockProvider`. Each is a single small file in `app/llm/providers/`.

### Configuring providers and tiers — `config/router.yaml`

```yaml
default_locale: local            # or "hosted" or "mixed"

tiers:
  extraction:
    providers:
      - vllm_qwen25_14b          # try local first
      - anthropic_haiku          # then fall over to hosted
  generation:
    providers:
      - vllm_qwen25_32b
      - anthropic_sonnet
      - openai_gpt41
  validation:
    providers:
      - vllm_qwen25_7b
      - anthropic_haiku
  vision:
    providers:
      - anthropic_sonnet_vision  # no good local default
      - openai_gpt41_vision
  analysis:
    providers:
      - vllm_qwen25_32b
      - anthropic_sonnet

providers:
  vllm_qwen25_14b:
    type: vllm
    base_url: http://vllm:8001/v1
    model: Qwen/Qwen2.5-14B-Instruct
    timeout_s: 60
    input_cost_per_1k: 0.0
    output_cost_per_1k: 0.0
  anthropic_haiku:
    type: anthropic
    model: claude-haiku-4-5
    api_key_env: ANTHROPIC_API_KEY
    input_cost_per_1k: 0.0008
    output_cost_per_1k: 0.004
  anthropic_sonnet:
    type: anthropic
    model: claude-sonnet-4-5
    api_key_env: ANTHROPIC_API_KEY
    input_cost_per_1k: 0.003
    output_cost_per_1k: 0.015
  openai_gpt41:
    type: openai
    model: gpt-4.1
    api_key_env: OPENAI_API_KEY
  gemini_pro:
    type: gemini
    model: gemini-2.5-pro
    api_key_env: GEMINI_API_KEY

cache:
  enabled: true
  ttl_hours: 24
  backend: postgres

budget:
  hourly_usd: 5.0                # NN-6 global cap
```

**To switch to a hosted-first deployment**, reorder the provider list inside each tier and restart. No code change.

### Caching

The router caches responses in `llm_log.llm_cache`. The cache key is content-addressed:

```
sha256(model_id + messages + schema + sampling_params)
```

Same inputs → same outputs for the same model and prompt. Re-running the eval suite or regenerating a draft after a UI bug fix does not burn provider credits. Cache TTL is configurable; rule-of-thumb is 24 h.

### Budget

Two enforced caps live in `app/llm/budget.py`:

- **Per-document VLM cap.** Before any VLM OCR call, the router checks the document's running VLM page count against `MAX_VLM_PAGES_PER_DOC`. Over → mark the page `vlm_budget_exceeded`, skip the call, surface a UI warning.
- **Global rolling-window spend cap.** The router tracks USD spend in a sliding 1-hour window. Over `LLM_HOURLY_BUDGET_USD` → return `BudgetExceededError` for any escalation tier. The cheap local path is unaffected.

Live spend and per-tier cost are exposed at `GET /admin/llm-stats`.

### Failover semantics

| Case | Strategy |
|---|---|
| Provider rate-limits | Exponential backoff (up to 3 tries), then fail over within the tier. |
| Provider returns 500 | Fail over immediately, log. |
| Schema violation (invalid JSON) | One retry on the same provider with a stricter reminder, then escalate to the next tier. |
| Timeout | Fail over; mark the original request `timeout` for offline analysis. |
| All providers exhausted | Raise `LLMUnavailableError` with cause chain. Never silently substitute a worse model without logging. |
| API key missing for a provider | Health probe marks it `unavailable` at startup; tier cascades or fails loudly if there is no fallback. |
| Hosted model deprecated | Health probe surfaces a loud config error rather than silent failover. |

### Calling the router

```python
from app.api.deps import get_llm_router

router = get_llm_router()
response = await router.generate(
    messages=[Message(role="user", content="...")],
    task="extraction",
    schema=MyPydanticModel,        # triggers JSON mode / guided decoding
    max_tokens=512,
    temperature=0.1,
)
# response.text, response.json (if schema), response.usage, response.cost_usd, response.cached
```

For embeddings and reranking, route through the same singleton:
```python
vectors = await router.embed(["chunk text", "another chunk"])
order   = await router.rerank(query="...", docs=[...])
```

### vLLM specifics

If you have a GPU, run vLLM as part of the Compose stack and the router will prefer it on every tier whose first provider is `vllm_*`. Recommendations:

- Start vLLM with **`--enable-prefix-caching`** — system-prompt-heavy calls (the draft engine reuses long prompts) see a large speedup.
- Tune **`--max-num-seqs`** to your GPU memory; AWQ-quantised 32B fits a 24 GB card with concurrency 2–4.
- Use the **OpenAI-compatible API** endpoint (vLLM exposes it by default). The `VLLMProvider` is a thin layer over OpenAI's SDK with a different `base_url`.
- vLLM supports **guided decoding** for JSON schemas. The provider passes `response_format` when a Pydantic schema is supplied.
- If you do not have a GPU, simply omit the `vllm` service. The health probe marks `vllm_*` providers unavailable at startup; every tier cascades to its hosted fallback. Zero code change.

A reference vLLM launch command for Qwen 2.5 14B Instruct:

```bash
vllm serve Qwen/Qwen2.5-14B-Instruct \
  --port 8001 \
  --max-model-len 16384 \
  --enable-prefix-caching \
  --max-num-seqs 4 \
  --gpu-memory-utilization 0.9
```

For the 32B model, swap in `Qwen/Qwen2.5-32B-Instruct-AWQ` and lower concurrency.

### Adding a new provider

1. Drop a new file in `app/llm/providers/` (e.g. `mistral.py`).
2. Implement the `LLMProvider` protocol — ~50 lines for a typical SDK wrapper.
3. Register it in `app/llm/providers/__init__.py` (the loader dispatches on `type:` from `config/router.yaml`).
4. Add a provider entry and tier reference in `config/router.yaml`.
5. Restart. No call-site change.

### Adding a new task tier

Tiers are not hardcoded; they are keys in `config/router.yaml`. Add a new tier with its provider order, then call `router.generate(..., task="my_new_tier")` from your service. The router will route, fail over, cache, and budget-track it identically.

### Embeddings and reranking

Both go through the same router pattern with their own provider interfaces:

- `app/llm/embedder.py` wraps `sentence-transformers` for `bge-large-en-v1.5` (local CPU/GPU). An OpenAI-embedder is pluggable via `EMBEDDER_PROVIDER=openai`.
- `app/llm/reranker_model.py` wraps `bge-reranker-base` (CPU, ~200 ms for 50 docs).

The retrieval layer calls `router.embed()` and `router.rerank()` — it never imports `sentence-transformers` directly.

---

## Running services individually

### API only (auto-reload)

```bash
make dev                                  # uvicorn app.main:app --reload
# or
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Worker process

```bash
make worker                               # python -m app.jobs.worker
```

The worker polls the `jobs` table, claims rows with `SELECT ... FOR UPDATE SKIP LOCKED`, dispatches by `kind`, and writes heartbeats. APScheduler inside the worker runs the rule extractor every `RULE_EXTRACTOR_INTERVAL_HOURS` and the reconciliation sweep every 5 minutes.

### vLLM (optional)

vLLM is not part of `docker-compose.yml` by default — start it on the host with the command in [vLLM specifics](#vllm-specifics), or add a `vllm` service to your Compose override file pointing at the same `VLLM_BASE_URL`.

### UI (Next.js 15)

```bash
cd ui
npm install
npm run dev                               # http://localhost:3000
```

The UI talks to the API via `NEXT_PUBLIC_API_URL` (defaults to `http://localhost:8000`). Production build: `npm run build && npm start`.

---

## HTTP API

The API root is `/`. Most routes are nested under `/api`; admin/observability lives under `/admin`.

### Documents (`/api/documents`)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/documents` | Multipart upload — returns `{document_id, status}`. Idempotent on SHA-256. Returns `429` when the job queue is saturated. |
| `GET` | `/api/documents` | Paginated list. |
| `GET` | `/api/documents/{id}` | Document status and metadata. |
| `GET` | `/api/documents/{id}/blocks` | Block tree (sections, paragraphs, tables, lists). |
| `GET` | `/api/documents/{id}/pages/{n}` | Rendered page image for citation highlighting. |
| `GET` | `/api/documents/{id}/events` | Server-Sent Events stream of status transitions. Honours `Last-Event-ID` for replay on reconnect; UI falls back to polling after 30 s disconnect. |

### Drafts (`/api/drafts`)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/drafts` | Generate a draft — body: `{template_id, document_ids}`. |
| `GET` | `/api/drafts/{id}` | Full draft with citations and validation report. |
| `POST` | `/api/drafts/{id}/sections/{name}/regenerate` | Regenerate one section. |
| `POST` | `/api/drafts/{id}/edit` | Save operator edits — body: `{final_output}`. Returns the updated draft and triggers diff + few-shot indexing. |

### Templates

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/templates` | List available templates with current `prompt_fingerprint`. |
| `GET` | `/api/templates/{id}/edit-metrics?days=30` | Edit-rate metrics per field/section. |

### Admin (`/admin`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/llm-stats` | Live LLM cost, latency, error rate, current-hour spend vs budget. |
| `POST` | `/admin/rule-extractor/run` | Synchronously trigger the rule extractor. Idempotent. |
| `GET` | `/admin/templates` | All templates with version history. |
| `GET` | `/admin/templates/{id}/versions` | Version timeline (each entry includes `prompt_fingerprint` and `appended_rules`). |

### Health

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness. |
| `GET` | `/readyz` | Readiness — DB, pgbouncer, and (if configured) vLLM reachability. |

Every request/response carries an `X-Request-ID` header; every log line and LLM request row records the same ID for tracing.

---

## Templates

A `DraftTemplate` is YAML in `config/templates/`. Three ship by default:

| Template | Use |
|---|---|
| `case_fact_summary` | Litigation case fact summary — parties, jurisdiction, claims, damages, procedural history, factual background. |
| `title_review_summary` | Title review — table-heavy, party + property + encumbrance extraction. |
| `document_checklist` | Stub demonstrating extensibility — proves a new draft type is config-only. |

Template shape (abbreviated):

```yaml
id: case_fact_summary
display_name: "Case Fact Summary"
system_prompt: |
  You are a legal analyst extracting and summarizing case facts...
appended_rules: []                    # populated by the rule extractor
retrieval_queries:
  parties: "parties plaintiff defendant names"
  jurisdiction: "jurisdiction venue court district"
  ...
extraction_schema:
  - name: parties
    type: list[party]
    description: "All named parties — plaintiffs and defendants"
    retrieval_key: parties
    required: true
  ...
sections:
  - name: procedural_history
    description: "Chronological summary of case procedural events"
    retrieval_key: procedural_history
    target_length_min: 50
    target_length_max: 200
    validators: [cites_at_least_one]
  ...
validators:
  - id: non_empty
    args: { field: parties }
```

The registry loads YAML on startup, snapshots each template, and computes a `prompt_fingerprint = sha256(system_prompt + appended_rules + extraction_schema + sections)`. The fingerprint — not the integer version — is the cache and edit-log identity key. A rule append without a version bump still changes the fingerprint and busts the cache.

**Adding a template** = a new YAML file. No Python changes.

---

## Database

One Postgres 16 instance, three schemas:

| Schema | Tables | Shape |
|---|---|---|
| `app` | `documents`, `pages`, `blocks`, `chunks`, `drafts`, `edits`, `templates`, `template_versions` | OLTP, transactional. |
| `jobs` | `jobs`, `job_history` | Append-heavy, frequent updates, short-lived rows. |
| `llm_log` | `llm_requests`, `llm_cache` | Append-only log + cache. `llm_requests` is partitioned monthly via `pg_partman`. |

Extensions: `vector`, `pg_trgm`, `pgcrypto`.

The retriever uses a **custom text-search configuration** `legal_en` (a copy of `english` with `simple` dictionary for proper-noun / acronym / statute-citation tokens), plus a `pg_trgm` fuzzy pass and a separate `entities` GIN index for party/statute tokens. This is what makes "Habeas corpus", "Pearson Specter Litt", and "§ 1983" retrievable.

HNSW index parameters are explicit, not defaults:

```sql
CREATE INDEX chunks_embedding_idx ON app.chunks
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
-- query-time:
SET LOCAL hnsw.ef_search = 100;
```

Heavy queries (retrieval, rerank fetch) `SET LOCAL work_mem = '64MB'` and `SET LOCAL statement_timeout = '5s'`.

### Migrations

```bash
make migrate              # alembic upgrade head
make migrate-down         # alembic downgrade -1
make reset-db             # drop, recreate, migrate
```

Alembic is configured to use `DATABASE_URL_DIRECT` (bypassing pgbouncer's transaction pooler, which does not support session-level statements that DDL needs).

---

## Tests and evaluation

### Unit and integration tests

```bash
make test                 # tests/unit  — fast, no external services
make test-integration     # tests/integration — needs a running Postgres
make test-all             # both, with coverage
pytest -k "ingest_idempotency"
```

Markers:

- `@pytest.mark.live` — hits real LLM provider APIs. Skipped unless `--live` is passed.
- `@pytest.mark.slow` — loads large models or runs end-to-end OCR.

### Eval harness

The eval harness is a real artifact, not a notebook. Datasets live in `eval/data/`; reports land in `eval/reports/` (git-ignored).

```bash
make eval                                 # runs everything, writes report.md
python eval/run_retrieval.py              # recall@k, MRR on legal-style queries
python eval/run_citation_validity.py      # % of generated citations that pass the validator
python eval/run_edit_improvement.py       # edit-rate per field, before vs after rule extraction
```

Every eval run writes to the same `llm_log.llm_requests` table the live system uses — so cost, latency, and cache hit rate are measured consistently.

---

## Operational notes

- **Ingestion is recoverable.** The job queue is persistent; the worker writes heartbeats every 10 s; a startup reconciler reclaims jobs whose heartbeat is older than `JOB_STALE_TIMEOUT`. Stuck or partial documents get re-queued at their missing stage. Retrieval refuses to return chunks from documents whose `status != 'ready'`.
- **Idempotency is atomic.** `POST /api/documents` does a single `INSERT ... ON CONFLICT (sha256) DO UPDATE ... RETURNING (xmax = 0) AS inserted` — no application-side read-then-write race.
- **Backpressure is explicit.** When the pending+running job count crosses `JOB_QUEUE_MAX_PENDING`, uploads return `429 Too Many Requests` with a `Retry-After` header. Loud, not silent.
- **Errors are typed.** Every domain error subclasses `AppError` with an error code; the FastAPI handler renders a uniform JSON shape. No bare 500 with an HTML page.
- **Logs are JSON.** structlog writes to stdout; every line includes `ts`, `level`, `request_id`, `event`, and event-specific fields. LLM calls additionally log to `llm_log.llm_requests` with model, tokens, cost, latency, status, cache hit.
- **SSE has a polling fallback.** Every SSE event carries an `id`; the endpoint honours `Last-Event-ID` on reconnect; the UI falls back to `GET /api/documents/{id}` polling at 5 s intervals after a 30 s disconnect.
- **Rule extractor is triggered, not hopeful.** APScheduler runs it every `RULE_EXTRACTOR_INTERVAL_HOURS`; the admin UI exposes a manual trigger; the extractor is idempotent and skips rules whose cosine similarity to existing rules exceeds `RULE_EXTRACTOR_SIMILARITY_THRESHOLD`.
- **Few-shot embeddings have a safety net.** Edits enqueue an index job on save; a reconciliation sweep retries any row with `few_shot_indexed_at IS NULL AND created_at < NOW() - INTERVAL '1 minute'` using exponential backoff.

---

## License

Internal demo project. License terms TBD.
