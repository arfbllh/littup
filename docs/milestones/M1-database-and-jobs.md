# M1 — Database Layer & Job Queue

**Estimated time:** 2 hours
**Dependencies:** M0
**Rubric impact:** Code Quality (foundation that prevents every later milestone from making mistakes)

## Goal

A working database layer with all aggregates modeled, three schemas (`app`, `jobs`, `llm_log`), `pgvector` + `pg_trgm` + the `legal_en` custom text search config, pgbouncer in the stack, a Postgres-backed job queue with heartbeats and stale-job reclaim, and a worker process skeleton that picks jobs from the queue. The reconciler module exists with no handlers yet but the scheduling scaffolding is in place.

This is the milestone that decides whether the system can survive a crash. Take it seriously.

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-1, NN-3, NN-4, NN-8**
2. `docs/architecture/project-skeleton.md`
3. `docs/architecture/03-components/retrieval.md` (DDL sketch)
4. `docs/architecture/03-components/ingestion-ocr.md` (block/span shapes)
5. `docs/architecture/03-components/edit-loop.md` (if it exists — otherwise infer Edit shape from NN-11)

## Non-Negotiables that apply

- **NN-1** — recoverable ingestion; document state machine; reconciliation sweeper
- **NN-3** — bounded concurrency; durable job queue; separate worker process
- **NN-4** — three schemas; pgbouncer; HNSW params explicit; LLM log partitioned
- **NN-8** — `legal_en` custom text search configuration

## Files to create

### Database

- `app/db/session.py` — async engine, sessionmaker; `get_session()` dependency for FastAPI
- `app/db/models/__init__.py` — re-exports all models
- `app/db/models/base.py` — declarative base + naming convention for constraints
- `app/db/models/document.py` — `Document`, `Page`, `Block`, `Span` (see schema below)
- `app/db/models/chunk.py` — `Chunk` with `embedding VECTOR(1024)`, `text_tsv TSVECTOR`, `entities TEXT[]`
- `app/db/models/draft.py` — `Draft`, `Section`, `Citation`
- `app/db/models/edit.py` — `Edit` with `prompt_fingerprint`, `few_shot_indexed_at`
- `app/db/models/template.py` — `TemplateVersion` (id, template_id, version, yaml_body, system_prompt, appended_rules, created_at). Schema only; population is M7.
- `app/db/models/job.py` — `Job`, `JobHistory`
- `app/db/models/llm_log.py` — `LLMRequest` (partitioned root + first partition), `LLMCache`

### Migrations

- `app/db/migrations/env.py`
- `app/db/migrations/versions/0001_initial.py` — the first migration. Hand-written, not autogen. It must:
  1. Create extensions: `vector`, `pg_trgm`, `pgcrypto` (for `gen_random_uuid`), `pg_partman` (optional — if not present, do month partitioning manually for first month, with a comment about adding partman later)
  2. Create schemas: `app`, `jobs`, `llm_log`
  3. Create the `legal_en` text search configuration as described in NN-8 (start with `COPY = english` plus a documented stub to refine in M5)
  4. Create all tables in their proper schemas
  5. Create indexes — HNSW on `app.chunks.embedding` with `m=16, ef_construction=64`; GIN on `text_tsv`; B-tree on `document_id`; trgm GIN on `text`
  6. Create the partitioned root for `llm_log.llm_requests` (partition by RANGE on `created_at`, monthly) and the first month's partition
  7. Create a small `legal_en_test()` SQL function that demonstrates the config: returns `to_tsvector('legal_en', '...')` for a fixed input (used in M6 test)

### Jobs

