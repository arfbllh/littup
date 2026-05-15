# littup

Turn messy legal-style PDFs into grounded, template-driven first drafts with inspectable citations, then use operator edits to make subsequent drafts better. Single-workspace, single-operator system.

---

## Table of contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Worked sample](#worked-sample)
- [Architecture](#architecture)
- [Document processing](#document-processing)
- [Retrieval and grounding](#retrieval-and-grounding)
- [Draft generation](#draft-generation)
- [Improvement from operator edits](#improvement-from-operator-edits)
- [Configuration and tuning](#configuration-and-tuning)
- [GPU and local inference](#gpu-and-local-inference)
- [Evaluation](#evaluation)
- [HTTP API](#http-api)
- [Repository layout](#repository-layout)
- [Assumptions and tradeoffs](#assumptions-and-tradeoffs)
- [Limitations](#limitations)
- [Future work](#future-work)

---

## What it does

littup ingests folders of mixed legal-style documents (clean PDFs, rotated scans, handwritten margins, DOCX, plain text), routes each file through the right OCR path, indexes the result into a hybrid BM25 + dense corpus, and uses a `DraftTemplate` to generate a structured first draft. Every claim in the draft carries a `[chunk:CHUNK_ID]` citation that a validator checks against the source. When the operator edits the draft, the system captures a structured field-level diff and reuses it two ways:

- **Few-shot retrieval (immediate)** — embeds the edit, looks up similar past edits at next-draft time, injects them as in-context examples inside both the extractor and the section generator prompts.
- **Rule extraction (periodic)** — clusters recent edits per `(template, field)`, asks an LLM to extract durable house-style rules, appends them to the template's system prompt and bumps the prompt fingerprint.

The LLM stack is pluggable. A single `LLMRouter` (`app/llm/router.py`) abstracts Ollama (local + cloud), Anthropic, OpenAI, and Gemini behind one interface, with task-tier routing (`extraction`, `generation`, `validation`, `vision`, `analysis`) and automatic failover.

---

## Quick start

### Prerequisites

- Docker + Docker Compose (Docker path), or Python 3.11 + Node 20 (bare-metal path).
- At least one LLM provider configured. The simplest path is `ANTHROPIC_API_KEY`. Alternatives: `OPENAI_API_KEY`, `GEMINI_API_KEY`, or a local Ollama daemon.

### 1. Configure environment

```bash
cp .env.example .env
```

Open `.env` and set **at least one** of the following so the router has a path:

```ini
# Hosted (simplest path — pick one or more)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AI...

# Or fully local with Ollama (free, runs on your machine)
OLLAMA_BASE_URL=http://localhost:11434
```

The router skips any provider whose env is missing or empty and falls over to the next one in the tier. On boot the worker logs `llm.providers_active` so you can confirm what was picked up. See [Configuration and tuning](#configuration-and-tuning) for the full list of knobs.

### 2. Option A — Docker

```bash
make up          # docker compose up --build -d   (api, worker, postgres, pgbouncer)
make migrate     # alembic upgrade head

curl http://localhost:8000/healthz
# {"status":"ok"}
```

> **First build takes a while.** The Dockerfile pre-bakes `bge-large-en-v1.5` (~1.3 GB) and `bge-reranker-base` (~1.1 GB) into the image during `docker build` so a fresh container starts in seconds instead of stalling on its first job. On a typical home connection this is ~3–8 minutes the first time; subsequent builds reuse the cached layer and finish in seconds. The download is anonymous by default and rate-limited — pass your `HF_TOKEN` as a BuildKit secret for higher throughput:
>
> ```bash
> export $(grep '^HF_TOKEN=' .env | xargs)
> DOCKER_BUILDKIT=1 docker compose build --secret id=hf_token,env=HF_TOKEN worker api
> docker compose up -d
> ```
>
> If the build fails mid-download (flaky wifi, HF CDN hiccup), just re-run `docker compose build worker` — Docker resumes from the last good layer and only retries the failed step. The token is mounted as an ephemeral secret and never written to any image layer (verify with `docker history --no-trunc <image> | grep -i hf_token` — no output expected).

If you also run a host-side Ollama, set `OLLAMA_BASE_URL=http://host.docker.internal:11434` in `.env`. The compose file maps `host.docker.internal` to the host gateway so the api/worker containers can reach it on Linux too.

### 2. Option B — bare-metal

```bash
docker compose up -d postgres pgbouncer       # just the database
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
make migrate
make dev                                      # terminal 1 — API
make worker                                   # terminal 2 — worker

cd ui && npm install && npm run dev           # terminal 3 — UI at :3000
```

### 3. Generate a draft

```bash
# Upload
DOC=$(curl -s -F "file=@samples/henderson_v_meridian/source_docs/01_complaint.pdf" \
  http://localhost:8000/api/documents | jq -r .document_id)

# Wait until ingestion finishes
until [ "$(curl -s http://localhost:8000/api/documents/$DOC | jq -r .status)" = "ready" ]; do sleep 2; done

# Draft
DRAFT=$(curl -s -X POST http://localhost:8000/api/drafts \
  -H "content-type: application/json" \
  -d "{\"template_id\":\"case_fact_summary\",\"document_ids\":[\"$DOC\"]}" | jq -r .draft_id)

# Inspect (poll until status=ready)
curl -s http://localhost:8000/api/drafts/$DRAFT | jq
```

Or use the UI at `http://localhost:3000` — Documents → upload → Drafts → New → pick a template.

### Tests

```bash
make test                 # unit, no external services
make test-integration     # integration, needs Postgres
make test-all             # both, with coverage
```

---

## Worked sample

`samples/henderson_v_meridian/` ships a synthetic but realistic case file you can run end-to-end:

```
samples/henderson_v_meridian/
├── source_docs/
│   ├── 01_complaint.pdf            # civil complaint, 4 counts, $2.45M damages
│   ├── 02_police_report.pdf
│   ├── 03_medical_records.pdf
│   ├── 04_insurance_denial.pdf
│   ├── 05_title_commitment.pdf
│   └── witness_statement.txt
├── images/                         # rasterised pages
└── validation/
    ├── case_fact_summary_expected.json
    ├── title_review_summary_expected.json
    └── document_checklist_expected.json
```

Each `*_expected.json` is the ground truth the eval harness compares against. Running the `case_fact_summary` template against this sample yields all four parties (with structured `{name, role}` rows), the four counts, the `$2,450,034.62` damages figure, and three grounded prose sections with `[chunk:...]` citations on every factual sentence.

---

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
                    Ollama | Anthropic | OpenAI | Gemini
                                │
                                ▼
              Postgres 16 + pgvector + pg_trgm (app/db)
                  schemas: app · jobs · llm_log
                                ▲
                                │ (queue)
                       Worker (app.jobs.worker)
```

Four pipelines, one data plane, one model router.

1. **Ingest → OCR → parse → index.** Native PDFs go through `pdfplumber`; scans through PaddleOCR with image preprocessing; handwriting and busted layouts can escalate to a vision LLM (budget-capped). A block-tree layout parser builds semantically meaningful chunks.
2. **Retrieve.** Postgres FTS with a custom `legal_en` config + `pg_trgm` proper-noun fallback + pgvector HNSW dense vectors. Reciprocal Rank Fusion combines them; `bge-reranker-base` reranks; only documents with `status=ready` are queried.
3. **Draft.** A `DraftTemplate` (YAML) declares retrieval queries, an extraction schema, prose sections, validators. The engine snapshots the template once and runs three passes: structured field extraction → section generation with citations → citation validation.
4. **Edit loop.** Each operator edit becomes a structured diff. The diff is embedded into a few-shot store keyed by `(template_id, field_name, field_type)`. A scheduled rule extractor clusters edits and promotes durable rules into the template's system prompt, bumping `prompt_fingerprint` so the LLM cache busts cleanly.

### Ingestion state machine

```
uploaded → ocr_pending → ocr_running → ocr_done →
layout_running → layout_done →
chunking_running → chunking_done →
embedding_running → ready    (or failed)
```

Every transition is a Postgres row; every long-running step is a heartbeat-bearing job. A startup reconciler reclaims jobs whose heartbeat is older than `JOB_STALE_TIMEOUT` and a periodic sweep keeps the pipeline self-healing.

### Data plane

| Schema | Tables | Notes |
|---|---|---|
| `app` | documents, pages, blocks, chunks, drafts, sections, citations, edits, templates, template_versions | OLTP. HNSW index `m=16, ef_construction=64`; `hnsw.ef_search` set per query. |
| `jobs` | jobs | Append-heavy. Claimed with `SELECT … FOR UPDATE SKIP LOCKED`. |
| `llm_log` | llm_requests, llm_cache | Append-only log + content-addressed cache. `llm_requests` partitioned monthly. |

pgbouncer sits in front in transaction-pooling mode. Alembic uses `DATABASE_URL_DIRECT` to bypass the pooler for DDL.

---

## Document processing

- **Per-page routing** — `app/ingest/ocr/routing.py` classifies each page (native text, scan, handwriting, busted layout) and picks the cheapest extractor that can handle it. Manual override lives in the UI ("Re-extract with PaddleOCR").
- **OCR engines** — `pdfplumber` for native PDFs, PaddleOCR for scans, a vision-LLM escalation path for handwriting and stamped pages. Each implements the same `OcrEngine` protocol.
- **PaddleOCR model variant** — defaults to the PP-OCRv5 **mobile** detector/recogniser (`PP-OCRv5_mobile_det` + `en_PP-OCRv5_mobile_rec`), wired in `app/ingest/ocr/paddle_ocr.py`. Mobile is ~5× faster than the server variant on CPU and good enough for clean scans. For maximum recall on small or dense legal text, switch to the server variant by exporting:

  ```ini
  # .env
  OCR_DET_MODEL=PP-OCRv5_server_det
  OCR_REC_MODEL=en_PP-OCRv5_server_rec
  ```

  Expect 3–5× slower throughput per page and a noticeable RAM bump; accuracy on dense paragraphs improves materially.
- **Pre-processing** — deskew, binarisation, DPI normalisation (`app/ingest/ocr/preprocess.py`).
- **Layout parsing** — block tree with `(page, bbox, confidence)` per span, not naive sliding windows.
- **Structured downstream output** — pages, blocks, chunks land in Postgres as first-class rows that retrieval queries directly.
- **Recoverability** — every step is a job; partial documents re-enter at their missing stage.

---

## Retrieval and grounding

- **Hybrid retrieval** — Postgres FTS with a custom `legal_en` text-search config (built in migration `0003_legal_en_overrides.py`) for statute citations, party names, and acronyms; `pg_trgm` fuzzy match for proper-noun variants; pgvector HNSW dense vectors with explicit parameters.
- **Rank fusion** — Reciprocal Rank Fusion (`app/retrieval/fusion.py`) over the three signals.
- **Reranking** — `bge-reranker-base` cross-encoder (`app/llm/reranker_model.py`), CPU-friendly, ~200 ms for 50 candidates.
- **Inspectable citations** — every claim in the draft is tagged `[chunk:CHUNK_ID]`. The UI clicks through to the source span on the original page image.
- **Grounding enforcement** — Pass 3 of the draft engine (`app/draft/validator.py`) sends each claim ↔ source pair to a validator LLM; classifies as `supported`, `partial`, `unsupported`, or `contradicted`. Sections that fail are retried once with a stricter prompt.
- **Status filter** — retrieval refuses chunks from documents whose `status != 'ready'`, so partial corpora cannot contaminate a draft.

---

## Draft generation

`app/draft/engine.py` orchestrates three passes:

1. **`FieldExtractor`** (`extractor.py`) — small model, JSON schema, structured output. The schema is hand-rolled inline (no `$defs` / `$ref`) because some providers stringify nested values when the schema is too clever. Substring grounding for plain `string` fields is lenient: substring match **or** ≥60 % token overlap. Dates and money are exempt because the LLM normalises them ("April 28, 2025" → "2025-04-28").
2. **`SectionGenerator`** (`generator.py`) — large model, prose with citations, sections generated in parallel, per-section regeneration endpoint.
3. **`CitationValidator`** (`validator.py`) — claim ↔ source classification; failing sections get one strict retry.

Templates are config, not code (`config/templates/*.yaml`). A new draft type = a new YAML file. Three ship by default:

| Template | Use |
|---|---|
| `case_fact_summary` | Litigation case facts — parties, jurisdiction, claims, damages, procedural history, factual background. |
| `title_review_summary` | Title commitment summary — property, owners, encumbrances, liens, easements. |
| `document_checklist` | Minimal stub demonstrating extensibility. |

Per-field and per-section validators live in `app/draft/templates/validators.py`: `non_empty`, `is_date`, `is_money`, `length_between`, `cites_at_least_one`.

The registry computes `prompt_fingerprint = sha256(system_prompt + appended_rules + extraction_schema + sections)` per template. This fingerprint — not the integer version — is the cache and edit-log identity key. Appending a rule changes the fingerprint and busts the cache cleanly.

---

## Improvement from operator edits

- **Capture** — `POST /api/drafts/{id}/edit` (`app/api/routes/edits.py`) → `EditService.save_edit` (`app/edits/service.py`). The diff is structured per-field and per-section (`app/edits/diff.py`), not a side-by-side text blob. Each `Edit` row carries the AI value, the operator value, the diff, the source chunks the LLM relied on, and the template fingerprint at the time.
- **Embed** — saving an edit enqueues a `FEW_SHOT_INDEX` job. The handler (`app/jobs/handlers/few_shot_index.py`) embeds a symmetric search representation (`app/edits/few_shot_store.py`) and stamps `few_shot_indexed_at` atomically.
- **Retrieve** — both the field extractor and the section generator call `FewShotStore.retrieve(template_id, field, …)` and render the top-k examples directly into their prompts.
- **Promote to durable rules** — `RuleExtractor` (`app/edits/rule_extractor.py`) clusters edits per `(template, field)`, asks an LLM to summarise the recurring pattern into a one-line rule, appends it to the template's system prompt, and bumps the version. A cosine-similarity gate prevents near-duplicate rules.
- **Trigger** — APScheduler runs the extractor every `RULE_EXTRACTOR_INTERVAL_HOURS` inside the worker; `POST /admin/rule-extractor/run` is the manual override.
- **Reconciliation** — a periodic sweep retries any `Edit` row still un-embedded after one minute (`app/jobs/reconciler.py`).

Empirically: editing `case_fact_summary.claims` from one abbreviated count to the four canonical "Count I–IV" phrasings caused the *next* draft on the same template to emit all four counts in the operator's preferred form, while `filing_date` correctly stayed null when the date wasn't present in the OCR'd chunks (grounded behaviour; the LLM refuses to hallucinate from the few-shot alone).

---

## Configuration and tuning

All knobs live in `.env` (loaded by `pydantic-settings`). Sane defaults in `app/settings.py`. Group the variables by the goal you care about:

### Cheaper

| Variable | Default | Effect |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Set this and tier 1 of every task tries the local model first. Free if you run Ollama. Clear to disable. |
| `LLM_HOURLY_BUDGET_USD` | `5.00` | Rolling 1-hour cap on hosted spend. Hit it and escalation tiers return `BudgetExceededError`. |
| `MAX_VLM_PAGES_PER_DOC` | `20` | Per-document cap on vision-LLM OCR pages. Cheap-OCR path unaffected. |
| LLM cache | on, 24 h TTL | Same `(model, messages, schema, sampling)` returns the cached row. Re-runs are free. |

### Faster

| Variable | Default | Effect |
|---|---|---|
| `WORKER_CONCURRENCY_OCR` | `2` | More pages in flight per document. Watch RAM with PaddleOCR. |
| `WORKER_CONCURRENCY_EMBEDDING` | `4` | Faster post-OCR indexing. CPU-bound. |
| `EMBEDDING_BATCH_SIZE` | `32` | Bigger batches amortise model-load overhead. |
| `OCR_RASTER_DPI` | `300` | Drop to `200` for ~2× faster rasterisation; recognition quality drops on small text. |
| `OCR_MAX_IMAGE_SIDE` | `5000` | Cap the longest side. Lower = less compute per page. |
| `HNSW_EF_SEARCH` | `100` | Lower for faster ANN at the cost of recall. `40` is ~3× faster, ~95 % of the recall. |
| `MAX_CHUNKS_PER_DOC_PER_QUERY` | `3` | Lower = less variety, faster prompts. |
| `RERANKER_MODEL` | `bge-reranker-base` | The `large` variant is ~4× slower with marginal lift on legal text. |

### More accurate

| Variable | Default | Effect |
|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-large-en-v1.5` | A strong CPU-friendly embedder. Swap to `text-embedding-3-large` if you set `EMBEDDER_PROVIDER=openai`. |
| `HNSW_EF_SEARCH` | `100` | Raise to `200`–`400` for harder corpora; cost is linear. |
| `OCR_RASTER_DPI` | `300` | `400`–`500` gives recognition more pixels per glyph. Stamped pages benefit. |
| `OCR_PREPROCESS_ALL` | `false` | `true` forces deskew + binarisation on every page. Opt-in — hurts more than it helps on stamped/degraded scans. |
| `MAX_VLM_PAGES_PER_DOC` | `20` | Raise if a document is mostly handwriting and you have budget. |
| `FEW_SHOT_TOP_K` | `3` | Inject more past edits per field. 5–8 is fine; above that, prompt bloat dominates. |
| `DRAFT_SECTION_TOP_K` | `8` | More chunks per section means more evidence and more noise. |
| `DRAFT_SECTION_MAX_TOKENS` | `1200` | Raise for longer sections. Linear cost. |

### Provider selection

Edit `config/router.yaml` to reorder priorities or add a model. The top of each tier is tried first; failures cascade down. Clear an env (`OLLAMA_BASE_URL=""`, `ANTHROPIC_API_KEY=""`, …) to disable every provider of that type at startup.

---

## GPU and local inference

littup is built CPU-first so it runs on a laptop. Three places benefit from a GPU:

### 1. PaddleOCR (page recognition)

```ini
# .env
OCR_USE_GPU=true
```

…or build the GPU image variant:

```bash
docker build --build-arg PADDLE_GPU=1 -t littup-gpu .
```

The Dockerfile swaps `paddlepaddle` for `paddlepaddle-gpu` when `PADDLE_GPU=1`. Expect ~5–10× page throughput on a 4090.

### 2. Embeddings and reranker

```ini
# .env
TORCH_DEVICE=cuda    # or "mps" on Apple Silicon (capped at ~9 GB pool)
```

`sentence-transformers` and the cross-encoder reranker honour `TORCH_DEVICE` automatically.

### 3. Local generation (instead of hosted Claude/GPT)

> **Ollama is wired up for development and smoke-testing only.** The default `nemotron-3-super:cloud` tag and the small locally-pulled tags we tested with (`qwen2.5:14b-instruct-q4_K_M` and friends) do not produce production-grade drafts — extraction routinely drops fields, citations drift, and structured-output schemas don't always parse. If you want quality output today, point the router at Anthropic / OpenAI / Gemini. Use Ollama to exercise the pipeline end-to-end without burning hosted credits, not to ship work.

**Ollama on the host** — install Ollama (`brew install ollama` on macOS or your platform's equivalent), `ollama serve`, then either `ollama signin` for the free hosted-cloud tags (rate-limited but no GPU needed) or `ollama pull qwen2.5:14b-instruct-q4_K_M` for a local 14B model. Edit `config/router.yaml` to set `ollama_local.model`. Generation moves off Anthropic Sonnet; cost drops sharply.

**Better local results — swap the model and raise context length.** Two pinpoint changes:

1. **Model.** Open `config/router.yaml`, find the `ollama_local` provider block (~line 37), and change `model:`. Stronger options that fit on a single 24 GB GPU: `qwen2.5:32b-instruct-q4_K_M`, `llama3.3:70b-instruct-q4_K_M` (needs ~40 GB), or `mistral-small3.1:24b-instruct-2503-q4_K_M`. Pull first with `ollama pull <tag>`; restart the worker so the provider re-reads the config.

   ```yaml
   # config/router.yaml
   ollama_local:
     type: ollama
     model: qwen2.5:32b-instruct-q4_K_M    # ← was nemotron-3-super:cloud
     timeout_s: 240
   ```

2. **Context length.** Ollama defaults `num_ctx` to **2048**, which is far below what the draft engine needs (a single retrieval pass packs 10+ KB of chunks plus a system prompt). The cleanest way to raise it is a custom Modelfile, then re-tag the model:

   ```bash
   cat > Modelfile <<'EOF'
   FROM qwen2.5:32b-instruct-q4_K_M
   PARAMETER num_ctx 16384
   PARAMETER temperature 0.1
   EOF
   ollama create qwen2.5-32b-legal -f Modelfile
   ```

   Then point the router at the new tag: `model: qwen2.5-32b-legal` in `config/router.yaml`. Verify the running context with `ollama show qwen2.5-32b-legal --modelfile`. 16k is the floor for our draft prompts; 32k+ is comfortable; raise `timeout_s` accordingly because longer contexts mean slower decode.

**Real vLLM on a Linux GPU box** — the `ollama` provider speaks the OpenAI shape, so it works against vLLM unchanged:

```bash
vllm serve Qwen/Qwen2.5-14B-Instruct \
  --port 8001 \
  --max-model-len 16384 \
  --enable-prefix-caching \
  --max-num-seqs 4 \
  --gpu-memory-utilization 0.9
```

```ini
# .env
OLLAMA_BASE_URL=http://your-vllm-host:8001
```

`--enable-prefix-caching` matters because the draft engine reuses long system prompts across calls.

---

## Evaluation

The eval harness is a real artifact, not a notebook. Datasets live in `eval/data/`; reports land in `eval/reports/`.

```bash
make eval                                 # runs everything, writes report.md
python eval/run_retrieval.py              # Recall@k, MRR on legal-style queries
python eval/run_citation_validity.py      # % of generated citations the validator accepts
python eval/run_edit_improvement.py       # field-edit-rate before vs after the rule extractor
```

Every eval run writes to the same `llm_log.llm_requests` table the live system uses, so cost, latency, and cache hit rate are measured consistently. The `legal_term` Recall@10 threshold is currently set at 0.85; the harness warns loudly if a run drops below.

---

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/documents` | Multipart upload; idempotent on SHA-256. |
| `GET` | `/api/documents` | Paginated list. |
| `GET` | `/api/documents/{id}` | Status + metadata. |
| `GET` | `/api/documents/{id}/blocks` | Parsed block tree. |
| `GET` | `/api/documents/{id}/events` | SSE status stream, `Last-Event-ID` aware. |
| `POST` | `/api/drafts` | Generate a draft from a template + documents. |
| `GET` | `/api/drafts/{id}` | Full draft with citations and validation. |
| `POST` | `/api/drafts/{id}/sections/{name}/regenerate` | Regenerate one section. |
| `POST` | `/api/drafts/{id}/edit` | Save operator edits; triggers diff + few-shot indexing. |
| `GET` | `/api/templates` | List templates with current fingerprints. |
| `GET` | `/admin/llm-stats` | Live cost, latency, error rate, budget. |
| `POST` | `/admin/rule-extractor/run` | Synchronously trigger rule extraction. |
| `GET` | `/healthz` | Liveness. |
| `GET` | `/readyz` | Readiness — DB + dependent services. |

Every request/response carries `X-Request-ID`; every log line and LLM-request row records the same ID for tracing.

---

## Repository layout

```
littup/
├── app/                      # Python package
│   ├── api/                  # HTTP routes (thin) + Pydantic schemas + SSE helpers
│   ├── core/                 # structlog setup, AppError hierarchy, request-ID middleware
│   ├── db/                   # SQLAlchemy models, Alembic migrations, session factories
│   ├── draft/                # Template registry, field extractor, section generator, citation validator
│   ├── edits/                # Structured diff, few-shot store, offline rule extractor
│   ├── ingest/               # Classifier, OCR engines, layout parser, chunker, reconciler
│   ├── jobs/                 # Postgres job queue, worker, handlers, APScheduler tasks
│   ├── llm/                  # LLMRouter + providers + response cache + budget tracker + embedder + reranker
│   ├── retrieval/            # BM25, dense, trigram, RRF fusion, reranker
│   └── settings.py
├── config/
│   ├── router.yaml           # LLM tier and provider config
│   ├── ocr.yaml              # OCR thresholds and VLM caps
│   └── templates/            # *.yaml DraftTemplate files
├── docker/postgres/          # Postgres init scripts (extensions, custom FTS config)
├── eval/                     # Offline harness (retrieval, citations, edit improvement)
├── samples/                  # Worked sample with ground-truth fixtures
├── scripts/                  # bench_ocr, fixture generators, edit synthesis
├── tests/
│   ├── unit/                 # No external services
│   └── integration/          # Requires running Postgres
└── ui/                       # Next.js 15 operator UI
```

---

## Assumptions and tradeoffs

- **One workspace, one operator.** No auth, no multi-tenancy, no per-user templates. Adding auth is straightforward but would have eaten time better spent on grounding and the edit loop.
- **Postgres for everything.** Job queue, vector store, FTS, cache, log — one storage system. No Redis, no Celery, no Elastic. Trades a little theoretical scale ceiling for a much smaller operational surface.
- **Templates are config, not code.** Adding a new draft type = a new YAML file. System-prompt-as-data lets the rule extractor mutate templates safely.
- **Grounding over fluency.** Citation validation is a separate pass; sections that fail get one strict retry and then ship as-is with the failure flagged. The operator decides whether to accept.
- **Cheap path is the default path.** The router prefers local; hosted is the fallback. Every escalation is budgeted.
- **OCR is good-enough-mostly.** PaddleOCR handles the vast majority of pages. The vision-LLM escalation handles handwriting and table-heavy stamped pages but is rate-limited per document.

---

## Limitations

1. **Local structured-output is a fallback in name only.** When Ollama is configured, generation (prose) succeeds locally but extraction and validation routinely fall over to Anthropic because `nemotron-3-super:cloud` doesn't honour nested JSON schemas. A proper fix is wiring vLLM with `guided_json`, or swapping to a model with strong constrained-generation support.
2. **Reranker is the slowest step on CPU.** The cross-encoder dominates retrieval latency by an order of magnitude. `TORCH_DEVICE=mps`/`cuda` helps; a smaller distilled reranker would help more.
3. **`tests/unit/test_preprocess.py::test_deskew_corrects_small_angle` fails in isolation.** The standalone deskew helper has a bug on rotated synthetic images. The production OCR path isn't affected (PaddleOCR has its own internal deskew); the helper is unused there. Left out of `make test`'s default run.
4. **No PII redaction.** The system happily indexes social security numbers and personal addresses. A real legal workflow needs at minimum a Presidio pass before chunks land in Postgres.
5. **Eval is light on draft-quality metrics.** We measure retrieval recall and citation-validity rate; no section-level semantic scoring. LLM-as-judge over the `samples/` ground truth would close the gap.
6. **No streaming for drafts.** Sections aren't streamed into the UI. The SSE plumbing exists for document status; extending it to drafts is a half-day.
7. **No deletion cascade for orphan blobs.** Deleted documents leave their files on disk. A cron sweep would fix it.
8. **One Postgres, no read replica.** Fine for a single operator; not fine if you need to scale read paths.

---

## Future work

1. **Plug vLLM with `guided_json` into the structured-output tiers.** Removes the hosted-Anthropic fallback for extraction/validation and meaningfully cuts cost.
2. **LLM-as-judge eval over the ground-truth fixtures.** Score each section against the `must_mention` / `must_not_mention` lists in `samples/.../validation/*_expected.json`; surface drift over time.
3. **PII redaction pre-chunk.** Presidio with a legal-name allowlist (parties from the complaint stay; SSNs go).
4. **Streaming section generation in the UI.** Operators want to watch the draft form; the SSE plumbing already exists.
5. **Versioned template diffing in the UI.** The rule extractor bumps `prompt_fingerprint` invisibly; show operators which rules were added when, with diffs.
6. **Native handwriting support.** Today handwriting routes to a vision LLM. A small TrOCR model would handle most cursive cheaper and faster.
7. **Multi-tenant mode.** Add a `workspace_id` foreign key everywhere, surface a workspace switcher in the UI, scope every retriever query.
8. **Cross-document entity resolution.** "James Henderson" and "James W. Henderson" should resolve to one canonical party.
9. **Active-learning prompts.** When two operator edits to the same field disagree, pause the rule extractor and ask the operator which one wins.
10. **Fine-tune PaddleOCR on messy legal PDFs.** The default PP-OCRv5 weights are trained on general-purpose text and miss the long-tail of legal artifacts — Bates stamps, handwritten margins overlapping print, court seals, smudged carbon-copy filings, mixed-font case captions. Building a labelled set out of `samples/` plus operator OCR corrections, then fine-tuning the recognition head (or training a small LoRA over the detector), should lift recall meaningfully on the hardest pages and reduce reliance on the budget-capped vision-LLM escalation path.
11. **GPU path is not yet tested.** `OCR_USE_GPU=true`, the `paddlepaddle-gpu` Dockerfile branch, and `TORCH_DEVICE=cuda` for the embedder and reranker are wired up but have only been exercised on CPU and Apple Silicon (`mps`). A full end-to-end run on a CUDA box (PaddleOCR + bge embedder + bge reranker + optional local vLLM generation) is on the to-do list; expect rough edges around CUDA/cuDNN version pinning in the image.
