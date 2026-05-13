# Fixes & Non-Negotiables

A senior reviewer pushed back on the v1 design. Their criticisms were valid. This doc converts each into a concrete engineering rule that every milestone must honor. **If a milestone violates one of these, the milestone is not done.**

Every milestone spec links back here. Subagents and Claude Code should read this before generating code.

---

## NN-1 — Ingestion is recoverable from crash

**The problem:** Background ingestion tasks live in the API process. A crash mid-pipeline (after hash write, before embedding) leaves the document in a half-baked state. Re-uploads silently return the broken doc. Retrieval returns nothing. The operator never knows.

**The rules:**

1. **Persistent job queue, not in-memory.** Every ingestion task is a row in a `jobs` table with: `id`, `kind`, `payload_json`, `status`, `attempts`, `picked_up_at`, `heartbeat_at`, `completed_at`, `error`. Workers claim jobs with `SELECT ... FOR UPDATE SKIP LOCKED` and update `heartbeat_at` every 10s.

2. **Document state machine is granular**, not binary:
   ```
   uploaded → ocr_pending → ocr_running → ocr_done
            → layout_running → layout_done
            → chunking_running → chunking_done
            → embedding_running → ready
            (any stage) → failed
   ```
   The `status` column on `documents` tracks the current stage. The `embedding_completed` boolean (or `embedded_at` timestamp) is what retrieval checks — not `status`.

3. **Startup reconciliation sweeper.** On every API boot:
   ```sql
   -- Reclaim stuck jobs
   UPDATE jobs SET status = 'pending', picked_up_at = NULL
   WHERE status = 'running' AND heartbeat_at < NOW() - INTERVAL '2 minutes';

   -- Identify partial documents and re-queue the missing stage
   SELECT d.id, d.status FROM documents d
   WHERE d.status NOT IN ('ready', 'failed')
     AND d.updated_at < NOW() - INTERVAL '10 minutes';
   ```
   Each partial doc gets a job re-enqueued for its missing stage. The decision is in `app/jobs/reconciler.py`.

4. **Periodic reconciliation sweep** every 5 minutes via the same reconciler. Idempotent — running it twice is a no-op.

5. **Retrieval refuses partial documents.** The retriever's query joins on `documents.status = 'ready'`. Anything in-progress simply doesn't appear in results — never returns half-embedded chunks.

**Owned by:** M1 (job queue), M3 (state machine), M5 (embedding completion gate), plus the reconciler module touched in M1.

---

## NN-2 — Hash idempotency is atomic

**The problem:** Read-then-write idempotency check races on concurrent uploads. Two identical files arriving together produce duplicate ingestion pipelines.

**The rule:**

Always insert with `ON CONFLICT ... RETURNING ... (xmax = 0) AS inserted`:

```sql
INSERT INTO documents (id, sha256, filename, status, ...)
VALUES (gen_random_uuid(), :sha256, :filename, 'uploaded', ...)
ON CONFLICT (sha256) DO UPDATE
  SET last_accessed_at = NOW()
RETURNING id, (xmax = 0) AS inserted;
```

`xmax = 0` is `true` when the row was inserted by this statement (the standard Postgres idiom). The application reads `inserted`:
- `true` → enqueue ingestion job
- `false` → return the existing `document_id`, do not enqueue

No application lock. No race window. One SQL statement.

**Owned by:** M3 (ingestion endpoint).

---

## NN-3 — Concurrency is bounded; queue is durable

**The problem:** FastAPI `BackgroundTasks` queues work with no concurrency cap. 20 simultaneous uploads spawn 20 simultaneous OCR + embedding pipelines, blow the GPU, starve vLLM, OOM, crash.

**The rules:**

1. **No `BackgroundTasks` for ingestion.** Use the Postgres job queue from NN-1.

2. **Worker concurrency is explicit.** A separate `app/jobs/worker.py` process polls the queue with a bounded `asyncio.Semaphore(N)`. Default `N=2` for OCR-bound work, `N=4` for embedding-bound work. Configurable per kind.

3. **Per-stage queues.** Jobs are tagged by `kind` (`ocr`, `layout`, `chunking`, `embedding`). The worker can be configured to drain one kind faster than another. Single Postgres table, partitioned in the query.

4. **The worker is a separate process in Compose**, not a coroutine in the API. Same image, different entrypoint (`python -m app.jobs.worker`). The API process should not do ingestion work — it should enqueue and return. This also fixes "what if I want to scale ingestion separately."

5. **Backpressure on upload.** If `SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running')` exceeds a threshold (default 100), `POST /documents` returns `429 Too Many Requests` with `Retry-After`. Loud, not silent.

**Owned by:** M1 (queue + worker skeleton), M3 (upload backpressure).

---

## NN-4 — Postgres is multi-schema; vector and FTS isolated

**The problem:** One Postgres instance serving HNSW scans, GIN-FTS scans, append-heavy LLM logs, and transactional writes leads to lock contention and noisy-neighbor symptoms under any real load.

**The rules:**

