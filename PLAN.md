# M9 — Edit Capture + Few-shot Store — PLAN

**Owner branch:** `feature/m9-edit-capture-few-shot`
**Depends on:** M1 (queue, reconciler, edits table, vector ext), M5 (embedder), M6 (retrieval session/work_mem patterns), M7 (DraftEngine), M8 (validator + groundedness persistence).
**Rubric impact:** Stage-1 improvement loop — front half of the 25-pt "Improvement from Edits" bucket.

This plan closes M9 with **zero open questions**. Every behavior, error path, schema column, index, and test is specified before any code is written. M10 (rule extractor — Stage 2) is explicitly out of scope; the wiring it depends on (`prompt_fingerprint` tagging, `Edit.context.few_shot_examples`) is in scope.

---

## 0. Non-Negotiables touched

| NN | How this milestone honors it |
|----|------------------------------|
| **NN-1** | Edit indexing runs as a `FEW_SHOT_INDEX` job on the existing Postgres queue (no `BackgroundTasks`). |
| **NN-3** | Per-kind semaphore for `FEW_SHOT_INDEX` (`WORKER_CONCURRENCY_FEW_SHOT=4`, embedding-bound), default backpressure unchanged. |
| **NN-4** | `app.edits.embedding` already `VECTOR(1024)`; this milestone adds the HNSW index. Retrieve sets `SET LOCAL work_mem='64MB'; SET LOCAL statement_timeout='5s'; SET LOCAL hnsw.ef_search=100;`. |
| **NN-5** | Every `Edit` row is tagged with **both** `template_version` and `prompt_fingerprint` copied from the parent `Draft` at save time. Few-shot retrieval filters by `template_id + field_or_section_name`; the `prompt_fingerprint` is preserved as a downstream grouping key for M10 (extractor groups by fingerprint). The draft engine **does not** re-query the registry — generator/extractor receive the already-snapshotted template. |
| **NN-7** | The injected few-shot block becomes part of the resolved `messages`, so it participates in the content-addressed LLM cache key automatically. No special handling required. *Implication:* because the block changes per-draft (different retrieved examples), cache hits on field/section calls effectively drop to zero once any edit is indexed. This is the desired behavior — the whole point of stage-1 is that the next call differs — but it must not be reported as a regression in `/admin/llm-stats`. The doc note in M9-DONE.md should call this out. |
| **NN-11** | Edits get an immediate `FEW_SHOT_INDEX` job on save; reconciler's `find_unembedded_edits()` (currently a stub returning IDs) is wired to **re-enqueue** with idempotent `dedup_key=few_shot_index:{edit_id}`. Job's `max_attempts=5` enforces exponential backoff via existing queue logic. |
| **NN-12** | `EditService`, `FewShotStore`, and the new routes use `structlog` with `event=` + `request_id`; new typed error class `EditError` extends `AppError`. LLM/embedder calls flow through existing logged paths. |

---

## 1. Architecture decisions (locked, not open)

### 1.1 Edit row granularity — **one row per changed field, one row per changed section**

A single `POST /api/drafts/{id}/edit` produces N rows, where N = number of fields whose value diff is non-trivial + number of sections whose text diff is non-trivial.

Reasons it must be per-field/section, not per-draft:
- Few-shot retrieval is keyed by `field_or_section_name` (per spec §"Few-shot store"). A row per draft would require splitting at retrieve time.
- Edit-metrics endpoint computes per-field edit rate. A row per draft would require parsing the JSON diff at metrics time.
- Embeddings represent **one** edit instance, not a bundle. Mixing fields into one vector destroys retrieval signal.

A field with no actual change → **no row written**.
A draft with zero changes → no rows, `Edit` count returns 0, but the draft's `final_output`, `edited_at`, `status='edited'` **are still updated** to record the operator's acceptance ("approve-as-is"). This is in `tests/integration/test_edit_save_noop.py`.

> **Status enum note:** Draft `status` today is one of `generating | ready | failed` (see `app/draft/draft_repo.py`). M9 introduces a new terminal value `'edited'` written by `EditService.save_edit`. There is no enum/migration to add (column is `TEXT`); the only consumer change is `app/api/routes/drafts.py:166` which currently rejects regenerate when `status != 'ready'` — this check is widened to `status NOT IN ('ready','edited')` so an operator can regenerate a section after editing.

### 1.2 Diff semantics — frozen here, not inferred at code-write time

#### Field diff (`compute_field_diff`)

| Field type | Operation | Diff payload |
|------------|-----------|--------------|
| `string` / `date` / `money` | scalar replace if `ai != user` (after `str.strip()`) | `{"ai": ai, "user": user, "operation": "modified"}` |
| `list[string]` | order-insensitive set diff, case-sensitive | `{"ai": ai_list, "user": user_list, "operation": "modified", "added_items": [...], "removed_items": [...]}` |
| `list[party]` | identity = `party["name"].strip().lower()`; preserves full object in added/removed lists | same shape as `list[string]` |
| any | `ai is None and user is not None` | `operation="added"` |
| any | `ai is not None and user is None` | `operation="removed"` |
| any | values equal | **no diff returned (sentinel `None`)** |

JSONB equality uses Python `==` after both sides are deserialized; that is sufficient because `Draft.ai_output` is already JSONB-deserialized SQLAlchemy returns and `body.final_output` is parsed by FastAPI from JSON.

#### Section diff (`compute_section_diff`)

- Both `ai_text` and `user_text` are normalized: `unicodedata.normalize("NFC", ...).strip()`.
- If equal → no diff returned.
- Otherwise: `difflib.SequenceMatcher(None, ai_tokens, user_tokens)` where tokens = `re.findall(r"\S+|\s+", text)` (preserves whitespace). Opcodes are filtered to `replace|delete|insert`; each becomes a `char_changes` entry: `{"op": op, "ai": " ".join(ai_tokens[i1:i2]), "user": " ".join(user_tokens[j1:j2])}`.
- Full payload: `{"section": name, "ai_text": ..., "user_text": ..., "char_changes": [...]}`. The full texts are included because that is what the embedding and prompt injection use; `char_changes` is the structured representation used by M10.

#### Top-level shape

