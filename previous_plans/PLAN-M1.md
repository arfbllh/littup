# Implementation Plan — M0 (Bootstrap) + M1 (Database & Jobs)

## Pre-flight state

M0 has **not** been implemented. The repo contains only directory skeletons
(empty `__init__.py` files and `.gitkeep` files). No Python entry point, no
Dockerfile, no `pyproject.toml`, no Makefile, no running service.

M1 depends on M0. Both milestones will be implemented in sequence in this
session.

---

## Phase 1 — M0: Bootstrap

**Goal:** Bootable FastAPI service with structlog JSON logging, typed error
hierarchy, `X-Request-ID` middleware, `/healthz` + `/readyz` endpoints, and
a passing test suite. Postgres container present but no schema yet.

### Files to create

| File | Purpose |
|------|---------|
| `pyproject.toml` | Python 3.11+, all deps, `uv` toolchain |
| `uv.lock` | Locked dependencies |
| `.env.example` | Every env var, one-line comments |
| `.gitignore` | Python/Node/IDE/`.env`/`eval/reports`/`__pycache__` |
| `Makefile` | `up`, `down`, `logs`, `test`, `lint`, `fmt`, `migrate`, `seed`, `eval` |
| `docker-compose.yml` | `api` + `postgres` services (pgbouncer + worker added in M1) |
| `Dockerfile` | Multi-stage, non-root, copies `app/` + `config/` |
| `app/main.py` | FastAPI app, mounts health router + middleware |
| `app/settings.py` | Pydantic `BaseSettings`, all env vars |
| `app/core/logging.py` | structlog: JSON in prod, console in dev; `request_id` bound |
| `app/core/errors.py` | `AppError` + `NotFoundError`, `ValidationError`, `ConflictError`, `BudgetExceededError`, `LLMUnavailableError`, `IngestError` |
| `app/core/middleware.py` | `X-Request-ID` middleware (reads or generates uuid7, binds to log context) + `AppError` exception handler |
| `app/core/ids.py` | `new_uuid7()`, `sha256_hex()`, `short_id()` |
| `app/api/routes/health.py` | `GET /healthz` → 200 `{"status":"ok"}`; `GET /readyz` → 503 if DB down |
| `tests/conftest.py` | `httpx.AsyncClient` fixture against the app |
| `tests/unit/test_health.py` | `/healthz` 200 + `X-Request-ID` echo |
| `tests/unit/test_errors.py` | `AppError` → JSON envelope with `code`, `message`, `request_id` |
| `tests/unit/test_logging.py` | Captured log lines are valid JSON with required fields |

### M0 acceptance gates

- `docker compose up` → `curl localhost:8000/healthz` returns 200
- `X-Request-ID: test-123` echoed in response header
- Every log line: valid JSON with `ts`, `level`, `request_id`, `event`
- `make test` green; `make lint` green

---

## Phase 2 — M1: Database Layer & Job Queue

**Goal:** All ORM models, three Postgres schemas, `pgvector`/`pg_trgm`/`legal_en`
text search config, HNSW index, partitioned `llm_requests` table, pgbouncer in
Compose, Postgres-backed job queue with heartbeats + stale reclaim, worker
process skeleton, reconciler stub.

### Files to create / modify

#### Database

| File | Purpose |
|------|---------|
| `app/db/session.py` | Async engine + sessionmaker; `get_session()` FastAPI dep |
| `app/db/models/base.py` | Declarative base, naming conventions |
| `app/db/models/document.py` | `Document`, `Page`, `Block`, `Span` |
| `app/db/models/chunk.py` | `Chunk` — `embedding VECTOR(1024)`, `text_tsv TSVECTOR`, `entities TEXT[]` |
| `app/db/models/draft.py` | `Draft`, `Section`, `Citation` |
| `app/db/models/edit.py` | `Edit` — `embedding VECTOR(1024)`, `few_shot_indexed_at` (NN-11) |
| `app/db/models/template.py` | `TemplateVersion` — schema only, no YAML loading |
| `app/db/models/job.py` | `Job`, `JobHistory` |
| `app/db/models/llm_log.py` | `LLMRequest` (partitioned root + first partition), `LLMCache` |
| `app/db/models/__init__.py` | Re-exports all models |
| `app/db/migrations/env.py` | Alembic env wired to async engine |
| `app/db/migrations/versions/0001_initial.py` | Hand-written migration (see below) |

#### Migration 0001 creates (in order)

1. Extensions: `vector`, `pg_trgm`, `pgcrypto`
2. Schemas: `app`, `jobs`, `llm_log`
3. `legal_en` text search config (COPY english; documented stub for M5 refinement) + `legal_en_test()` SQL function
4. All tables in their proper schemas per M1 DDL spec
5. Indexes:
   - HNSW on `app.chunks.embedding` with `m=16, ef_construction=64` (NN-4)
   - GIN on `text_tsv`
   - B-tree on `document_id`
   - trgm GIN on `chunks.text` (NN-8)
   - GIN on `chunks.entities` (NN-8)
