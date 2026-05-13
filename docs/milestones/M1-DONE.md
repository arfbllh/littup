# M1 — Database Layer & Job Queue: DONE

**Date shipped:** 2026-05-14
**NN rules verified:** NN-1, NN-3, NN-4, NN-8

## What shipped

### ORM Models (`app/db/models/`)
- `base.py` — `DeclarativeBase` with explicit constraint naming convention
- `document.py` — `Document`, `Page`, `Block`, `Span` (cascade delete, coordinate fields)
- `chunk.py` — `Chunk` with `embedding` (VECTOR(1024)), `text_tsv` (TSVECTOR), `entities` TEXT[]
- `draft.py` — `Draft`, `Section`, `Citation`
- `edit.py` — `Edit` with `embedding` (VECTOR(1024)), `few_shot_indexed_at` (NN-11)
- `template.py` — `TemplateVersion` (schema only)
- `job.py` — `Job`, `JobHistory`
- `llm_log.py` — `LLMRequest` (partitioned root), `LLMCache`

### Database Session
- `app/db/session.py` — async engine, `async_session_factory`, `get_session()` FastAPI dependency with commit/rollback lifecycle

### Alembic
- `alembic.ini` — points to `app/db/migrations`, uses `DATABASE_URL_DIRECT` (bypasses pgbouncer)
- `app/db/migrations/env.py` — async migration runner via `run_sync`
- `app/db/migrations/versions/0001_initial.py` — hand-written migration:
  - Extensions: `vector`, `pg_trgm`, `pgcrypto`
  - Schemas: `app`, `jobs`, `llm_log`
  - `legal_en` text search config (COPY = english; M5 will refine dictionaries) (NN-8)
  - `legal_en_test()` SQL function for integration test validation
  - All tables in correct schemas with proper FK + cascade rules
  - HNSW index: `m=16, ef_construction=64` (NN-4)
  - GIN on `text_tsv`, trgm GIN on `text`, GIN on `entities` (NN-8)
  - `chunks_tsv_trigger` — auto-populates `text_tsv` using `legal_en` on INSERT/UPDATE
  - `llm_log.llm_requests` partitioned root + `llm_requests_2026_05` first partition (NN-4)

### Job Queue (`app/jobs/`)
- `kinds.py` — `JobKind` enum, empty `HANDLERS` registry, `get_handler()`
- `queue.py` — `JobQueue`: `enqueue` (ON CONFLICT dedup), `claim_one` (`ANY(:kinds)` SKIP LOCKED, NN-3), `heartbeat`, `complete`, `fail` (retryable/non-retryable), `reclaim_stuck`, `pending_count`; `complete` and `fail` write a `job_history` row via `_write_history`
- `worker.py` — poll loop, per-kind `asyncio.Semaphore`, 10s heartbeat task, SIGTERM graceful shutdown; reconciler runs immediately on startup then every 60s (NN-1, NN-3); task references kept in `_TASKS` set to prevent GC
- `reconciler.py` — `reclaim_stuck_jobs` (uses `settings.JOB_STALE_TIMEOUT`), `find_partial_documents` (re-enqueues missing stage), `find_unembedded_edits` stub (NN-1, NN-11)
- `scheduler.py` — empty, reserved for M10 APScheduler

### Infra
- `docker-compose.yml` — `api`, `worker`, `postgres` (pgvector/pgvector:pg16), `pgbouncer` (bitnami, transaction-pooling mode) (NN-4)
- `docker/postgres/init.sql` — creates extensions on cold start
- `docker/pgbouncer/pgbouncer.ini`, `userlist.txt` — reference config

## Tests

- `tests/unit/test_session.py` — 2 tests (commit on success, rollback on exception)
- `tests/integration/test_migrations.py` — 7 tests (extensions, schemas, legal_en config, legal_en_test(), legal_en tsvector, HNSW params, partitions, trigger)
- `tests/integration/test_job_queue.py` — 7 tests (enqueue, exclusive claim, complete, fail retryable, fail non-retryable, stale reclaim, dedup)
- `tests/integration/test_reconciler.py` — 3 tests (stuck doc re-queue, ready/failed ignored, reclaim)

All 30 tests pass (11 unit + 19 integration). `ruff check` clean. Integration tests require a live `TEST_DATABASE_URL`.

## Deviations from spec

- `TIMESTAMPTZ` is not directly importable from `sqlalchemy.dialects.postgresql`; used `TIMESTAMP(timezone=True)` instead. DDL in the migration uses `TIMESTAMPTZ` natively.
- `text_tsv` is a regular `TSVECTOR` column populated by a trigger (`chunks_tsv_trigger`) rather than a `GENERATED ALWAYS AS` column. This is required to use the `legal_en` config — PostgreSQL does not allow `GENERATED ALWAYS AS` columns to reference a text search configuration by name (only the default).
- `pg_partman` not available in the base pgvector image; first partition created manually. Comment in migration documents the pattern for adding future months.
- `LLMRequest` ORM model maps `created_at` as part of the primary key (required for SQLAlchemy to work with partitioned tables without `__mapper_args__ = {"concrete": True}`).

## Post-ship fixes (same session)

- All ORM timestamp columns changed from `Mapped[str]` to `Mapped[datetime]` — SQLAlchemy returns `datetime` objects at runtime; `str` caused a silent type mismatch for any downstream code doing date arithmetic.
- `JobHistory` wired up: `complete()` and `fail()` now write a `job_history` row via `_write_history()` using `RETURNING kind, status, worker_id` from the job update.
- `_reconcile_loop` now runs once at startup before its first 60s sleep, satisfying NN-1's "startup reconciler re-queues stuck jobs" requirement.
- `reclaim_stuck()` now receives `timeout_seconds=settings.JOB_STALE_TIMEOUT` instead of ignoring the setting.
- `claim_one` f-string SQL replaced with `ANY(:kinds)` parameterised form; guard added for empty kinds list.
- In-flight asyncio tasks now held in `_TASKS` set to prevent silent GC.

## Follow-ups

- M5: refine `legal_en` dictionary mappings (asciiword/numword → `simple` for statute citations and proper nouns).
- M5: run `ANALYZE` after first bulk embedding to let Postgres update HNSW stats.
- M5: add `SET LOCAL work_mem = '64MB'` to heavy retrieval queries (NN-4).
- Monthly: add new `llm_requests_YYYY_MM` partition (or install `pg_partman`).
- M9: wire `find_unembedded_edits` in `Reconciler` to an actual embedding job.
- M10: populate `app/jobs/scheduler.py` with APScheduler cron for rule extractor.
- Pre-prod: pin Dockerfile to a specific Python 3.11.x image tag; add pgbouncer healthcheck.
