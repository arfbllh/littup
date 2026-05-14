# M9-DONE — Edit Capture + Few-shot Store

## What shipped

### Schema and types

- **`app/db/migrations/versions/0005_edits_few_shot_index.py`** — Partial HNSW index on `app.edits.embedding` (only indexed edits participate):
  ```sql
  CREATE INDEX edits_embedding_hnsw_idx ON app.edits
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE few_shot_indexed_at IS NOT NULL;
  ```

- **`app/edits/diff.py`** — Pure diff engine:
  - `StructuredDiff` frozen dataclass: `fields: dict[str, dict]`, `sections: dict[str, dict]`, `is_empty()`
  - `compute_diff(template, ai_output, user_output)` — top-level dispatcher
  - `compute_field_diff(field_spec, ai_value, user_value)` — scalar replace, list[string] set-diff, list[party] identity-by-name.strip().lower()
  - `compute_section_diff(section_name, ai_text, user_text)` — NFC normalize → `SequenceMatcher` on `re.findall(r"\S+|\s+", text)` tokens; returns `char_changes` opcode list plus full ai/user texts
  - Returns `None` when values are deep-equal (no diff entry written)

- **`app/core/errors.py`** — Added `EditError(AppError)`:
  - `EDIT_PAYLOAD_INVALID` → 400
  - `EDIT_DRAFT_NOT_READY` → 409
  - `EMBEDDER_DIM_MISMATCH` → 500, `retryable=False`
  - `FEW_SHOT_INDEX_FAILED` → retryable

- **`app/api/schemas/edits.py`** — `EditCreateRequest`, `EditMetricFieldRow`, `EditMetricSectionRow`, `EditMetricsResponse`

- **`app/settings.py`** — `WORKER_CONCURRENCY_FEW_SHOT=4`, `FEW_SHOT_TOP_K=3`, `FEW_SHOT_INDEX_MAX_ATTEMPTS=5`, `EDIT_METRICS_DEFAULT_DAYS=30`

### Few-shot store + job handler

- **`app/edits/few_shot_store.py`** — Core few-shot machinery:
  - `FewShotResult` frozen dataclass: `edit_id`, `ai_value`, `user_value`, `context_tags`, `distance`
  - `FewShotStore(embedder)` — dim check at `__init__`; `index(edit_id, *, session)` and `retrieve(template_id, field_name, *, session, field_type, chunk_context, top_k)`
  - `_search_repr(...)` — symmetric pure function used at both index and query time; per-line truncation at 1500 chars, block at 3500 chars; section values truncated to 800 chars
  - `index()` idempotent via `WHERE few_shot_indexed_at IS NULL` on the UPDATE; concurrent-safe
  - `retrieve()` issues `SET LOCAL work_mem='64MB'; SET LOCAL statement_timeout='5s'; SET LOCAL hnsw.ef_search=100` inside `begin_nested()` before the cosine-distance ORDER BY query (NN-4)
  - `chunks_to_context(chunks)`, `_render_field_few_shot(examples)`, `_render_section_few_shot(examples)` — prompt-format helpers owned by this module

- **`app/jobs/handlers/few_shot_index.py`** — `handle_few_shot_index(payload, session)` registered into `HANDLERS[JobKind.FEW_SHOT_INDEX]`:
  - Loads `Edit` by id; skips if already indexed (idempotent)
  - Embeds `_search_repr` (no chunk_context at index time)
  - `UPDATE ... WHERE few_shot_indexed_at IS NULL` — race-safe against reconciler
  - Does not commit; worker owns the transaction (NN-1)

- **`app/jobs/handlers/__init__.py`** — Added `from app.jobs.handlers import few_shot_index` for registration side-effect

### Edit service + routes

- **`app/edits/service.py`** — `EditService(session, queue, registry)`:
  - `save_edit(draft_id, user_output, *, trace_id)` — `SELECT … FOR UPDATE` on Draft; validates payload against template schema; computes `merged_final_output`; writes one `Edit` row per changed field/section with `template_version` + `prompt_fingerprint` copied from the parent draft (NN-5); updates `draft.status='edited'`; enqueues `FEW_SHOT_INDEX` in the same transaction (NN-1)
  - No-change path: skips Edit rows + jobs, still marks `status='edited'` with `edited_at`; returns `SavedEditResult(edit_ids=[], skipped_reason="no_changes")`
  - `metrics(template_id, *, days=30)` — window-scoped draft count, per-field/section edit counts, `edit_rate = count/drafts_count` (None when `drafts_count=0`)

- **`app/api/routes/edits.py`** — `POST /api/drafts/{draft_id}/edit` → `DraftResponse`; `GET /api/templates/{template_id}/edit-metrics` → `EditMetricsResponse`

- **`app/api/schemas/drafts.py`** — Added `DraftResponse.edit_count: int = 0`

- **`app/api/routes/drafts.py`** — Extracted `_build_draft_response(draft_id, session)` helper that populates `edit_count`; widened status gate to `status NOT IN ('ready', 'edited')` for regenerate

- **`app/main.py`** — `application.include_router(edits_router)`

### Reconciler + worker wiring

- **`app/jobs/reconciler.py`** — `reconcile_unembedded_edits()` enqueues `FEW_SHOT_INDEX` with `dedup_key=f"few_shot_index:{edit_id}"` for any edit with `few_shot_indexed_at IS NULL`; same dedup_key as the primary path so the queue's `ON CONFLICT DO NOTHING` prevents duplicates (NN-11)

- **`app/jobs/worker.py`** — `_reconcile_loop` calls `reconcile_unembedded_edits()` each 60s tick alongside `reclaim_stuck_jobs`

### Generator + extractor wiring

