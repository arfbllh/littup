# Running littup locally (Mac M1 / dev workflow)

Step-by-step. Run each block in its own terminal. Numbered = order; comments are optional context.

---

## One-time setup

```bash
# 1. Python virtualenv + deps
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. UI deps
cd ui && npm install && cd ..

# 3. Environment file (copy/edit as needed)
cp .env.example .env   # if you have one; otherwise edit .env directly
```

---

## Daily startup — split-stack (recommended on M1)

> Postgres/pgbouncer in Docker; api + worker + UI run on the host so reload works
> and the M1 doesn't try to run two Python copies of BGE inside containers.

### Terminal 1 — infra (Postgres + pgbouncer in Docker)

```bash
# Start only the data plane. The api and worker compose services stay down
# because we'll run them on the host.
docker compose up -d postgres pgbouncer

# Apply migrations (uses DATABASE_URL_DIRECT, bypasses pgbouncer)
make migrate
```

### Terminal 2 — API server (FastAPI, hot reload)

```bash
source .venv/bin/activate
make dev
# uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Terminal 3 — background worker (jobs, draft generation, OCR, embeddings)

```bash
source .venv/bin/activate
make worker
# python -m app.jobs.worker
```

### Terminal 4 — Next.js UI

```bash
cd ui
npm run dev
# Next dev server on http://localhost:3000
```

---

## Why this split?

- `make up` (full Docker) duplicates Python deps inside the `api` and `worker`
  containers — each loads BGE-large + reranker into its own RAM. On 8 GB M1 this
  is the OOM path. Running api+worker on the host shares the one Python process
  set with macOS unified memory.
- The compose `api` service binds port 8000; if it's running, `make dev`
  will fail with "address already in use." Same for the `worker` container —
  it will compete with `make worker` for the same job queue (both will try to
  claim jobs, leading to confusing behavior).

---

## Common operations

```bash
# Reset DB (drops + recreates littup, re-runs migrations)
make reset-db

# Seed fixture docs (requires api + worker running)
make seed

# Run tests
make test                # unit only
make test-integration    # integration
make test-all            # both + coverage

# Lint / format
make lint
make fmt

# Eval harness
make eval
```

---

## Shutdown

```bash
# Stop host processes: Ctrl+C in each terminal (dev, worker, ui)

# Stop Docker infra:
docker compose down
# Add -v to also drop the pgvector volume (DESTRUCTIVE — wipes the DB).
```

---

## Troubleshooting

- **Port 8000 already in use** — a stray uvicorn or the compose `api` is up.
  `docker compose stop api` and `lsof -i :8000`, then kill the PID.
- **Worker not picking up jobs** — confirm `make worker` is actually running
  (terminal 3) and that the compose `worker` container is NOT also up
  (`docker compose ps`). Two workers competing causes flaky behavior.
- **Mac kernel panic / freeze on Generate** — memory pressure from BGE +
  reranker. Lower `EMBEDDING_BATCH_SIZE`, `WORKER_CONCURRENCY_EMBEDDING`,
  and `WORKER_DRAFT_CONCURRENCY` in `.env`. See conservative values in
  the `.env` comments.
- **Migrations hang** — pgbouncer is in transaction-pooling mode; Alembic
  must use `DATABASE_URL_DIRECT` (port 5432 → postgres, not pgbouncer).
  Check `alembic/env.py` is reading the direct URL.

---

## Performance tuning (env vars)

Drop these into `.env`. Defaults shown are from `app/settings.py`. The "M1
8 GB" column is the safe value if you're hitting kernel panics, slow
ingestion, or BGE OOMs on Apple Silicon. Bump back toward defaults on a
beefier box.

```bash
# ── Embeddings / device ─────────────────────────────────────────────────
# CPU on M1 is the boring-correct choice — MPS has a 9 GB pool cap and
# OOMs unpredictably when BGE-large + reranker share the device. "cuda"
# on a Linux box with a real GPU.
TORCH_DEVICE=cpu                       # default cpu  | options: cpu | mps | cuda
EMBEDDING_MODEL=BAAI/bge-large-en-v1.5 # default; ~1.3 GB resident
RERANKER_MODEL=BAAI/bge-reranker-base  # default; ~280 MB resident
EMBEDDER_PROVIDER=bge                  # bge (local) | openai (cloud, costs $$)
EMBEDDING_BATCH_SIZE=32                # default 32 | M1 8GB: 8–16 to avoid OOM
HF_TOKEN=                              # only needed for gated models

# ── Worker concurrency (semaphores per job kind) ────────────────────────
# Each unit = one in-flight job. Multiply by per-job RAM to estimate
# peak: e.g. OCR=2 means up to 2 PaddleOCR instances at ~1 GB each.
WORKER_CONCURRENCY_OCR=2               # default 2 | M1 8GB: 1
WORKER_CONCURRENCY_EMBEDDING=4         # default 4 | M1 8GB: 2 (BGE is the heavy one)
WORKER_CONCURRENCY_DEFAULT=2           # default 2 | catch-all for layout/chunk/etc.
WORKER_DRAFT_CONCURRENCY=2             # default 2 | M1 8GB: 1 (each draft holds an LLM connection)
WORKER_CONCURRENCY_FEW_SHOT=4          # default 4 | edit-embedding workers; safe to leave at 4
WORKER_HEARTBEAT_INTERVAL=10           # seconds between worker heartbeats
JOB_STALE_TIMEOUT=120                  # seconds; reconciler reclaims jobs whose heartbeat is older
JOB_QUEUE_MAX_PENDING=100              # 429 backpressure threshold on POST /api/documents