1. **Three schemas in one database**:
   - `app` — `documents`, `pages`, `blocks`, `chunks`, `drafts`, `edits`, `templates`. Transactional, OLTP-shape.
   - `jobs` — `jobs`, `job_history`. Append-heavy, frequent updates, short-lived rows.
   - `llm_log` — `llm_requests`, `llm_cache`. Append-only log + cache. Largest table.

2. **pgbouncer in the Compose stack** in transaction-pooling mode. Single connection limit on the database; pool exposed to app. Removes the "Postgres ran out of connections" failure mode at zero cost.

3. **Heavy queries set `work_mem` locally.** Vector retrieval and rerank fetch use:
   ```sql
   SET LOCAL work_mem = '64MB';
   SET LOCAL statement_timeout = '5s';
   ```
   Sort-spill to disk is acceptable; runaway memory is not.

4. **HNSW index parameters** are explicit at index creation, not defaults:
   ```sql
   CREATE INDEX chunks_embedding_idx ON app.chunks
     USING hnsw (embedding vector_cosine_ops)
     WITH (m = 16, ef_construction = 64);
   -- query-time: SET hnsw.ef_search = 100;
   ```

5. **LLM log table is partitioned by month** with `pg_partman`. v1 ships the partition root + first month; partman handles future months. Old partitions detach + drop per retention policy (default 90 days).

6. **Cache table has a TTL column** + a cheap nightly DELETE. Not partitioned — it's the cache, churn is fine.

**Owned by:** M1 (all schemas, indexes, partitioning, pgbouncer).

---

## NN-5 — Template snapshot is immutable per draft

**The problem:** Templates have a `version` field that the rule extractor bumps. If draft generation re-queries the registry mid-pipeline, extraction and validation can run against different template versions, poisoning the edit log with mis-tagged signal.

**The rules:**

1. **`DraftEngine.generate()` loads the template once** at the top of the function and passes the snapshot object explicitly to every sub-step (extractor, retriever queries, generator, validator). No method below `generate()` calls `template_registry.get_latest()`.

2. **The snapshot includes a `prompt_fingerprint`** — `sha256(system_prompt + appended_rules + extraction_schema + section_specs)`. This fingerprint is what goes into edit logs and the LLM cache key, not the integer `version`. A rule append without a version bump still changes the fingerprint and busts the cache.

3. **Templates are loaded by version, not "latest", inside `generate()`.** The code is:
   ```python
   async def generate(self, template_id: str, ...):
       template = self.registry.get_latest(template_id)   # ONLY here
       fingerprint = template.compute_fingerprint()
       # pass `template` and `fingerprint` to every sub-call
   ```

4. **Edits log both `template_version` and `prompt_fingerprint`.** The rule extractor groups by `prompt_fingerprint` when analyzing patterns, not by `template_version` — that's the integrity guarantee.

**Owned by:** M7 (draft engine), M9 (edit logging).

---

## NN-6 — VLM has per-document and global spend caps

**The problem:** A concurrency semaphore caps speed, not money. A 200-page handwritten document burns $2–6 in VLM calls before anyone notices.

**The rules:**

1. **Per-document VLM cap.** Before any VLM call, check the document's VLM-page counter against `MAX_VLM_PAGES_PER_DOC` (default 20, configurable). Over cap → mark page `status='vlm_budget_exceeded'`, surface a UI warning, skip the VLM call.

2. **Global rolling-window spend cap.** The LLM router tracks USD spend in a sliding window (default 1h). Over cap → return `BudgetExceededError` for any escalation tier. The cheap local path is unaffected. Configurable via `LLM_HOURLY_BUDGET_USD`.

3. **Per-session warning.** The first time a document hits the per-doc cap in a session, the UI shows a one-time toast with the cap and how to raise it. Quiet thereafter.

4. **The dashboard shows live spend.** `/admin/llm-stats` includes current hour spend + budget remaining. Two lines on the page, not a sub-feature.

**Owned by:** M2 (router with budget tracking), M4 (per-document VLM accounting).

---

## NN-7 — LLM cache key is prompt-content addressed

**The problem:** A cache key built from `template_version` misses when `appended_rules` grows without a version bump.

**The rule:**

Cache key includes the full resolved prompt content, not version integers:

```python
def cache_key(
    *,
    model_id: str,                # provider:model:revision
    messages: list[Message],      # full resolved messages
    schema: type[BaseModel] | None,
    sampling: SamplingParams,
) -> str:
    return sha256_hex(
        json.dumps({
            "model_id": model_id,
            "messages": [m.model_dump() for m in messages],
            "schema": schema.model_json_schema() if schema else None,
            "sampling": sampling.model_dump(),
        }, sort_keys=True)
    )
```

The `messages` already contain the resolved system prompt with rules appended. Any change to inputs changes the key. The `prompt_fingerprint` from NN-5 is a useful tag for grouping cache stats, not the key itself.

**Owned by:** M2 (router cache).

---

## NN-8 — BM25 is tuned for legal text, not English-by-default

**The problem:** `to_tsvector('english', ...)` aggressively stems and discards short tokens. "Parties" → "parti". "42 U.S.C. § 1983" → noise. Latin phrases, party names, statute citations retrieve poorly.

**The rules:**