- **`app/draft/extractor.py`** — `FieldExtractor.__init__` accepts `few_shot_store=None`, `current_template_id=None`, `session=None`; `_extract_field` appends `_render_field_few_shot` block to user message when examples retrieved (None defaults preserve all existing tests)

- **`app/draft/generator.py`** — `SectionGenerator.__init__` accepts same three params; `generate_section` prepends `_render_section_few_shot` block before `extra_instructions`; removed the never-populated `few_shot=[]` parameter from `generate_all` signature

- **`app/draft/engine.py`** — Constructs `FewShotStore(embedder=embedder)` once at init (dim-check at boot); opens a dedicated `gen_session` spanning field extraction + section generation so few-shot retrieval reuses the same connection; retry `SectionGenerator` gets its own `retry_session`; `regenerate_section()` gains `session: AsyncSession | None` kwarg with `_nullctx` helper for route-provided sessions

- **`app/api/deps.py`** — `get_draft_engine()` calls `await get_embedder()` and passes `embedder=embedder` to `DraftEngine`

### Tests

| Suite | File | Tests |
|-------|------|-------|
| Unit | `tests/unit/test_diff.py` | 15 diff cases (scalar, list[string], list[party], section NFC, is_empty) |
| Unit | `tests/unit/test_few_shot_search_repr.py` | 12 cases (truncation, party render, purity, chunk_context gating) |
| Integration | `tests/integration/test_edit_save.py` | Edit rows created, NN-5 fingerprint stored, jobs enqueued; merged final_output |
| Integration | `tests/integration/test_edit_save_noop.py` | No-change path: zero rows, zero jobs, draft marked edited |
| Integration | `tests/integration/test_few_shot_index_handler.py` | Embeds + stamps, idempotent, dim-mismatch raises at construction |
| Integration | `tests/integration/test_few_shot_retrieval.py` | Ranked order, template filter, unindexed filter, empty result |
| Integration | `tests/integration/test_few_shot_reconcile.py` | Reconciler enqueues, deduplicates, worker indexes |
| Integration | `tests/integration/test_edit_metrics.py` | Per-field rates, null rates when drafts_count=0, all fields/sections present |
| Integration | `tests/integration/test_loop_closes_stage1.py` | **Rubric demo**: edit indexed → draft B extraction prompt contains corrected value → ai_output reflects correction |

---

## Deviations from plan

- **`FewShotResult` vs `FewShotExample`**: named `FewShotResult` in `few_shot_store.py` to avoid collision with the existing `FewShotExample` Pydantic model in `app/api/schemas/schema.py` (template config).
- **`generate_all` signature**: plan §1.9 says remove `few_shot=[]` param — done. No callers were passing it (M7 already left it empty).
- **`backfill_unindexed` method**: dropped as specified in §3.3 — reconciler owns that responsibility entirely.

---

## NN compliance

| NN | How honored |
|----|-------------|
| NN-1 | `FEW_SHOT_INDEX` runs as a Postgres-backed job; `save_edit` enqueues in the same transaction as the `Edit` INSERT — no orphaned jobs or edits |
| NN-3 | `WORKER_CONCURRENCY_FEW_SHOT=4` semaphore (embedding-bound); main concurrency path unchanged |
| NN-4 | `retrieve()` sets `work_mem='64MB'`, `statement_timeout='5s'`, `hnsw.ef_search=100` in a `begin_nested()` savepoint before the cosine query |
| NN-5 | Every `Edit` row tagged with `template_version` + `prompt_fingerprint` from the parent Draft at save time |
| NN-7 | Few-shot block is part of the resolved `messages` list → participates in the LLM content-addressed cache key automatically. Side-effect: cache hit rate on field/section calls drops to near-zero after any edit is indexed for that template+field (desired — the point of stage-1 is that the next call differs). Operators should not interpret this as a regression in `/admin/llm-stats`. |
| NN-11 | `few_shot_indexed_at` set atomically; reconciler re-enqueues failed embeddings; dedup_key shared between primary path and reconciler prevents duplicate jobs; `max_attempts=5` enforces the backoff cap |
| NN-12 | `EditService`, `FewShotStore`, and the handler use `structlog` with `event=` keys; `EditError` extends `AppError`; embedder calls flow through the existing logged `LLMRouter` path |

---

## Cache-hit note (NN-7 implication)

When a `FewShotStore.retrieve()` call returns at least one example, the injected block changes the user message, which changes the LLM cache key (`sha256(model_id + messages + ...)`). This is intentional — the improvement loop works precisely because the model sees different in-context examples per draft. However, it means field/section extraction cache-hit rates in `llm_log.llm_requests` will fall substantially for any template+field that has indexed edits. This is not a performance regression; it is the expected trade-off. The cache still serves fully identical prompts (e.g. drafts generated before any edits were saved).

---

## Loop-closes sample (test output)

```
tests/integration/test_loop_closes_stage1.py::test_stage1_loop_closes PASSED
```

Step-by-step:
1. Template `case_fact_summary` synced; draft A seeded with `parties=[{"name":"Smith"}]`
2. `EditService.save_edit` → 1 Edit row, `status='edited'`, 1 `FEW_SHOT_INDEX` job
3. `FewShotStore.index` → `embedding` set, `few_shot_indexed_at` stamped
4. Draft B generated; `FieldExtractor._extract_field` for `parties` retrieves the indexed edit
5. Mock router sees `"Smith, et al."` in the extraction prompt → returns corrected value
6. `DraftRepo.finalize` writes `ai_output["fields"]["parties"]["value"] = [{"name": "Smith, et al."}]`
7. Assertions pass: prompt contains `"Smith, et al."` ✓ ; DB value matches ✓