- `app/jobs/__init__.py`
- `app/jobs/kinds.py` — `class JobKind(str, Enum)`: `OCR`, `LAYOUT`, `CHUNKING`, `EMBEDDING`, `RULE_EXTRACTION`, `FEW_SHOT_INDEX` (some used only later — define them all now). Handler registry: `HANDLERS: dict[JobKind, Callable]` — empty for now, milestones M3+ register handlers.
- `app/jobs/queue.py` — `JobQueue` class:
  - `enqueue(kind, payload, dedup_key=None) -> Job`
  - `claim_one(allowed_kinds, worker_id) -> Job | None` (`SELECT ... FOR UPDATE SKIP LOCKED ... LIMIT 1`, sets `status='running'`, `picked_up_at=NOW()`, `heartbeat_at=NOW()`)
  - `heartbeat(job_id, worker_id)` (updates `heartbeat_at`; raises if worker_id doesn't match)
  - `complete(job_id, result_json)` / `fail(job_id, error_msg, retryable)`
  - Stale reclaim query exposed as `reclaim_stuck(timeout='2 minutes') -> int`
  - Backpressure query `pending_count() -> int`
- `app/jobs/worker.py` — `python -m app.jobs.worker` entrypoint. Polls `JobQueue.claim_one` in a loop with backoff when empty. Per-kind semaphores from config. Heartbeats every 10s in a background task while a handler runs. Graceful shutdown on SIGTERM (finish current job, mark in-flight if can't).
- `app/jobs/reconciler.py` — `Reconciler` class with:
  - `reclaim_stuck_jobs()`
  - `find_partial_documents()` — `documents.status NOT IN ('ready','failed') AND updated_at < NOW() - INTERVAL '10 min'`. Re-enqueues the missing stage job for each.
  - `find_unembedded_edits()` (NN-11; stub for now, fully wired in M9).
  - Periodic invocation via a simple `asyncio` loop in the worker (every 60s) — APScheduler comes in M10.
- `app/jobs/scheduler.py` — empty file with the import path reserved; M10 populates.

### Infra

- `docker-compose.yml` — add: `postgres` upgraded to 16, with init script to install extensions and run alembic; `pgbouncer` (transaction-pooling mode); `worker` (same image as `api`, command `python -m app.jobs.worker`).
- `docker/postgres/init.sql` — `CREATE EXTENSION IF NOT EXISTS vector;` etc. (alembic also creates them, but the init script makes a cold start faster)
- `docker/pgbouncer/userlist.txt` and `docker/pgbouncer/pgbouncer.ini` — minimal config

### Tests

- `tests/integration/test_migrations.py` — apply migrations to a clean DB; assert extensions installed; assert `legal_en` config exists and `legal_en_test()` returns a non-empty `tsvector`; assert HNSW index exists with the right params; assert partitioned root for `llm_requests` exists with at least one partition.
- `tests/integration/test_job_queue.py` — enqueue, claim with two workers, assert only one gets the job; complete a job; fail a job with retry; let a "worker" die (just don't heartbeat) and assert `reclaim_stuck` recovers it.
- `tests/integration/test_reconciler.py` — insert a document with `status='ocr_running'` and old `updated_at`; assert `find_partial_documents` returns it.
- `tests/unit/test_session.py` — session lifecycle, rollback on exception.

## Schema details

### `app.documents`
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
sha256          TEXT NOT NULL UNIQUE
filename        TEXT NOT NULL
mime_type       TEXT
size_bytes      BIGINT
page_count      INT
doc_type        TEXT          -- native | scan | mixed | unknown
status          TEXT NOT NULL -- uploaded|ocr_pending|ocr_running|...|ready|failed
embedded_at     TIMESTAMPTZ
last_event_seq  BIGINT NOT NULL DEFAULT 0  -- NN-10 SSE
error_code      TEXT
error_message   TEXT
vlm_pages_used  INT NOT NULL DEFAULT 0    -- NN-6
created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
last_accessed_at TIMESTAMPTZ DEFAULT NOW()
```

### `app.pages`, `app.blocks`, `app.spans`
Follow `03-components/ingestion-ocr.md`. Coordinates always preserved. Foreign keys cascade-delete from documents.

### `app.chunks`
Already sketched in `03-components/retrieval.md`. Plus:
```sql
entities TEXT[]          -- NN-8 proper nouns / statute citations extracted during chunking
prompt_fingerprint TEXT  -- NULL until used in a draft (informational)
```

### `app.drafts`, `app.sections`, `app.citations`
```sql
-- draft
id, template_id, template_version, prompt_fingerprint, document_ids UUID[],
status (generating|ready|edited|failed),
ai_output JSONB, final_output JSONB,  -- final populated when user saves
generated_at, edited_at,
model_used TEXT, tokens_in INT, tokens_out INT, cost_usd NUMERIC

-- section (one per draft section)
id, draft_id, name, ai_text, final_text, target_length_min, target_length_max

-- citation (one row per cited chunk in a section)
id, section_id, chunk_id, claim_span_start, claim_span_end,
validation_status TEXT,   -- supported|partial|unsupported|contradicted|unchecked
validation_reason TEXT
```

### `app.edits`
```sql
id, draft_id, template_id, template_version, prompt_fingerprint,
field_or_section_name TEXT,
field_type TEXT,                       -- 'field' | 'section'
ai_value JSONB, user_value JSONB,
diff JSONB,                            -- structured diff
context JSONB,                         -- source_chunks ids, model_used, etc.
embedding VECTOR(1024),                -- nullable until indexed
few_shot_indexed_at TIMESTAMPTZ,       -- NN-11
created_at TIMESTAMPTZ DEFAULT NOW()
```

### `app.templates`
```sql
template_id TEXT NOT NULL,  -- e.g. "case_fact_summary"
version     INT  NOT NULL,
yaml_body   TEXT NOT NULL,  -- full YAML serialization
system_prompt        TEXT,
appended_rules JSONB NOT NULL DEFAULT '[]',
prompt_fingerprint TEXT NOT NULL,
created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (template_id, version)
```

### `jobs.jobs`
```sql
id UUID PRIMARY KEY,
kind TEXT NOT NULL,
payload JSONB NOT NULL,
status TEXT NOT NULL,        -- pending|running|completed|failed
attempts INT NOT NULL DEFAULT 0,
max_attempts INT NOT NULL DEFAULT 3,
picked_up_at TIMESTAMPTZ,
heartbeat_at TIMESTAMPTZ,
worker_id TEXT,
result JSONB,
error TEXT,
dedup_key TEXT UNIQUE,        -- optional, prevents re-enqueue of identical work
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

### `llm_log.llm_requests` (partitioned by month on `created_at`)
```sql
id UUID,
trace_id TEXT,          -- matches request_id
tier TEXT,
provider TEXT,
model TEXT,
prompt_fingerprint TEXT,
cache_key TEXT,
tokens_in INT, tokens_out INT,
cost_usd NUMERIC(10,6),
latency_ms INT,
status TEXT,
error_code TEXT,
cache_hit BOOLEAN,
created_at TIMESTAMPTZ NOT NULL,
PRIMARY KEY (id, created_at)   -- composite for partitioning
) PARTITION BY RANGE (created_at);
```

### `llm_log.llm_cache`
```sql
cache_key TEXT PRIMARY KEY,
response JSONB,
model TEXT,
created_at TIMESTAMPTZ DEFAULT NOW(),
expires_at TIMESTAMPTZ
```

## Acceptance criteria

- [ ] `make migrate` applies migration 0001 against a clean Postgres in < 5s
- [ ] All three schemas exist; selecting from each works
- [ ] `pgvector`, `pg_trgm`, `pgcrypto` installed
- [ ] `SELECT to_tsvector('legal_en', 'plaintiff Pearson Specter Litt filed')` returns a non-empty `tsvector`
- [ ] `worker` container starts and logs "polling for jobs"
- [ ] Enqueue 5 jobs, run 2 workers, verify each is claimed exactly once
- [ ] Kill a worker mid-job; verify `reclaim_stuck` re-claims it 2 min later
- [ ] `pgbouncer` is in the path; `psql` through it to verify
- [ ] All integration tests pass

## Out of scope

- Any handler logic (OCR, embedding, etc.) — handlers register in their own milestones
- Real APScheduler — that's M10
- Edit indexing job handler — M9
- Template population — M7

## Definition of done

A new contributor runs `make up && make migrate && make test` and gets a green build with all schemas, extensions, a working job queue, and a worker idling. `M1-DONE.md` documents the schema versions and any deviations.

## Sub-agent delegation

Optional: split into (a) "schemas + migrations" and (b) "job queue + worker + reconciler" sub-agents that work in parallel after `app/db/session.py` and `app/db/models/base.py` are in place. Most useful if you're in a hurry. If unsure, build sequentially.