1. **Custom Postgres text search configuration** named `legal_en`:
   ```sql
   CREATE TEXT SEARCH CONFIGURATION legal_en (COPY = english);
   -- Override the asciiword/word/numword mappings to use the `simple`
   -- dictionary for tokens that look like proper nouns, acronyms, or
   -- statute citations. Keep `english_stem` for ordinary prose tokens.
   ```

2. **Hybrid retrieval includes a tri-gram pass** for proper-noun-heavy queries. `pg_trgm` `similarity()` over `chunks.text` with a low threshold catches party names BM25's stemmer mangles.

3. **Token-preserving identifiers.** Statute citations, case names, and party names go into a separate `chunks.entities` text array (extracted in M5 chunking via a tiny regex pass) and are indexed via GIN. The retriever scores entity overlap separately and folds it into RRF.

4. **Empirical check is mandatory.** The eval in M12 includes a retrieval test where every query is built from a known-rare term ("Habeas corpus", "Pearson Specter Litt", "§ 1983"). If recall@10 < 0.85 for these, the config is wrong, not the test.

**Owned by:** M1 (text search config), M5 (entity extraction during chunking), M6 (tri-gram + entity scoring in fusion), M12 (eval).

---

## NN-9 — Rule Extractor is triggered, not hopeful

**The problem:** "Nightly or on-demand" doesn't run. There must be a thing that actually triggers it.

**The rules:**

1. **Admin HTTP endpoint** `POST /admin/rule-extractor/run` triggers an extraction job. Used by the demo to show the loop closing within a session.

2. **APScheduler in the worker process** runs the extractor every 6h by default. Configurable. Single source of truth — not "nightly" in prose, an actual cron expression in code.

3. **The extractor is idempotent.** Running it twice on the same edit set produces the same rule set (the rules are diffed against the template's current `appended_rules` and only added if novel).

4. **Manual trigger from the UI** — the admin/edit-stats page has a "Re-extract rules now" button hitting the endpoint. Lets the reviewer demonstrate the loop in 30 seconds.

**Owned by:** M10.

---

## NN-10 — SSE reconnect doesn't strand the operator

**The problem:** SSE drops on flaky networks. Without `Last-Event-ID` handling, the UI never sees the `ready` event and shows a forever-spinner.

**The rules:**

1. **Every SSE event has an `id`** (monotonic per document_id). Stored on `documents.last_event_seq` for replay.

2. **The SSE endpoint honors `Last-Event-ID` header.** On reconnect, the server replays events since that ID before resuming live streaming.

3. **The UI's status component falls back to polling** `GET /api/documents/:id` every 5s if SSE has been disconnected for > 30s. The polling response is identical in shape to what the SSE event carries.

4. **Status endpoint is the source of truth.** SSE is an optimization. If SSE didn't exist, polling would still work.

**Owned by:** M3 (SSE + status endpoint), M11 (UI client).

---

## NN-11 — Few-shot embeddings have a reconciliation sweep

**The problem:** An edit is logged but its embedding write fails. The edit is permanently absent from few-shot retrieval.

**The rules:**

1. **Edits have `few_shot_indexed_at` timestamp** column. `NULL` means not yet embedded into the few-shot store.

2. **Reconciliation sweep** (same `app/jobs/reconciler.py` from NN-1) periodically:
   ```sql
   SELECT id FROM app.edits
   WHERE few_shot_indexed_at IS NULL
     AND created_at < NOW() - INTERVAL '1 minute'
   LIMIT 100;
   ```
   For each, attempt embedding + index. Failure is logged and retried next sweep with exponential backoff (`attempts` counter on the row).

3. **Newly written edits enqueue their own index job** immediately. The sweep is the safety net, not the primary path.

**Owned by:** M9.

---

## NN-12 — Observability is built in, not bolted on

This isn't from the review but the milestones cumulatively need to honor it.

**The rules:**

1. **Every request has a correlation ID** (`X-Request-ID` header in / set if absent). Logged in every line for the request. Returned in the response header. Used as the `trace_id` for all downstream LLM router calls.

2. **Structured JSON logs to stdout.** No print statements. `structlog` configured at the entry point. Every log line has at minimum: `ts`, `level`, `request_id`, `event`, plus event-specific fields.

3. **Error path is typed**, always. Every domain error is a subclass of `AppError` with an error code string. The FastAPI exception handler translates these to consistent JSON responses with the error code, message, and request ID. Never a raw 500 with an HTML page.

4. **LLM call metrics are mandatory** — every `LLMRouter.generate` call writes to `llm_log.llm_requests` with the fields from the router doc. The `/admin/llm-stats` endpoint reads from this table.

5. **Eval harness logs to the same place.** Eval runs produce comparable telemetry, not a separate sink.

**Owned by:** M0 (logging skeleton, error class hierarchy, request ID middleware), M2 (LLM log writes).

---

## How to use this doc

- Every milestone spec has a **"Non-Negotiables that apply"** section listing the NN-N rules relevant to it.
- When generating code, Claude Code (or sub-agents) should re-read the listed NN-N entries before starting.
- Code review and tests should explicitly verify the NN behavior. If a test would not catch a violation, the test is incomplete.
