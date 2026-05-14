# PLAN — M10 Rule Extractor (Stage-2 Edit Loop)

**Spec:** `docs/milestones/M10-rule-extractor.md`
**Depends on:** M9 (shipped) — `app.edits` rows are tagged with `template_id`, `template_version`, `prompt_fingerprint`, `field_or_section_name`, `field_type`, `ai_value`, `user_value`; few-shot retrieval already wired.
**Rubric:** Improvement from Edits — turns M9's per-call few-shot signal into durable, versioned rules on the template. Closes the loop the reviewer is looking for.

## Goal

Run an offline extractor that groups recent edits per template, asks the `analysis`-tier LLM for a generalizable rule, dedups against the template's existing `appended_rules`, and (on novel rule) inserts a new `TemplateVersion` with `version+1` and a recomputed `prompt_fingerprint`. Triggered by APScheduler (default 6h) **and** an admin endpoint. Single-runner across N workers via Postgres advisory lock. Idempotent on re-run.

## Current state (verified)

- `JobKind.RULE_EXTRACTION` already exists in `app/jobs/kinds.py:7-14` — **reserved-but-unused**; M10 runs synchronously in-process and does NOT register a handler (retries are inappropriate; advisory lock is the right primitive; admin endpoint needs a synchronous response per spec).
- `app/draft/templates/registry.py::get_by_version(template_id, version, *, session)` exists (line 177) — used by the rubric demo test to resolve a draft's `prompt_fingerprint` back to its `system_prompt + appended_rules`. No new lookup needed.
- `TemplateRegistry.sync_to_db()` (registry.py:56-120) inserts a new version on YAML fingerprint mismatch. **Footgun:** if the operator edits YAML after M10 has appended rules, `sync_to_db` will write a new version dropping the extracted rules. M10 does **not** modify `sync_to_db` — documented operational note in `M10-DONE.md` (don't reload YAML mid-session; remove bad rules via DB or wait for the rule-deletion endpoint, out of scope). Splitting `appended_rules` into `yaml_rules + extracted_rules` columns is the proper fix and is a follow-up refactor.
- `DraftTemplate.compute_fingerprint()` (`app/draft/templates/schema.py:50-67`) is the existing SHA256-over-canonical-JSON. Reused verbatim — NN-5 holds.
- `app/llm/router.py::generate(..., task="analysis", schema=..., trace_id=...)` already supports JSON-mode structured output with `schema_retry` (router.py:119,159). M10 passes a fixed JSON schema; on parse failure we log + skip the group rather than retrying.
- `app/jobs/worker.py::_main` (line 144, gather at line 160) starts `_poll_loop` + `_reconcile_loop` via `asyncio.gather`. M10 starts the scheduler in the **worker process only**, alongside those. API process never schedules — horizontal API scaling doesn't multiply ticks.
- No `pg_advisory_lock` usage anywhere in the codebase today. M10 introduces it.
- **pgbouncer is in `POOL_MODE: transaction`** (`docker/pgbouncer/pgbouncer.ini`, `docker-compose.yml:63`) and `settings.DATABASE_URL` routes through it. A **session-scoped** `pg_try_advisory_lock` held across multiple LLM calls (each its own implicit txn) would be silently released when pgbouncer returns the server connection to the pool between transactions. M10 must use the existing `settings.DATABASE_URL_DIRECT` (already wired in `app/db/migrations/env.py:21,25`) for the lock-holding session — see "Advisory lock + pgbouncer" section below for the concrete design.
- `apscheduler` not in `pyproject.toml` — add `apscheduler>=3.10,<4` (3.x is stable + async-friendly; 4.x is beta).
- `app/api/routes/admin.py` exists with only `GET /admin/llm-stats`. Router already included in `app/main.py:71` (plan previously said `:63` — verified `:71`).
- `_current_trace_id()` is currently a private helper in `app/api/routes/edits.py:21`. Extract to `app/core/trace.py` as `current_trace_id()` so admin route can reuse — pure refactor, no behavior change. **Do NOT invent a new `new_trace_id()`** — `app/core/ids.py::new_uuid7()` already exists and is the codebase-wide ID helper (used by `middleware.py:16`, `worker.py:148`). Scheduler tick imports `new_uuid7` directly and uses its dashed-string form for consistency with the rest of the trace-ID surface.
- Embedder is the M9 singleton from `app/api/deps.py::get_embedder` — `async def embed(texts) -> list[list[float]]`, 1024-dim.
- Latest migration: `0005_edits_few_shot_index.py`. M10's is `0006_rule_extractor_state.py`. **Spec drift:** `docs/milestones/M10-rule-extractor.md` calls the migration `0002_rule_extractor_state.py` (stale spec numbering); `M10-DONE.md` will note the renumbering.
- `TemplateRegistry.append_rules()` does **not** yet exist; M10 adds it (see Modified §8).

## Files touched

### New

1. **`app/db/migrations/versions/0006_rule_extractor_state.py`** — `CREATE TABLE app.template_extractor_state (template_id TEXT PRIMARY KEY, last_run_at TIMESTAMPTZ, last_edit_id UUID, edits_processed INT NOT NULL DEFAULT 0, rules_added INT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`. `IF NOT EXISTS` so dev resets are safe. Downgrade drops the table.

2. **`app/db/models/template_extractor_state.py`** — SQLAlchemy ORM model matching the migration. UPSERT pattern at write time:
   ```sql
   INSERT INTO app.template_extractor_state (...)
   VALUES (...)
   ON CONFLICT (template_id) DO UPDATE
     SET last_run_at = EXCLUDED.last_run_at,
         last_edit_id = EXCLUDED.last_edit_id,
         edits_processed = template_extractor_state.edits_processed + EXCLUDED.edits_processed,
         rules_added = template_extractor_state.rules_added + EXCLUDED.rules_added,
         updated_at = NOW();
   ```
   Cumulative counters across the template's lifetime; per-run numbers live on the in-memory `ExtractionResult`.

3. **`app/edits/rule_extractor.py`** — `RuleExtractor`, `ExtractionResult` (frozen dataclass), plus the four private helpers below.
   - `class RuleExtractor(session, lock_session, registry, llm_router, embedder, settings)` — note the **two** sessions: `session` (through pgbouncer, used for all data reads/writes) and `lock_session` (direct-connection, holds the advisory lock for the run). See "Advisory lock + pgbouncer" below.
   - `async run(template_id, *, since=None, trace_id=None) -> ExtractionResult` — never raises for "no edits", "no rules", or "lock not held"; only raises for programmer errors (DB unreachable, embedder dim mismatch which is M9's contract).

   `ExtractionResult` (frozen dataclass, fields explicitly enumerated — `app/api/schemas/admin.py::ExtractionResultRow` mirrors these 1:1):
   ```python
   @dataclass(frozen=True)
   class ExtractionResult:
       template_id: str
       skipped_reason: Literal["locked", "no_template", "no_edits", None]  # None == ran
       edits_processed: int          # rows pulled from app.edits this run
       groups_evaluated: int         # groups with n >= MIN_EDITS that hit the LLM
       groups_skipped_min_edits: int # groups with n < MIN_EDITS
       new_rules: list[str]          # rules appended this run (post-dedup)
       new_version: int | None       # None if no rules appended
       prompt_fingerprint: str       # post-run latest (== pre-run if new_version is None)
       trace_id: str | None
   ```

   - `_render_case(edit, max_chars=400) -> tuple[str, str]` — returns `(ai_repr, user_repr)`. Matches M9's `_search_repr` style in `app/edits/few_shot_store.py:134-159` (which renders via `_render_value()`, not party-role notation — earlier plan claim of `"Smith (Plaintiff)"` party formatting was wrong; the actual style is JSON-value rendering with NFC normalize + strip + truncate). Importing from `few_shot_store.py` would create an awkward cycle; the helper is small enough to duplicate. Test fixture strings adjusted accordingly.
   - `_build_analysis_prompt(name, field_type, cases, min_evidence) -> list[Message]` — system message is fixed (below); user message lists numbered cases.
   - `_parse_rule(response, *, min_evidence, trace_id) -> str | None` — JSON-decode `response.text`; defensive returns:
     - JSONDecodeError or non-object → `None`, log `event="rule_extractor.parse_failed"`
     - `rule == "NO_RULE"` → `None`, log `event="rule_extractor.no_rule"` (info, not warning — this is the model doing its job)
     - missing/empty `rule` field → `None`, log `event="rule_extractor.parse_failed"`
     - `evidence_count < min_evidence` → `None`, log `event="rule_extractor.evidence_insufficient"` with `evidence_count` + `min_evidence`
     - else → `rule.strip()`
   - `_is_similar(rule, existing, *, embedder, threshold) -> bool` — casefold short-circuit (`rule.casefold().strip() == existing[i].casefold().strip()` for any i → `True`); else embed `[rule] + existing` and check max cosine ≥ threshold (default `0.88`, BGE-large). If `existing == []` → `False` without embedder call.
   - Advisory-lock helpers `_try_acquire(lock_session)` / `_release(lock_session)` using session-scoped `pg_try_advisory_lock(:k)` / `pg_advisory_unlock(:k)` with `k = settings.RULE_EXTRACTOR_LOCK_ID` (default `9_010_001`). Released in `finally`. **Issued on `lock_session` only** — see "Advisory lock + pgbouncer".

   Algorithm in `run()`:
   1. Acquire advisory lock on `lock_session`. If not acquired → log `event="rule_extractor.lock_busy"` and return `ExtractionResult(skipped_reason="locked", new_rules=[], new_version=None, prompt_fingerprint=<pre-run fp or "">, edits_processed=0, groups_evaluated=0, groups_skipped_min_edits=0, ...)`.
   2. Load `state = SELECT ... FROM template_extractor_state WHERE template_id=:tid` (None if first run).
   3. `effective_since = since or state.last_run_at or NOW() - INTERVAL ':first_run_lookback_days days'`.
   4. `template = await registry.get_latest(template_id, session=session)` — snapshot per NN-5; if missing → return `ExtractionResult(skipped_reason="no_template", ...)`.
   5. `edits = SELECT * FROM app.edits WHERE template_id=:tid AND created_at > :since AND template_version >= :v_floor ORDER BY created_at DESC`. `v_floor` is `max(1, template.version - settings.RULE_EXTRACTOR_VERSION_LOOKBACK)` (default lookback = 2). **Why the version floor:** edits made against a much older template version reflect patterns that may already be encoded as `appended_rules` on the current template; cosine dedup catches most of these but filtering at the SQL layer avoids paying for the LLM call. v1 default of 2 keeps the window generous; tune in M12 eval.
   6. Group by **`(field_or_section_name, field_type)`** (composite key — defends against the unlikely case of a field name being reused across types). For each group with `len >= settings.RULE_EXTRACTOR_MIN_EDITS` (default 3): take the **most recent** `RULE_EXTRACTOR_MAX_EDITS_PER_GROUP` (default 12); `min_evidence = max(2, ceil(n * 0.6))`; build prompt; `await llm_router.generate(messages, task="analysis", schema=RULE_EXTRACTION_SCHEMA, trace_id=trace_id)`; parse; if rule and not `_is_similar(rule, template.appended_rules + new_rules_accumulator, ...)` → append.
   7. If `new_rules`: `new_template = await registry.append_rules(template_id, new_rules, session=session)`; record `new_version` and post-append `prompt_fingerprint`. Else: keep pre-run fingerprint, `new_version=None`.
   8. UPSERT `template_extractor_state` with `last_run_at=NOW()`, `last_edit_id=edits[0].id if edits else state.last_edit_id`, `edits_processed=len(edits)`, `rules_added=len(new_rules)` (the UPSERT adds these to cumulative totals).
   9. Release lock on `lock_session`. Return `ExtractionResult`.

   Session is **not** committed by `run()` — the caller (scheduler tick or admin route) owns the transaction on `session`. `lock_session` is managed by the caller's context manager (entered before `run()`, exited after).

### Advisory lock + pgbouncer

The lock-holding connection MUST bypass pgbouncer (transaction-pooled connections silently drop session-scoped advisory locks between transactions, defeating the lock entirely). Design:

- Add a module-level lazy singleton in `app/db/session.py`: `_direct_engine = create_async_engine(settings.DATABASE_URL_DIRECT, pool_size=2, max_overflow=0)` and an `async_sessionmaker` bound to it. Export `direct_session_factory()`.
- `RuleExtractorScheduler._tick()` and `app/api/deps.py::get_rule_extractor` both open a `lock_session` from `direct_session_factory()` via `async with` and pass it into `RuleExtractor(session=..., lock_session=...)`.
- `lock_session` is used **only** for `pg_try_advisory_lock` / `pg_advisory_unlock` — no other queries. `session` (pgbouncer-routed) does all data work as usual.
- `test_rule_extractor_advisory_lock.py` runs both the "lock held externally" and "lock released" assertions against the **direct** connection URL (the simulated competing holder) AND the **pgbouncer** URL (the extractor's data session). This exercises the actual production topology — running both through direct would mask the original bug.
- Final test assertion `SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=9010001` is issued against the direct URL (pgbouncer would re-route to a different server connection and report 0 vacuously).

4. **`app/jobs/scheduler.py`** — `class RuleExtractorScheduler(session_factory, lock_session_factory, registry, llm_router, embedder, settings)` wrapping `AsyncIOScheduler`. `add_job(self._tick, IntervalTrigger(hours=settings.RULE_EXTRACTOR_INTERVAL_HOURS), id="rule_extractor.tick", max_instances=1, coalesce=True, misfire_grace_time=300)`. `_tick()`:
   1. Generates a per-tick `trace_id = new_uuid7()` (reuses `app/core/ids.py::new_uuid7`; dashed-string form for consistency with middleware/worker).
   2. Binds `structlog.contextvars.bind_contextvars(request_id=trace_id, source="scheduler")`.
   3. Opens `async with session_factory() as session, lock_session_factory() as lock_session:` (data session via pgbouncer; lock session via `DATABASE_URL_DIRECT`).
   4. Calls `_list_known_template_ids(session)` (= `SELECT DISTINCT template_id FROM app.templates`).
   5. Iterates and calls `RuleExtractor(session, lock_session, ...).run(tid, trace_id=trace_id)` for each. Logs each `ExtractionResult` summary (`rule_extractor.run_complete` with `template_id`, `new_rules` count, `skipped_reason`).
   6. `await session.commit()`; clears contextvars in `finally`.

   `max_instances=1` covers in-process re-entry; the advisory lock covers cross-process / cross-worker. Both are required (and load-tested by `test_rule_extractor_advisory_lock.py`).

5. **`app/core/trace.py`** — `def current_trace_id() -> str | None` (extracted from `edits.py:21`). `edits.py` re-imports. **No `new_trace_id()`** — callers use the existing `app.core.ids.new_uuid7` directly (already in use at `middleware.py:16`, `worker.py:148`).

6. **`app/api/schemas/admin.py`** — Pydantic models for the three endpoints:
   - `RuleExtractorRunRequest(template_id: str | None = None)`
   - `ExtractionResultRow` mirrors `ExtractionResult` fields
   - `RuleExtractorRunResponse(results: list[ExtractionResultRow], partial: bool)`
   - `AdminTemplateRow(template_id, latest_version, prompt_fingerprint, appended_rules_count, last_run_at: datetime | None)`
   - `AdminTemplatesResponse(templates: list[AdminTemplateRow])`
   - `TemplateVersionRow(version, prompt_fingerprint, appended_rules: list[str], rules_added_vs_previous: list[str], resolved_system_prompt: str, created_at)`
   - `TemplateVersionsResponse(template_id, versions: list[TemplateVersionRow])` — `rules_added_vs_previous` is the simple set-diff against v-1. `resolved_system_prompt` is `template.system_prompt + "\n\n" + "\n".join(appended_rules)` (the exact string the router would receive at draft time). Including it lets the spec's DoD ("rule visible in the rendered system prompt on the admin page") be satisfied with a single GET — M11 just renders the field.

7. **Tests** (names per spec + the two M10 deems necessary):
   - `tests/unit/test_rule_extractor_parse.py` — valid JSON → rule string; `"rule": "NO_RULE"` → `None` + `event="rule_extractor.no_rule"` log entry; low `evidence_count` → `None` + `event="rule_extractor.evidence_insufficient"` log; empty rule string → `None` + parse_failed log; garbage text → `None` + parse_failed log; extra keys tolerated.
   - `tests/unit/test_rule_extractor_prompt.py` — n cases → n numbered entries in user message; 1000-char ai_value truncated to ≤400 chars; rendered case strings match `_render_value()` style from `few_shot_store.py:134-159` (verified by importing the same fixture pattern M9's tests use); `min_evidence` formula (`n=5 → 3`, `n=12 → 8`, `n=3 → 2`).
   - `tests/unit/test_rule_similarity.py` — empty existing → `False` (assert embedder not invoked); casefold-equal short-circuits without embedder call (assert embedder not invoked); uniform embedder (cosine=1) ≥ 0.88 → `True`; orthogonal embedder (cosine=0) < 0.88 → `False`.
   - `tests/integration/test_rule_extractor_basic.py` — sync `case_fact_summary` (v1, fp=fp1); insert 5 edits for `parties` (Smith → Smith, LLC) using the M9 edit-shape fixtures; mock router returns the well-formed rule with `evidence_count=5`; assert `result.new_version=2`, `result.prompt_fingerprint != fp1`, `app.templates` row v2 contains the rule, `template_extractor_state.rules_added=1`, `template_extractor_state.last_edit_id == edits[0].id`.
   - `tests/integration/test_rule_extractor_no_pattern.py` — 4 inconsistent edits; mock returns `NO_RULE`; assert `new_rules=[]`, no v2 row, `template_extractor_state.rules_added=0`, exactly one `event="rule_extractor.no_rule"` log emitted.
   - `tests/integration/test_rule_extractor_idempotent.py` — run basic scenario twice; mock returns the same rule; second run yields `new_rules=[]` (similarity dedup), `app.templates` has exactly 2 versions.
   - `tests/integration/test_rule_extractor_min_edits.py` — 2 edits for `parties` (below 3); assert mock router **not called** (`make_router` invocation count = 0 for `analysis` tier); `groups_evaluated=0`, `groups_skipped_min_edits=1`.
   - `tests/integration/test_rule_extractor_version_lookback.py` — sync v1, seed 3 edits at `template_version=1`, then bump to v3, then run; with `RULE_EXTRACTOR_VERSION_LOOKBACK=2` and `v_floor=max(1,3-2)=1`, edits ARE included; with lookback=0 and `v_floor=3`, edits are excluded and `groups_evaluated=0`. Pins the SQL filter behavior so a future tuning doesn't silently regress.
   - `tests/integration/test_rule_extractor_advisory_lock.py` — open a competing session against `DATABASE_URL_DIRECT` (NOT pgbouncer): `SELECT pg_advisory_lock(9_010_001)`. Run extractor (its `lock_session` also goes via `DATABASE_URL_DIRECT`) → `skipped_reason="locked"`, `new_rules=[]`, exactly one `event="rule_extractor.lock_busy"` log line. Release in competing session; re-run extractor → succeeds. Final assert via direct URL: `SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=9010001` returns 0 (no leak). The test must use the direct URL — pgbouncer would re-route the `pg_locks` query to a different server connection and report 0 vacuously, masking a real leak.
   - `tests/integration/test_rule_extractor_via_admin.py` — `POST /api/admin/rule-extractor/run {template_id:"case_fact_summary"}` → 200, one result row with new rule; second POST → empty `new_rules`; POST with no `template_id` → row per known template; POST with unknown id → 404 `TEMPLATE_NOT_FOUND`. Also: `GET /api/admin/templates/case_fact_summary/versions` after the first POST returns v2 with `resolved_system_prompt` containing the rule string verbatim (validates the new field on `TemplateVersionRow`).
   - `tests/integration/test_rule_applied_to_next_draft.py` (**rubric demo**) — sync v1; seed 5 edits; run extractor → v2 created with rule R; invalidate registry cache; `DraftEngine.generate(template_id, document_ids=[D6], session=s)` for draft B; assert `draft_B.template_version==2`; resolve via `registry.get_by_version(...)` and assert R in `appended_rules`; **spy on the mock router's `generation`-tier call for draft B and assert the resolved system message contains R verbatim** (proves the rule reaches the prompt, not just the DB).

### Modified

8. **`app/draft/templates/registry.py`** — add:
   ```python
   async def append_rules(
       self, template_id: str, new_rules: list[str], *, session: AsyncSession
   ) -> DraftTemplate:
       """Create a new TemplateVersion with appended_rules extended.
       Dedups new_rules against current appended_rules by exact (post-strip) string match.
       If post-dedup new_rules is empty -> returns existing latest unchanged (no INSERT).
       Recomputes prompt_fingerprint via DraftTemplate.compute_fingerprint().
       Invalidates self._cache.pop(template_id, None) on successful INSERT.
       Does NOT commit; caller owns the transaction.
       """
   ```
   The exact-string dedup is defense-in-depth — cosine dedup happens upstream in `RuleExtractor._is_similar`. Cache invalidation is critical: without it, the next `get_latest()` in the same process returns stale data and the rubric demo test fails.

9. **`app/jobs/worker.py`** — in `_main`, after the `_reconcile_loop` task is scheduled, instantiate `RuleExtractorScheduler(session_factory=..., lock_session_factory=direct_session_factory, ...)` and add `scheduler.start()` to the startup path + `scheduler.stop()` to the shutdown path. The scheduler is owned by the worker process only — verified by inspection of `app/main.py` (no scheduler import in API lifespan). On shutdown, `scheduler.stop(wait=True)` lets any in-flight `_tick` finish so the advisory lock releases cleanly.

10. **`app/api/routes/admin.py`** — add three endpoints:
    - `POST /api/admin/rule-extractor/run` — body `RuleExtractorRunRequest`; if `template_id` provided, validate it exists in `app.templates` (else 404); else `_list_known_template_ids(session)`. For each target, call `RuleExtractor(...).run(tid, trace_id=current_trace_id())`. Enforce `RULE_EXTRACTOR_ADMIN_MAX_DURATION_S=180` wall-clock budget; on exceed, set `partial=True` and break. `await session.commit()` once at the end. Skipped rows (locked) are returned in the response, not 409; only **single-template + locked** returns 409 with `RULE_EXTRACTOR_BUSY` + `Retry-After: 60`.
    - `GET /api/admin/templates` — `SELECT DISTINCT ON (template_id) template_id, version, prompt_fingerprint, jsonb_array_length(appended_rules) FROM app.templates ORDER BY template_id, version DESC` joined to `template_extractor_state` for `last_run_at`.
    - `GET /api/admin/templates/{template_id}/versions` — all rows ordered by version ASC; per-row `rules_added_vs_previous = list(set(v.appended_rules) - set(prev.appended_rules))`.

11. **`app/api/deps.py`** — `async def get_rule_extractor(session, registry, router, embedder, settings) -> AsyncIterator[RuleExtractor]` factory used by the admin route. Yields with both `session` (FastAPI's pgbouncer-routed dependency) and a freshly-opened `lock_session` from `direct_session_factory()`, closing the lock session on teardown. `get_template_registry` and `get_embedder` already exist. **Also add `direct_session_factory`** to `app/db/session.py` alongside the existing pgbouncer-routed factory (lazy singleton; `pool_size=2, max_overflow=0` — only the scheduler tick + admin endpoint use it).

12. **`app/core/errors.py`** — `class RuleExtractorError(AppError)` with codes: `RULE_EXTRACTOR_BUSY` (409, retryable=True), `TEMPLATE_NOT_FOUND` (404, retryable=False), `LLM_ANALYSIS_FAILED` (502, retryable=True). Parse failures are NOT errors — `_parse_rule` returns `None`, group is skipped, run continues.

13. **`app/settings.py`** — add eight settings, defaults inline above: `RULE_EXTRACTOR_INTERVAL_HOURS=6`, `RULE_EXTRACTOR_MIN_EDITS=3`, `RULE_EXTRACTOR_SIMILARITY_THRESHOLD=0.88`, `RULE_EXTRACTOR_MAX_EDITS_PER_GROUP=12`, `RULE_EXTRACTOR_FIRST_RUN_LOOKBACK_DAYS=30`, `RULE_EXTRACTOR_VERSION_LOOKBACK=2`, `RULE_EXTRACTOR_ADMIN_MAX_DURATION_S=180`, `RULE_EXTRACTOR_LOCK_ID=9_010_001`.

14. **`pyproject.toml`** — add `apscheduler>=3.10,<4`.

### Untouched

`app/edits/service.py`, `app/edits/few_shot_store.py`, `app/edits/diff.py` (M9 surface, read-only consumer of `app.edits`). `app/draft/engine.py`, `extractor.py`, `generator.py` (engine's existing `registry.get_latest()` picks up new versions automatically — verified by the rubric test). `app/jobs/queue.py`, `app/jobs/kinds.py` (`RULE_EXTRACTION` kind stays reserved-but-unused).

## Analysis prompt — frozen

System (deterministic, no interpolation):
```
You are a careful technical editor reviewing how an experienced legal operator
corrects an AI draft. Your task is to extract ONE generalizable, instruction-shaped
rule that explains a consistent edit pattern across multiple cases, or to declare
that no consistent pattern exists.

Output JSON with exactly this shape:
{"rule": "<one sentence imperative, OR the literal string NO_RULE>",
 "evidence_count": <integer: cases the rule applies to>,
 "rationale": "<one sentence>"}

Output the JSON object and nothing else. No prose, no markdown fences.
```

User:
```
Field or section: {name}
Type: {field_type}

Cases ({n}):
1. AI produced: {ai_repr_1}
   Operator changed to: {user_repr_1}
2. ...

Look for a CONSISTENT pattern. The rule must be imperative (e.g., "When listing
parties, always include their role in parentheses."). If fewer than {min_evidence}
of the cases support a single rule, return NO_RULE.
```

`schema=RULE_EXTRACTION_SCHEMA` is passed to the router so the analysis tier uses structured-output mode where supported. JSON parse failure → log + skip group (NOT raise).

## Acceptance checks (mapped to spec)

- [ ] Both scheduler tick and `POST /api/admin/rule-extractor/run` trigger the extractor (covered by `test_rule_extractor_via_admin.py` + the worker wiring exercised in `test_rule_extractor_advisory_lock.py` setup).
- [ ] New rule → new `TemplateVersion` row → new `prompt_fingerprint`, observable in DB (`test_rule_extractor_basic.py` steps 5–7).
- [ ] Re-running on the same edit set with no new patterns is a no-op (`test_rule_extractor_idempotent.py` — second call has `new_rules=[]`, `app.templates` count unchanged).
- [ ] Next draft uses the new rules (`test_rule_applied_to_next_draft.py` — rule string verbatim in draft B's resolved system message).
- [ ] APScheduler / extractor run only once across workers (`test_rule_extractor_advisory_lock.py`), with the lock-session routed through `DATABASE_URL_DIRECT` so pgbouncer transaction pooling can't silently drop it.
- [ ] No-pattern case gracefully handled (`test_rule_extractor_no_pattern.py`) with one `event="rule_extractor.no_rule"` log line per group.
- [ ] Min-edits threshold respected (`test_rule_extractor_min_edits.py` — no LLM call when n<3; `groups_skipped_min_edits` increments).
- [ ] Version-floor filter pins which edits are considered (`test_rule_extractor_version_lookback.py`).
- [ ] NN-5: append goes through `compute_fingerprint()`, drafts pick up new version via existing `get_latest()`.
- [ ] NN-9: dual trigger + idempotent (similarity dedup + exact-string dedup in `append_rules`).
- [ ] NN-12: structlog `event=rule_extractor.*` keys; LLM `analysis` calls logged to `llm_log.llm_requests` by the router (free); admin endpoint uses request-scoped `current_trace_id()`; scheduler tick uses a generated uuid7.
- [ ] `make test` green; new unit + integration tests added; M9 / M8 / M7 / M2 regression suites pass unchanged.
- [ ] No advisory locks remain held after test runs (`SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=9010001` returns 0).

## Risks / mitigations

- **YAML reload clobbers extracted rules** (the §"Current state" footgun): documented operational note in `M10-DONE.md`; real fix is splitting `appended_rules` into `yaml_rules + extracted_rules` columns — follow-up refactor, not blocking M10. Single-operator demo bounds the blast radius.
- **Analysis model hallucinates a rule from 3 unrelated edits:** the model's own `evidence_count < min_evidence` self-check is the primary defense; cosine dedup catches near-duplicates on subsequent runs; operator can inspect via `GET /api/admin/templates/{id}/versions`. No human-approval flow in v1 (per spec).
- **Advisory lock leaked on worker SIGKILL:** session-scoped lock on the **direct** connection (not pgbouncer) auto-releases when Postgres detects the dropped client. The lock-session test verifies no held locks via the direct URL.
- **pgbouncer transaction pooling silently drops session-scoped advisory locks** (the real issue the M9 plan glossed over): mitigated by routing the lock-holding session through `DATABASE_URL_DIRECT`. `app/db/session.py` gains `direct_session_factory` (pool size 2, no overflow); only the scheduler tick and admin endpoint use it. Data reads/writes continue through pgbouncer as before. `test_rule_extractor_advisory_lock.py` asserts the lock is actually held — running it through pgbouncer instead of direct would have masked the original bug.
- **Both APScheduler in-process guard AND advisory lock deny simultaneously:** means the previous tick is stuck. Surfaces as repeated `event="rule_extractor.lock_busy"` log lines — alerting is M12 eval territory.
- **Mock router returns malformed JSON in real provider runs:** `_parse_rule` is defensive (returns `None`, logs warning); router has `schema_retry` for the `analysis` tier (`router.py:119,159`). Failure mode is "no rule this run", not crash.
- **Rule prompt grows unbounded across many runs:** after ~50 rules the system prompt is still well under the analysis-tier context window. Pruning is v2.
- **Concurrent admin + scheduler:** admin wins (synchronous, lock-acquiring); scheduler sees `skipped_reason="locked"`, logs, moves on. Acceptable.

## Out of scope (per spec)

- Real fine-tuning / DPO.
- Rule deletion / human-approval-before-applying flow.
- Cross-template rule generalization.
- Pruning stale rules.
- Splitting `appended_rules` into `yaml_rules + extracted_rules` (follow-up).
- Rate-limiting the admin endpoint (single operator; lock suffices).

## Definition of done

- All acceptance checks tick.
- `docs/milestones/M10-DONE.md` written with: deviations from plan, the migration-numbering drift vs. the spec (`0002_…` → `0006_…`), a sample admin-endpoint JSON showing one extraction with a real rule, the resolved system prompt for the next-generated draft showing the rule appended verbatim (copy-pasted from `GET /api/admin/templates/{id}/versions[].resolved_system_prompt`), and the operational note about YAML reload + extracted rules.