```python
@dataclass
class StructuredDiff:
    fields: dict[str, dict]       # field_name -> diff payload (only changed fields)
    sections: dict[str, dict]     # section_name -> diff payload (only changed sections)

    def is_empty(self) -> bool:
        return not self.fields and not self.sections
```

### 1.3 Embedder dimension — locked to 1024

`app.edits.embedding` is `VECTOR(1024)`. The `BGEEmbedder` produces 1024-dim vectors. The `OpenAIEmbedder` produces 1536. The chunks table is also 1024-dim today.

Decision: **fail fast with a typed error** at `FewShotStore.index()` if `embedder.dim != 1024`, rather than silently casting or migrating the schema. The error code is `EMBEDDER_DIM_MISMATCH`; the job is marked non-retryable. This surfaces the misconfiguration loudly to the operator in `/admin/llm-stats` rather than burying it.

Rationale for not adding a migration to bump dim: chunk embeddings are already 1024, so OpenAI was never the runtime default. Keeping the constraint in one place (an explicit check) is simpler than two migrations. M10's eval also assumes 1024.

### 1.4 Vector index on edits

Add `app.edits_embedding_hnsw_idx`:

```sql
CREATE INDEX edits_embedding_hnsw_idx ON app.edits
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE few_shot_indexed_at IS NOT NULL;
```

Partial — only indexed edits participate. This is also the filter the retriever applies, so the index covers the hot path. `m`/`ef_construction` match `chunks_embedding_hnsw_idx` for consistency.

Migration `0005_edits_few_shot_index.py`. Downgrade drops the index.

### 1.5 Few-shot search representation — frozen

Inputs to the embedder for indexing **and** retrieval are constructed by the same pure function `_search_repr(field_or_section_name, field_type, ai_value, user_value, context_tags) -> str` so they are symmetric:

```
[FIELD case_caption | section]
AI: <ai_value rendered>
USER: <user_value rendered>
TAGS: <comma-joined context tags>
```

Rendering rules:
- Scalars → `str(v).strip()`
- Lists → `"; ".join(stringify(item) for item in v)` (parties rendered as `"name (role)"`)
- Section text → first 800 chars (BGE max chunk is 512 tokens ≈ 1500 chars; section AI text is typically that long, plus user text)
- Each line is truncated to 1500 chars; the full block is truncated to 3500 chars before embedding

`context_tags` is the small bag of categorical signal: `template_id`, `field_type`, `document_id` (first), and (optionally) per-template hints. v1 just emits `template_id` + `field_type` — that is enough for the retrieval filter to also act on `template_id + field_or_section_name`, and the tag is also useful for the M10 extractor.

### 1.6 Retrieval query — `FewShotStore.retrieve()`

```
SET LOCAL work_mem = '64MB';
SET LOCAL statement_timeout = '5s';
SET LOCAL hnsw.ef_search = 100;

SELECT id, ai_value, user_value, context, prompt_fingerprint,
       (embedding <=> CAST(:qvec AS vector)) AS distance
FROM app.edits
WHERE template_id = :tid
  AND field_or_section_name = :name
  AND few_shot_indexed_at IS NOT NULL
ORDER BY embedding <=> CAST(:qvec AS vector)
LIMIT :k
```

- `CAST(:qvec AS vector)` matches the existing pgvector binding pattern in `app/retrieval/dense.py:37` (parameter is bound as a Python list of floats and cast server-side).
- `:qvec` is built from the **current draft context** (see §1.7).
- Cosine distance via `<=>`.
- Rows where `embedding IS NULL` are excluded by the `few_shot_indexed_at IS NOT NULL` filter (the index call sets both atomically).
- No score threshold in v1; `top_k=3` caps the noise.

Returned shape:

```python
@dataclass(frozen=True)
class FewShotExample:
    edit_id: str
    ai_value: Any
    user_value: Any
    context_tags: list[str]
    distance: float
```

### 1.7 Query-side representation for retrieval

The generator/extractor calls retrieve **once per (field|section, draft)**. The query embedding is built by calling the same `_search_repr` function as indexing, but with these query-time inputs:

- For a field: `ai_value="<NEW DRAFT>"` (literal sentinel string), `user_value=""`, tags = `[template_id, field_type]`, plus an optional `chunk_context` parameter — a single concatenated string of the top 3 retrieved chunks' text (each truncated to 600 chars). When `chunk_context` is present, `_search_repr` appends a `CONTEXT: <chunk_context>` line at the end (truncated to the same 1500-char per-line / 3500-char total limit).
- For a section: same shape; the chunks come from the section's retrieval-key result set.

> **On asymmetry:** §1.6 is only "symmetric" in the sense of *function reuse* — both call sites go through `_search_repr` so the embedding manifold stays consistent. The *inputs* are deliberately different: indexing has the actual ai/user pair (the edit signal), retrieval has a placeholder + chunk context (a proxy for "this is the kind of situation"). This is a design choice, not a bug. Without the chunk context, retrieval-time queries would be near-identical across drafts of the same template+field and HNSW would degenerate. We are matching "kind of evidence + kind of field" to "kind of evidence + kind of field that was edited before," not "edit pattern to current context."

`chunk_context` is optional on `_search_repr` so unit tests can pin behavior with `chunk_context=None` (deterministic).

### 1.8 Prompt injection format — frozen

Injected at the **end** of the user-message of the existing extractor/generator prompts. Truncate each ai/user value to 600 chars; emit nothing if `examples == []`.

For sections (in `SectionGenerator.generate_section`, before `extra_instructions`):

```
Past edits relevant to this section (most similar first):

Example 1:
  AI draft was:
    """<ai_value truncated to 600 chars>"""
  Operator changed it to:
    """<user_value truncated to 600 chars>"""

Example 2:
  ...

Apply these patterns when relevant. Do not invent citations; cite only chunks shown above.
```

For fields (in `FieldExtractor._extract_field`):

```
Past operator corrections for this field:

Example 1:
  Previous extraction: <ai_value JSON>
  Operator corrected to: <user_value JSON>
  
Prefer the corrected form when the document supports it.
```

The block contributes to the LLM cache key automatically (NN-7).

### 1.9 Generator/Extractor wiring

`DraftEngine.generate()` constructs a `FewShotStore` instance once (it takes the embedder + session_factory; both already available to the engine via `deps`). Engine passes it down:

- `FieldExtractor.__init__(self, router, few_shot_store=None, current_template_id=None)` — None preserves the existing test surface.
- `SectionGenerator.__init__(self, router, few_shot_store=None, current_template_id=None)` — same.
- The existing `few_shot: list` parameter on `SectionGenerator.generate_all` is removed (it was never populated). Engine no longer passes `few_shot=[]`.

This keeps few-shot retrieval inside the generation pass where the retrieved chunks already exist, instead of pre-fetching in the engine.

### 1.10 API surface

| Method | Path | Body | Response |
|--------|------|------|----------|
| `POST` | `/api/drafts/{draft_id}/edit` | `EditCreateRequest{final_output: dict}` | `DraftResponse` + `edit_count: int` (added field on response) |
| `GET` | `/api/templates/{template_id}/edit-metrics?days=30` | — | `EditMetricsResponse` |

`final_output` shape mirrors the AI output we already persist:

```json
{
  "fields": {"parties": [...], "case_caption": "...", ...},
  "sections": [{"name": "background", "text": "..."}, ...]
}
```

Validation: every key in `final_output["fields"]` must appear in `{f.name for f in template.extraction_schema}` (it's a `list[FieldSpec]`, not a dict); every `name` in `final_output["sections"]` must appear in `{s.name for s in template.sections}` (likewise a `list[SectionSpec]`). Extra keys/sections → `400 EDIT_PAYLOAD_INVALID`. Missing keys/sections are **allowed** — they are treated as unchanged from `ai_output` for diff purposes.

**Persistence of `final_output`**: the row's `draft.final_output` is written as the *merged* form — start from `ai_output`, override with the user's submitted values for any field/section the user included. This guarantees `final_output` is always a complete, renderable document regardless of how partial the request body was. The diff (and the `Edit` rows) are still computed from the *delta*, so a partial body does not produce phantom diffs for unchanged fields.

Edit-metrics shape:

```json
{
  "template_id": "case_fact_summary",
  "window_days": 30,
  "drafts_count": 41,
  "fields": [
    {"name": "parties", "edited_count": 28, "edit_rate": 0.683},
    ...
  ],
  "sections": [
    {"name": "background", "edited_count": 12, "edit_rate": 0.293},
    ...
  ]
}
```

`edit_rate = edited_count / drafts_count`, where `drafts_count = count(distinct draft_id) where draft.template_id=:tid and draft.created_at > NOW() - :days_interval`. If `drafts_count = 0`, all rates are `null` (not `0` — that misleads).

### 1.11 Error taxonomy

New typed errors in `app/core/errors.py`:

- `EditError(AppError)` — base
- code `EDIT_PAYLOAD_INVALID` → 400 (shape/schema mismatch)
- code `EDIT_DRAFT_NOT_READY` → 409 (draft status not in `{ready, edited}`)
- code `EMBEDDER_DIM_MISMATCH` → 500, non-retryable, fired by indexer
- code `FEW_SHOT_INDEX_FAILED` → retryable (transient embedder issue)

### 1.12 Job-handler details

`FEW_SHOT_INDEX` payload: `{"edit_id": str}`. The handler is invoked by the worker with the worker-managed `session` (matching the convention used by `app/jobs/handlers/embedding.py`, `chunking.py`, etc.) and uses **that session** end-to-end — no new session_factory inside `FewShotStore.index`. The worker commits/rolls back the session after the handler returns.

Handler steps:
1. Load `Edit` by id from the worker's `session`; if missing → log warning and return (deleted)
2. If `few_shot_indexed_at IS NOT NULL` → log `event="few_shot.already_indexed"`, return success (idempotent)
3. Compute `_search_repr` (no `chunk_context` at index time) then `vec = (await embedder.embed([repr]))[0]`
4. `UPDATE app.edits SET embedding = CAST(:vec AS vector), few_shot_indexed_at = NOW() WHERE id = :id AND few_shot_indexed_at IS NULL` — guards against the race vs. reconciler. If `rowcount=0`, another worker indexed it concurrently; log and return success.
5. Worker commits the session (handler does **not** commit — matches the pattern in existing handlers).

Failure modes (Queue's retry decision uses `getattr(exc, "retryable", True)` per `app/jobs/worker.py:83`):
- `EMBEDDER_DIM_MISMATCH` → raise `EditError(retryable=False)`. Queue calls `JobQueue.fail(retryable=False)` which marks job `failed` immediately (no retry).
- Embedder transient (network / 5xx) → raise `EditError(retryable=True)`. Queue retries up to `max_attempts=5`; failed attempts increment `attempts` and re-set `status='pending'` until cap (see `app/jobs/queue.py:136-162`). Backoff is the queue's existing logic — M9 does not add new backoff machinery.

### 1.13 Reconciler integration

`find_unembedded_edits()` already returns IDs. Wire a new method `reconcile_unembedded_edits()` that calls it, then for each ID enqueues `FEW_SHOT_INDEX` with `dedup_key=f"few_shot_index:{edit_id}"`. The dedup_key matches `EditService.save_edit`'s enqueue dedup_key, so the primary path and the safety-net path are de-duplicated by the queue's existing `ON CONFLICT (dedup_key) DO NOTHING`.

Call site: `app/jobs/worker.py::_reconcile_loop`, alongside `reclaim_stuck_jobs` and `find_partial_documents`. Same 60s tick.

### 1.14 What we are deliberately **not** doing in M9

- **Cross-template signal sharing.** Filter is exact `template_id`. Cross-template requires similarity over template metadata; out of v1.
- **Per-fingerprint filtering at retrieve time.** We tag edits with `prompt_fingerprint` (for M10) but do not filter on it during retrieve — too aggressive, would lose most signal in a single-operator demo where fingerprints rarely change. M10's *rule extractor* groups by fingerprint, which is what NN-5 actually requires.
- **Score threshold / MMR diversification.** `top_k=3` from cosine is enough. Diversification = M12 if needed.
- **UI for edits.** M11 owns it.
- **Privacy/PII scrubbing of edits.** Out of v1.
- **Persistent rule extraction.** M10.

---

## 2. Files to create / modify

### 2.1 Create

| Path | Purpose |
|------|---------|
| `app/db/migrations/versions/0005_edits_few_shot_index.py` | HNSW partial index on `app.edits.embedding` |
| `app/edits/diff.py` | `compute_diff`, `compute_field_diff`, `compute_section_diff`, `StructuredDiff` dataclass |
| `app/edits/service.py` | `EditService.save_edit`, `EditService.metrics` |
| `app/edits/few_shot_store.py` | `FewShotStore.index`, `.retrieve`, `.backfill_unindexed`, `_search_repr`, `FewShotExample` |
| `app/jobs/handlers/few_shot_index.py` | `handle_few_shot_index` registered into `HANDLERS` |
| `app/api/schemas/edits.py` | `EditCreateRequest`, `EditMetricsResponse`, supporting models |
| `app/api/routes/edits.py` | `POST /api/drafts/{id}/edit`, `GET /api/templates/{id}/edit-metrics` |
| `tests/unit/test_diff.py` | Field + section diff cases (see §4) |
| `tests/unit/test_few_shot_search_repr.py` | Symmetry / truncation of `_search_repr` |
| `tests/integration/test_edit_save.py` | save → row exists, status updated, job enqueued |
| `tests/integration/test_edit_save_noop.py` | empty diff path — no Edit rows, draft still marked edited |
| `tests/integration/test_few_shot_index_handler.py` | Handler embeds, sets `few_shot_indexed_at`, idempotent |
| `tests/integration/test_few_shot_retrieval.py` | 3 indexed edits, query returns ranked |
| `tests/integration/test_few_shot_reconcile.py` | Unindexed edit older than 1m → reconciler enqueues → worker indexes |
| `tests/integration/test_edit_metrics.py` | Per-field / per-section rates, `drafts_count=0` returns null |
| `tests/integration/test_loop_closes_stage1.py` | Rubric demo — see §4.4 |
| `docs/milestones/M9-DONE.md` | After-merge writeup |

### 2.2 Modify

| Path | Change |
|------|--------|
| `app/jobs/handlers/__init__.py` | Import `few_shot_index` for registration side-effect |
| `app/jobs/reconciler.py` | Add `reconcile_unembedded_edits()` that enqueues `FEW_SHOT_INDEX` jobs |
| `app/jobs/worker.py` | `_reconcile_loop` calls `reconcile_unembedded_edits()` each tick |
| `app/draft/generator.py` | `SectionGenerator.__init__` accepts `few_shot_store` + `current_template_id`; drop unused `few_shot` param from `generate_all` signature; inject few-shot block into user prompt |
| `app/draft/extractor.py` | `FieldExtractor.__init__` accepts `few_shot_store` + `current_template_id`; inject few-shot block into field extraction prompt |
| `app/draft/engine.py` | Construct `FewShotStore` from `embedder` (passed in via deps); pass to `FieldExtractor` and `SectionGenerator`; remove `few_shot=[]` arg from `generate_all` call (line 85); thread `session` keyword-arg through `generate` and `regenerate_section` |
| `app/api/deps.py` | `get_draft_engine` calls `await get_embedder()` and passes the embedder into `DraftEngine`. Engine constructs `FewShotStore(embedder=…)` internally — no session_factory plumbing |
| `app/api/routes/drafts.py` | `generate` and `regenerate_section` route handlers pass `session=session` into engine calls. Widen the `status != 'ready'` gate at line 166 to `status not in ('ready','edited')` so a section can be regenerated after editing. Extract `_build_draft_response(draft_id, session)` helper for re-use by the edits route |
| `app/api/schemas/drafts.py` | `DraftResponse.edit_count: int = 0` |
| `app/api/routes/drafts.py` (continued) | `_build_draft_response(draft_id, session)` populates `edit_count` via `SELECT COUNT(*) FROM app.edits WHERE draft_id=:id` and returns the same `DraftResponse` shape used today (extracted from existing `get_draft` body) |
| `app/main.py` | `application.include_router(edits_router)` |
| `app/core/errors.py` | `class EditError(AppError)` |
| `app/jobs/kinds.py` | (already has `FEW_SHOT_INDEX` enum value — no change) |
| `app/settings.py` | `WORKER_CONCURRENCY_FEW_SHOT: int = 4`; `FEW_SHOT_TOP_K: int = 3`; `FEW_SHOT_INDEX_MAX_ATTEMPTS: int = 5`; `EDIT_METRICS_DEFAULT_DAYS: int = 30` |

---

## 3. Module contracts (signatures and invariants)

### 3.1 `app/edits/diff.py`

```python
@dataclass(frozen=True)
class StructuredDiff:
    fields: dict[str, dict]
    sections: dict[str, dict]
    def is_empty(self) -> bool: ...

def compute_diff(
    template: "DraftTemplate",
    ai_output: dict,                  # draft.ai_output["fields"], ["sections"]
    user_output: dict,                # POST body
) -> StructuredDiff: ...

def compute_field_diff(
    field_spec: "FieldSpec", ai_value, user_value
) -> dict | None: ...

def compute_section_diff(
    section_name: str, ai_text: str, user_text: str
) -> dict | None: ...
```

Invariants:
- Pure functions, no IO.
- `user_output` keys missing → treated as unchanged from `ai_output` (no diff entry).
- `ai_output` keys missing entirely (older drafts that pre-date some template field) → treated as `None` baseline.
- `compute_field_diff` returns `None` iff values are deep-equal post-normalization.

### 3.2 `app/edits/service.py`

```python
class EditService:
    def __init__(self, session, queue, registry) -> None: ...

    async def save_edit(
        self, draft_id: str, user_output: dict, *, trace_id: str | None
    ) -> SavedEditResult: ...
    # SavedEditResult.edit_ids: list[str]; .draft: Draft; .skipped_reason: str | None

    async def metrics(
        self, template_id: str, *, days: int = 30
    ) -> EditMetrics: ...
```

`registry` here is the already-loaded `TemplateRegistry` instance (from `get_template_registry()`); `EditService` calls `await registry.get_by_version(draft.template_id, draft.template_version, session=self._session)` rather than `.get()` (the audit confirmed that's the actual method name).

`save_edit` transaction (single commit, owned by the route handler — service does not commit):
1. `SELECT ... FOR UPDATE` on `Draft`; raise `EditError(EDIT_DRAFT_NOT_READY, status_code=409)` if `status NOT IN ('ready', 'edited')`.
2. Load template via `registry.get_by_version(draft.template_id, draft.template_version, session=self._session)` to interpret field types.
3. Validate `user_output` shape against template (per §1.10) — raise `EditError(EDIT_PAYLOAD_INVALID, status_code=400)` on extra keys.
4. Compute `merged_final_output` = deep-merge of `draft.ai_output` overridden by `user_output` (sections matched by `name`).
5. `diff = compute_diff(template, draft.ai_output, user_output)` — note diff uses the *partial* user_output (so unchanged fields don't show up), not the merged form.
6. For each `(field_name, field_diff)`:
   - Resolve `ai_chunk_ids = draft.ai_output["fields"][field_name].get("supporting_chunk_ids", [])` (key may be missing on older drafts pre-M7 — fall back to `[]`).
   - Build `context = {"source_chunks": ai_chunk_ids, "retrieved_at": draft.generated_at.isoformat() if draft.generated_at else None, "model_used": draft.model_used}`.
   - Insert `Edit` row with `field_type='field'`, `field_or_section_name=field_name`, `ai_value={"value": ai_value}`, `user_value={"value": user_value}`, `diff=field_diff`, `template_id=draft.template_id`, `template_version=draft.template_version`, `prompt_fingerprint=draft.prompt_fingerprint`. Collect `edit_id`.
7. For each `(section_name, section_diff)`:
   - Resolve `ai_text` from `draft.ai_output["sections"]` by matching on `name`.
   - Resolve `section_chunk_ids` by querying `app.citations` joined to `app.sections` filtered by `(draft_id, section_name)` — this is the persisted truth (see `Section.citations` relationship in `app/db/models/draft.py:75`). Fall back to `[]` if the section had no citations.
   - Build `context` as above with `source_chunks=section_chunk_ids`.
   - Insert `Edit` row with `field_type='section'`, `ai_value={"text": ai_text}`, `user_value={"text": user_text}`, `diff=section_diff`, plus the same template/version/fingerprint copy.
8. `UPDATE app.drafts SET final_output=CAST(:fo AS jsonb), edited_at=NOW(), status='edited' WHERE id=:id` where `:fo = merged_final_output` (step 4).
9. For each `edit_id`: `await queue.enqueue("few_shot_index", {"edit_id": id}, dedup_key=f"few_shot_index:{id}", max_attempts=settings.FEW_SHOT_INDEX_MAX_ATTEMPTS)` — this runs against the same session, so the job is enqueued in the same transaction as the `Edit` insert. Either both commit or both roll back; no orphaned job referring to a missing edit, no orphaned edit without a job.

If `diff.is_empty()`: skip steps 6, 7, 9; still execute step 8 (so the draft is marked `edited` for "approve-as-is"). Return `SavedEditResult(edit_ids=[], skipped_reason="no_changes")`.

If any step raises, the route handler's `session.rollback()` undoes the whole transaction; no half-state.

### 3.3 `app/edits/few_shot_store.py`

```python
class FewShotStore:
    def __init__(self, embedder) -> None: ...

    async def index(self, edit_id: str, *, session: AsyncSession) -> None: ...

    async def retrieve(
        self,
        template_id: str,
        field_or_section_name: str,
        *,
        session: AsyncSession,
        chunk_context: str | None = None,
        top_k: int = 3,
    ) -> list[FewShotExample]: ...

def _search_repr(
    field_or_section_name: str,
    field_type: str,
    ai_value,
    user_value,
    context_tags: list[str],
    chunk_context: str | None = None,
) -> str: ...
```

Notes:
- `FewShotStore` no longer takes a `session_factory`. Both `index()` and `retrieve()` accept a session from the caller (the worker for `index`, the engine's session for `retrieve`). This matches the rest of the codebase, avoids opening N sessions per draft generation, and keeps transactional behavior obvious.
- The `backfill_unindexed` method is dropped — that responsibility lives entirely in the reconciler (§3.5). Centralizing it removes a duplicate code path.
- Convenience helper `chunks_to_context(chunks: list[Chunk]) -> str | None` (private at module level) builds the `chunk_context` string from the top-3 chunks (each truncated to 600 chars). Generator/extractor call it before invoking `retrieve`.

Invariants:
- `embedder.dim == 1024` checked at `FewShotStore.__init__`. Mismatch → raises `EditError(EMBEDDER_DIM_MISMATCH, retryable=False)` immediately. (Per audit: both `BGEEmbedder` and `OpenAIEmbedder` expose `.dim` as a class attribute, so the check is cheap and synchronous at construction.)
- `retrieve()` returns `[]` when no matches (never raises).
- `retrieve()` issues `SET LOCAL work_mem`, `SET LOCAL statement_timeout`, `SET LOCAL hnsw.ef_search` on the *passed-in* session before the SELECT. These bind to the current transaction only and don't leak.
- `index()` is idempotent on `few_shot_indexed_at IS NOT NULL` (UPDATE with WHERE clause guards the race).
- `index()` does not commit; the caller (handler) is responsible for transaction lifecycle.

### 3.4 `app/jobs/handlers/few_shot_index.py`

```python
async def handle_few_shot_index(payload: dict, session: AsyncSession) -> dict:
    edit_id = payload["edit_id"]
    embedder = await get_embedder()
    store = FewShotStore(embedder=embedder)
    await store.index(edit_id, session=session)
    return {"edit_id": edit_id, "status": "indexed"}

HANDLERS[JobKind.FEW_SHOT_INDEX] = handle_few_shot_index
```

The handler uses the worker-provided `session` end-to-end (matches the convention in `app/jobs/handlers/embedding.py` and the other M-handlers). The worker (see `app/jobs/worker.py`) commits the session after the handler returns and rolls back on raise. Because `FewShotStore` no longer opens its own session (per §3.3), there is exactly one transaction per job.

### 3.5 `app/jobs/reconciler.py`

Add:

```python
async def reconcile_unembedded_edits(self) -> int:
    ids = await self.find_unembedded_edits()
    if not ids:
        return 0
    queue = JobQueue(self._session)
    for edit_id in ids:
        await queue.enqueue(
            kind=JobKind.FEW_SHOT_INDEX,
            payload={"edit_id": edit_id},
            dedup_key=f"few_shot_index:{edit_id}",
            max_attempts=settings.FEW_SHOT_INDEX_MAX_ATTEMPTS,
        )
    logger.info("reconciler.requeued_unembedded_edits", count=len(ids))
    return len(ids)
```

### 3.6 `app/jobs/worker.py`

In `_reconcile_loop`, after `find_partial_documents()`:

```python
await rec.reconcile_unembedded_edits()
```

### 3.7 Generator + extractor wiring

The generator/extractor receive a session via a new constructor param `session` (alongside `few_shot_store` and `current_template_id`). The engine forwards its own session — generation already runs inside a request-scoped session for draft-row writes, so we reuse it.

`SectionGenerator.generate_section`:

```python
few_shot_block = ""
if self._few_shot_store is not None and self._session is not None:
    chunk_context = chunks_to_context(retrieved.get(section_spec.name, []))
    examples = await self._few_shot_store.retrieve(
        self._current_template_id,
        section_spec.name,
        session=self._session,
        chunk_context=chunk_context,
        top_k=settings.FEW_SHOT_TOP_K,
    )
    if examples:
        few_shot_block = _render_section_few_shot(examples)

user_content = (
    ...existing...
    + few_shot_block
    + (extra_instructions or "")
)
```

Per audit (`app/draft/extractor.py:47`), the extractor's per-field method is named `_extract_field`. It does the analogous injection in the user message after the existing evidence section, using `chunks_to_context(retrieved.get(field_spec.name, []))`.

`_render_section_few_shot` and `_render_field_few_shot` (and `chunks_to_context`) are private helpers in `app/edits/few_shot_store.py` so the prompt format is owned by the edits module (single source of truth).

### 3.8 Engine wiring

```python
# app/draft/engine.py
class DraftEngine:
    def __init__(self, retriever, llm_router, registry, session_factory,
                 embedder=None) -> None:
        ...
        self._embedder = embedder
        # FewShotStore construction is cheap and synchronous; we still build it
        # once so the dim-check happens at boot, not per-request.
        self._few_shot_store = (
            FewShotStore(embedder=embedder) if embedder is not None else None
        )

    async def generate(self, ..., *, session: AsyncSession):
        ...
        extractor = FieldExtractor(
            self._router,
            few_shot_store=self._few_shot_store,
            current_template_id=template_id,
            session=session,
        )
        generator = SectionGenerator(
            self._router,
            few_shot_store=self._few_shot_store,
            current_template_id=template_id,
            session=session,
        )
```

`generate()` (and `regenerate_section()`) gain a keyword-only `session` argument forwarded by the route handler. Per audit, the engine is instantiated as a singleton in `app/api/deps.py:get_draft_engine`; route handlers already hold an `AsyncSession` from `Depends(get_session)` and pass it through. This avoids opening new sessions inside generation.

`regenerate_section` likewise constructs `SectionGenerator` with the same args so few-shots also apply on regenerate.

The existing `few_shot=[]` argument passed to `generator.generate_all` at `app/draft/engine.py:85` and the parameter on `SectionGenerator.generate_all` (audit confirmed at line 50) are both removed.

### 3.9 Routes — `app/api/routes/edits.py`

```python
router = APIRouter(tags=["edits"])

@router.post("/api/drafts/{draft_id}/edit", response_model=DraftResponse)
async def save_edit(draft_id: str, body: EditCreateRequest,
                    session: AsyncSession = Depends(get_session)) -> DraftResponse:
    service = EditService(
        session=session,
        queue=JobQueue(session),
        registry=get_template_registry(),
    )
    result = await service.save_edit(
        draft_id, body.final_output, trace_id=_current_trace_id()
    )
    await session.commit()
    # Re-read the draft to return the post-update view (status='edited', edit_count, etc.)
    return await _build_draft_response(draft_id, session)

@router.get("/api/templates/{template_id}/edit-metrics",
            response_model=EditMetricsResponse)
async def edit_metrics(template_id: str,
                       days: int = Query(settings.EDIT_METRICS_DEFAULT_DAYS, gt=0, le=365),
                       session: AsyncSession = Depends(get_session)) -> EditMetricsResponse:
    service = EditService(session=session, queue=JobQueue(session),
                          registry=get_template_registry())
    return await service.metrics(template_id, days=days)
```

`_build_draft_response(draft_id, session)` is extracted from existing `get_draft` so M9 doesn't duplicate logic; this refactor lives in `app/api/routes/drafts.py`.

### 3.10 Schemas — `app/api/schemas/edits.py`

```python
from typing import Any
from pydantic import BaseModel

class EditCreateRequest(BaseModel):
    final_output: dict[str, Any]  # validated against template at service layer

class EditMetricFieldRow(BaseModel):
    name: str
    edited_count: int
    edit_rate: float | None

class EditMetricSectionRow(BaseModel):
    name: str
    edited_count: int
    edit_rate: float | None

class EditMetricsResponse(BaseModel):
    template_id: str
    window_days: int
    drafts_count: int
    fields: list[EditMetricFieldRow]
    sections: list[EditMetricSectionRow]
```

`DraftResponse` (in `app/api/schemas/drafts.py`) gains `edit_count: int = 0`.

### 3.11 Migration `0005_edits_few_shot_index.py`

```python
revision = "0005"
down_revision = "0004"

def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS edits_embedding_hnsw_idx "
        "ON app.edits USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) "
        "WHERE few_shot_indexed_at IS NOT NULL"
    )

def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.edits_embedding_hnsw_idx")
```

`IF NOT EXISTS` makes the migration safe to re-run in dev when the DB was reset.

---

## 4. Test plan (every test owned, no gaps)

### 4.1 Unit (`tests/unit/`)

| Test | Asserts |
|------|---------|
| `test_diff.py::test_scalar_modified` | `compute_field_diff(date, "2025-01-01", "2025-02-02")` → `{operation: modified, ai, user}` |
| `test_diff.py::test_scalar_equal_returns_none` | equal strings → `None` |
| `test_diff.py::test_list_string_added_removed` | sets correctly populated |
| `test_diff.py::test_list_party_identity_lowercased` | `[{"name":"Smith"}]` vs `[{"name":"smith"}]` → no change |
| `test_diff.py::test_list_party_added` | new party appears in `added_items` with full dict |
| `test_diff.py::test_added_from_none` | `ai=None, user=value` → `operation=added` |
| `test_diff.py::test_removed_to_none` | inverse → `operation=removed` |
| `test_diff.py::test_section_text_equal_returns_none` | identical text → `None` |
| `test_diff.py::test_section_text_replace` | SequenceMatcher replace op emitted |
| `test_diff.py::test_section_text_normalize_nfc` | NFC-equivalent unicode strings → `None` |
| `test_diff.py::test_top_level_is_empty` | both empty dicts → `is_empty()` |
| `test_diff.py::test_top_level_extra_user_field_rejected` | the validator in service layer is unit-tested via `EditService` builder; unit-level: `compute_diff` is forgiving (extra keys yield diffs), validation happens in service |
| `test_few_shot_search_repr.py::test_truncation` | Inputs > limits → output ≤ 3500 chars |
| `test_few_shot_search_repr.py::test_party_render` | `list[party]` renders as `"name (role)"`; deterministic ordering |
| `test_few_shot_search_repr.py::test_pure_function` | `_search_repr(...)` called twice with identical args (including `chunk_context=None`) returns the same string (purity) |
| `test_few_shot_search_repr.py::test_chunk_context_appended` | When `chunk_context` is non-None, output contains a `CONTEXT:` line; when None, no `CONTEXT:` line — confirms the §1.7 query-time augmentation is gated correctly |

### 4.2 Integration — edit save (`tests/integration/test_edit_save.py`)

1. Seed a `ready` draft with known `ai_output` (fields + sections), `prompt_fingerprint='abc'`, `template_version=3`.
2. `POST /api/drafts/{id}/edit` with `user_output` that changes `parties` (modify) and `background` section (text replace).
3. Assert response: `edit_count == 2`, `status == 'edited'`, `final_output` matches body.
4. Query `app.edits` → 2 rows with the expected `field_type`, `field_or_section_name`, both carrying `template_version=3`, `prompt_fingerprint='abc'`. (NN-5)
5. Query `jobs.jobs` → 2 `few_shot_index` jobs `pending`, each with `dedup_key=few_shot_index:<edit_id>`, `max_attempts=5`.

### 4.3 Integration — edit save no-op (`tests/integration/test_edit_save_noop.py`)

1. Seed `ready` draft.
2. POST `/edit` with `final_output` identical to `ai_output`.
3. Assert: `edit_count == 0`, `status == 'edited'`, `edited_at` set, **zero** `app.edits` rows for this draft, **zero** `few_shot_index` jobs.

### 4.4 Integration — index handler (`tests/integration/test_few_shot_index_handler.py`)

1. Insert an `Edit` row directly with `embedding IS NULL`, `few_shot_indexed_at IS NULL`.
2. Call `handle_few_shot_index({"edit_id": id}, session)` with the test embedder (returns deterministic 1024-d vector). Commit the session (the handler does not commit per §1.12 step 5; the test mimics the worker by committing after).
3. Assert `embedding IS NOT NULL` and `few_shot_indexed_at IS NOT NULL`.
4. Call the handler again with the same id → no error, `few_shot_indexed_at` unchanged (idempotent).
5. Construct `FewShotStore(embedder=embedder_dim_1536)` directly (bypassing the handler) and assert the constructor raises `EditError(EMBEDDER_DIM_MISMATCH)` with `retryable=False` — per §3.3 the dim check moved to `__init__`.

### 4.5 Integration — retrieval (`tests/integration/test_few_shot_retrieval.py`)

1. Seed template `case_fact_summary` + 3 edits for field `parties`, each with embeddings that point in known directions in 1024-d space (use the existing `StubEmbedder` extension or a fixture embedder that hashes the input to a deterministic vector).
2. Build a current_context whose `_search_repr` matches edit #2's input.
3. Call `FewShotStore.retrieve("case_fact_summary", "parties", current_context, top_k=3)`.
4. Assert order: edit #2 first (smallest distance), then the others.
5. Assert filter: an edit for a different `template_id` does not appear.
6. Assert filter: an edit with `few_shot_indexed_at IS NULL` does not appear.

### 4.6 Integration — reconcile (`tests/integration/test_few_shot_reconcile.py`)

1. Insert an `Edit` row with `few_shot_indexed_at IS NULL`, `created_at = NOW() - INTERVAL '2 minutes'`.
2. Run `Reconciler(session).reconcile_unembedded_edits()`. Assert returns 1 and a `few_shot_index` job is pending with the right `dedup_key`.
3. Run again immediately → enqueue is deduplicated; no second job.
4. Execute the job via the worker handler. Assert `few_shot_indexed_at` is now set.

### 4.7 Integration — edit metrics (`tests/integration/test_edit_metrics.py`)

1. Seed 5 drafts for template T1, 2 of them with `parties` edited, 1 with `background` edited.
2. `GET /api/templates/T1/edit-metrics?days=30` → `drafts_count=5`, `parties.edited_count=2, edit_rate≈0.4`, `background.edited_count=1, edit_rate=0.2`, all other fields/sections rate=0.
3. With `days=1` and all drafts older than 1 day → `drafts_count=0`, all `edit_rate=null`.

### 4.8 Integration — loop closes Stage 1 (`tests/integration/test_loop_closes_stage1.py`)

The rubric demo. Uses a deterministic mock router (already exists in test infra) that, for the `generation` task, **echoes the few-shot user_value into the section text**. For the `extraction` task, **returns the few-shot user_value verbatim** when one is present.

Steps:
1. Seed two documents D1 and D2 with overlapping party-name text.
2. Generate draft A on template `case_fact_summary` over [D1].
3. POST edit for draft A changing `parties` from `[{"name":"Smith"}]` to `[{"name":"Smith, et al."}]`.
4. Execute the worker once so `few_shot_index` job runs and the edit is embedded.
5. Generate draft B on the same template over [D2].
6. Spy on the mock router's `extraction` call for `parties` → assert the prompt contains the substring `Smith, et al.` (the past user_value was injected).
7. Read draft B's `ai_output["fields"]["parties"]` → assert value contains `Smith, et al.` (the mock echoed it, proving the few-shot influenced output end-to-end).

This single test is the acceptance proof for the rubric.

### 4.9 Existing tests that must still pass

- All `tests/unit/test_generator_*.py`, `tests/unit/test_extractor_*.py` — the new constructor params have defaults of `None`, so the existing test surfaces are unchanged.
- All `tests/integration/test_validation_*.py`, `test_groundedness_score.py`, `test_regenerate_*.py` — the engine wiring change (`few_shot_store=None` when no embedder) preserves M8 behavior. We assert this by running the full M8 test set.

---

## 5. Acceptance criteria (mapped to spec checklist)

- [ ] **Edit row tagged with `prompt_fingerprint`** — covered by `test_edit_save.py` step 4.
- [ ] **Next generation retrieves few-shot example** — covered by `test_loop_closes_stage1.py` step 6.
- [ ] **Reconciliation sweep recovers unindexed edits** — covered by `test_few_shot_reconcile.py`.
- [ ] **Edit-metrics endpoint returns per-field rates** — `test_edit_metrics.py`.
- [ ] **Loop-closes deterministic test green** — `test_loop_closes_stage1.py` step 7.
- [ ] **NN-3 backpressure unchanged** — no new path opens an in-process pipeline.
- [ ] **NN-4 SET LOCAL applied to retrieve** — asserted by mock at session-event level in retrieval test (see `tests/integration/conftest.py` fixture pattern for `SET LOCAL`).
- [ ] **NN-5 fingerprint stored** — verified in DB by `test_edit_save.py`.
- [ ] **NN-11 reconciler wired** — `test_few_shot_reconcile.py`.
- [ ] All existing M7 + M8 tests pass.

---

## 6. Risks and mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Embedder dim mismatch silently corrupts the index | High — index unusable, error obscure | Fail fast at `index()` with `EMBEDDER_DIM_MISMATCH`, non-retryable; surface in `/admin/llm-stats` (already wired via `llm_log`). |
| Reconciler + immediate enqueue race | Low — dedup_key collision | `dedup_key=few_shot_index:{edit_id}` shared by both paths; queue's existing `ON CONFLICT (dedup_key) DO NOTHING` is the guard. |
| Few-shot injection blows past LLM context window | Med | Truncate each ai/user value to 600 chars; `top_k=3` ceiling. Sum < 4 KB → safe for all routed tiers. |
| Bad embedding gets indexed (model regression) | Low | `few_shot_indexed_at` is set even on bad indexes — but the search representation is deterministic from input; if the embedder is broken, retrieval will return nothing useful but won't crash. Acceptable for v1. |
| `compute_section_diff` perf on multi-page sections | Low | `SequenceMatcher` is O(n²) worst-case; section text is bounded by template `target_length_max` (≤ ~500 words). Fine. |
| Concurrent `POST /edit` for the same draft | Low | `SELECT ... FOR UPDATE` on the draft row serializes edits. |
| HNSW index build cost on a large `app.edits` table at deploy | Low for v1 (single operator, < 1k edits expected) | Partial index keeps it tight; documented in migration. |
| Generator change breaks M8 retry path | Med | The retry call to `generator.generate_section` already gets `few_shot_store` via `__init__`; the retry continues to inject the same few-shots (correct — pattern shouldn't disappear on retry). M8 tests in §4.9 catch regressions. |

---

## 7. Implementation order (no merge gaps)

The spec's sub-agent split is correct; this is the merge order:

1. **Step A — schema and types** (single commit, blocks everything):
   - Migration `0005`
   - `app/edits/diff.py` + unit tests
   - `app/core/errors.py::EditError`
   - `app/api/schemas/edits.py`
   - `app/settings.py` additions

2. **Step B — few-shot store + handler** (parallel to C):
   - `app/edits/few_shot_store.py` + `test_few_shot_search_repr.py`, `test_few_shot_retrieval.py`
   - `app/jobs/handlers/few_shot_index.py` + `test_few_shot_index_handler.py`
   - `app/jobs/handlers/__init__.py` registration

3. **Step C — edit service + routes** (parallel to B):
   - `app/edits/service.py` + `test_edit_save.py`, `test_edit_save_noop.py`, `test_edit_metrics.py`
   - `app/api/routes/edits.py`
   - `app/main.py` router include
   - `app/api/schemas/drafts.py` + `routes/drafts.py` `edit_count` field

4. **Step D — reconciler wiring** (after B):
   - `app/jobs/reconciler.py::reconcile_unembedded_edits`
   - `app/jobs/worker.py::_reconcile_loop` call site
   - `test_few_shot_reconcile.py`

5. **Step E — engine wiring + loop-closes** (after B, C, D):
   - `app/draft/extractor.py`, `app/draft/generator.py`, `app/draft/engine.py`, `app/api/deps.py`
   - `test_loop_closes_stage1.py`
   - Full regression run of M7 + M8 suites

6. **Step F — `docs/milestones/M9-DONE.md`** with deviations and the loop-closes sample run.

---

## 8. Out of scope (M10 / later)

- LLM rule extraction over the edit set (M10).
- `POST /admin/rule-extractor/run` endpoint (M10).
- APScheduler 6h cron for the extractor (M10).
- Editing UI (M11).
- Cross-template few-shot signal.
- Per-fingerprint filtering in retrieve (preserved as a column tag only).
- Edit privacy/PII redaction.
- Embedding model swap or 1536-dim migration.

---

## 9. Definition of done

A reviewer can, in a fresh stack:

```
make up
make seed
# (UI or curl): generate draft A on case_fact_summary over a seed doc
# edit field `parties` via POST /api/drafts/{id}/edit
# wait <5s for the worker to index
# generate draft B on the same template over a different seed doc
# observe draft B's parties field contains the corrected form from the prior edit
```

…and:

- `pytest tests/unit/test_diff.py tests/unit/test_few_shot_search_repr.py` green.
- `pytest tests/integration/ -k "edit or few_shot or loop_closes"` green.
- The full pre-existing M7 + M8 test set still green.
- `docs/milestones/M9-DONE.md` written, listing actual deviations (if any) from this plan.