6. `llm_log.llm_requests` partitioned root + first month's partition (2026-05)

#### Jobs

| File | Purpose |
|------|---------|
| `app/jobs/kinds.py` | `JobKind` enum: `OCR`, `LAYOUT`, `CHUNKING`, `EMBEDDING`, `RULE_EXTRACTION`, `FEW_SHOT_INDEX`; empty `HANDLERS` registry |
| `app/jobs/queue.py` | `JobQueue`: `enqueue`, `claim_one` (SKIP LOCKED), `heartbeat`, `complete`, `fail`, `reclaim_stuck`, `pending_count` |
| `app/jobs/worker.py` | Poll + dispatch loop; per-kind `asyncio.Semaphore`; heartbeat task; SIGTERM graceful shutdown |
| `app/jobs/reconciler.py` | `Reconciler`: `reclaim_stuck_jobs`, `find_partial_documents`, `find_unembedded_edits` (stub); 60s asyncio loop |
| `app/jobs/scheduler.py` | Empty file, import path reserved for M10 |

#### Infra

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Add `pgbouncer` + `worker` services; postgres 16 |
| `docker/postgres/init.sql` | `CREATE EXTENSION IF NOT EXISTS vector` etc. (cold-start speedup) |
| `docker/pgbouncer/pgbouncer.ini` | Transaction-pooling mode config |
| `docker/pgbouncer/userlist.txt` | Credentials file |
| `alembic.ini` | Alembic config pointing to `app/db/migrations` |

#### Tests

| File | What it verifies |
|------|-----------------|
| `tests/integration/test_migrations.py` | Extensions installed; `legal_en` config + `legal_en_test()` works; HNSW index exists with correct params; `llm_requests` partition exists |
| `tests/integration/test_job_queue.py` | Enqueue; two-worker exclusive claim; complete; fail + retry; stale reclaim |
| `tests/integration/test_reconciler.py` | Document stuck in `ocr_running` with old `updated_at` is found |
| `tests/unit/test_session.py` | Session lifecycle; rollback on exception |

### M1 acceptance gates (NN-1, NN-3, NN-4, NN-8)

- `make migrate` applies 0001 against a clean Postgres in < 5s
- All three schemas exist; each queryable
- `SELECT to_tsvector('legal_en', 'plaintiff Pearson Specter Litt filed')` returns non-empty tsvector
- `worker` container starts and logs "polling for jobs"
- Enqueue 5 jobs, 2 workers → each claimed exactly once (SKIP LOCKED)
- Kill worker mid-job → `reclaim_stuck` re-claims after 2 min
- `psql` via pgbouncer succeeds
- All integration tests pass

---

## Key design decisions

| Decision | Rationale |
|----------|-----------|
| `text_tsv` as a **regular** column (not GENERATED ALWAYS) | Lets us use `'legal_en'` config not `'english'`; GENERATED columns can't reference a mutable config object safely in older pgvector builds |
| Worker as a **separate** Compose service | NN-3: API never does ingestion work |
| Heartbeat every 10s, reclaim after 2 min | 12× margin; resistant to slow GC pauses |
| First partition named `llm_requests_2026_05` | Matches current month; comment documents adding future partitions |
| `dedup_key UNIQUE` on `jobs` | Prevents double-enqueue of identical work without application-level locking |

---

## Risks

| Risk | Mitigation |
|------|-----------|
| `pgvector` not installed in the Postgres image | Use `pgvector/pgvector:pg16` image, not vanilla `postgres:16` |
| Alembic async dialect setup | Use `asyncpg` driver for the engine; `run_sync` in `env.py` for the migration connection |
| Integration tests need a live Postgres | `conftest.py` reads `TEST_DATABASE_URL`; CI uses a service container; local dev uses Compose |
| `pg_partman` not available in base image | Skip partman; do month partitioning manually; comment explaining where partman would go |

---

## Out of scope (not in this plan)

- Any handler logic (OCR, embedding, etc.)
- APScheduler (M10)
- Edit indexing job handler (M9)
- Template YAML population (M7)
- LLM router (M2)
- Next.js UI (M11)

---

## Sequence

```
1. pyproject.toml + uv.lock
2. .env.example, .gitignore, Makefile
3. Dockerfile, docker-compose.yml (M0 version, api + postgres only)
4. app/settings.py
5. app/core/{ids,logging,errors,middleware}.py
6. app/main.py + app/api/routes/health.py
7. tests/conftest.py + M0 unit tests
8. [M0 acceptance gate check]
9. app/db/models/base.py + all model files
10. app/db/session.py + alembic.ini + migrations/env.py
11. migrations/0001_initial.py
12. app/jobs/{kinds,queue,worker,reconciler,scheduler}.py
13. docker-compose.yml upgrade (pgbouncer + worker)
14. docker/postgres/init.sql + docker/pgbouncer/
15. Integration + unit tests
16. [M1 acceptance gate check]
17. docs/milestones/M0-DONE.md + M1-DONE.md
```