# ── Retrieval (hybrid BM25 + dense + rerank) ────────────────────────────
# HNSW_EF_SEARCH trades recall for latency at query time. 100 is the
# sweet spot; bump to 200 if recall feels low, drop to 50 for snappier
# queries on a slow box.
HNSW_EF_SEARCH=100                     # default 100 | range 50–400
RETRIEVAL_WORK_MEM=64MB                # per-connection work_mem for retrieval queries
RETRIEVAL_STATEMENT_TIMEOUT=5s         # kill any retrieval query >5s
MAX_CHUNKS_PER_DOC_PER_QUERY=3         # diversity cap; prevents one big doc dominating
TRIGRAM_THRESHOLD=0.15                 # pg_trgm similarity floor for proper-noun fallback
RETRIEVER_ALWAYS_TRIGRAM=false         # true = always run trigram branch (debug only)

# ── OCR ─────────────────────────────────────────────────────────────────
# Paddle on CPU is ~3× slower than GPU but the GPU path on M1 isn't worth
# the setup pain. Lower OCR_RASTER_DPI to 200 if you mostly ingest
# native-text PDFs (the dpi only matters when we rasterise scanned ones).
OCR_USE_GPU=false                      # default false on M1 | true if CUDA available
OCR_PAGE_WORKERS=4                     # thread-pool size for per-page OCR | M1 8GB: 2
OCR_RASTER_DPI=300                     # default 300 | drop to 200 if speed > fidelity

# ── Draft engine ────────────────────────────────────────────────────────
# Top-K controls how many chunks each pass sees. Bigger K = more grounded
# but more tokens billed to the LLM tier. SECTION_MAX_TOKENS is per
# section, not per draft.
DRAFT_EXTRACTION_TOP_K=5               # pass 1 (field extraction)
DRAFT_SECTION_TOP_K=8                  # pass 2 (section generation)
DRAFT_SECTION_MAX_TOKENS=1200          # output cap per section
DRAFT_REGENERATE_TIMEOUT_S=30          # per-section regenerate hard timeout

# ── LLM router / budget ─────────────────────────────────────────────────
# VLLM_BASE_URL: for RunPod Serverless use the OpenAI-compatible URL,
# e.g. https://api.runpod.ai/v2/<endpoint-id>/openai/v1
# IMPORTANT: pydantic-settings does NOT strip inline comments — put any
# explanatory text on its OWN line, never after the value.
VLLM_BASE_URL=http://localhost:8001/v1
RUNPOD_API_KEY=                        # bearer token for RunPod vLLM endpoint
ANTHROPIC_API_KEY=                     # fallback tier
OPENAI_API_KEY=                        # only used for vision tier today
GEMINI_API_KEY=                        # optional
LLM_HOURLY_BUDGET_USD=5.00             # global rolling-window cap; exceed → BudgetExceededError
MAX_VLM_PAGES_PER_DOC=20               # per-document VLM page cap

# ── Few-shot store / edits (M9) ─────────────────────────────────────────
FEW_SHOT_TOP_K=3                       # how many past edits to inject per section
FEW_SHOT_INDEX_MAX_ATTEMPTS=5          # reconciliation sweep retries for un-embedded edits
EDIT_METRICS_DEFAULT_DAYS=30           # window for GET /api/templates/{id}/edit-metrics

# ── Rule extractor (M10) ────────────────────────────────────────────────
# Runs periodically in the worker. Lower interval = faster rule updates
# at the cost of more LLM calls. Min edits keeps it from over-fitting on
# 1–2 corrections.
RULE_EXTRACTOR_INTERVAL_HOURS=6        # default 6 | bump to 24 to save tokens
RULE_EXTRACTOR_MIN_EDITS=3             # don't extract a rule from fewer than N edits
RULE_EXTRACTOR_SIMILARITY_THRESHOLD=0.88  # cosine threshold for clustering edits
RULE_EXTRACTOR_MAX_EDITS_PER_GROUP=12  # cap to keep prompts bounded
RULE_EXTRACTOR_FIRST_RUN_LOOKBACK_DAYS=30
RULE_EXTRACTOR_VERSION_LOOKBACK=2
RULE_EXTRACTOR_ADMIN_MAX_DURATION_S=180  # /admin/rule-extractor/run hard timeout

# ── SSE / streaming ─────────────────────────────────────────────────────
SSE_KEEPALIVE_SECONDS=15.0             # heartbeat ping interval
SSE_POLL_INTERVAL_SECONDS=1.0          # server-side event-bus poll
SSE_MAX_STREAM_SECONDS=600.0           # disconnect long-lived streams after 10 min

# ── Misc ────────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES=52428800              # 50 MB upload cap
LOG_LEVEL=INFO                         # DEBUG noisy but useful when chasing pipeline issues
```

### Quick presets

**M1 8 GB laptop, just want it to work:**
```bash
TORCH_DEVICE=cpu
EMBEDDING_BATCH_SIZE=8
WORKER_CONCURRENCY_OCR=1
WORKER_CONCURRENCY_EMBEDDING=2
WORKER_DRAFT_CONCURRENCY=1
OCR_PAGE_WORKERS=2
OCR_RASTER_DPI=200
HNSW_EF_SEARCH=50
```

**Linux box with a real GPU:**
```bash
TORCH_DEVICE=cuda
OCR_USE_GPU=true
EMBEDDING_BATCH_SIZE=64
WORKER_CONCURRENCY_EMBEDDING=8
HNSW_EF_SEARCH=200
```

**RunPod serverless vLLM (LLM tier only, embedder still local):**
```bash
VLLM_BASE_URL=https://api.runpod.ai/v2/<endpoint-id>/openai/v1
RUNPOD_API_KEY=rpa_xxx
# leave TORCH_DEVICE=cpu — embedder runs locally regardless
```
