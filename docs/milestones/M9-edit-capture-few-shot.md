# M9 — Edit Capture + Few-shot Store

**Estimated time:** 2 hours
**Dependencies:** M7, M8
**Rubric impact:** Improvement from Edits — front half of 25 pts (the larger half)

## Goal

When an operator saves an edited draft, the system captures a structured field-level diff with full context, logs it, embeds it, and indexes it into the few-shot store. The next time a draft is generated for the same template, similar past edits are retrieved and injected as few-shot examples. This is **Stage 1** of the improvement loop — the one the rubric explicitly tests for.

Stage 2 (LLM-extracted persistent rules) is M10.

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-5, NN-11**
2. `docs/architecture/03-components/edit-loop.md` (if written; otherwise infer from the architecture)
3. `docs/architecture/03-components/draft-engine.md` — few-shot injection point

## Non-Negotiables that apply

- **NN-5** — edits are tagged with `prompt_fingerprint`, not just `template_version`
- **NN-11** — few-shot embedding has a reconciliation sweep; failed indexes are retried

## Files to create / modify

### Edit service

- `app/edits/__init__.py`
- `app/edits/diff.py` — `compute_diff(template, ai_output, user_output) -> StructuredDiff`:
  - Per **field**: `{"field": "parties", "ai": [...], "user": [...], "operation": "modified|added|removed", "added_items": [...], "removed_items": [...]}`
  - Per **section**: text-level diff via `difflib.SequenceMatcher` collapsing into `{"section": "...", "ai_text": "...", "user_text": "...", "char_changes": [{"op":"replace","ai":"...","user":"..."}, ...]}`
  - Excludes unchanged sections/fields entirely — diff only carries the deltas
- `app/edits/service.py` — `EditService`:
  - `async save_edit(draft_id, user_output) -> Edit`:
    1. Load `Draft` with its template version (NN-5)
    2. `diff = compute_diff(template, draft.ai_output, user_output)`
    3. For each changed field/section, create an `Edit` row with:
       - `template_id, template_version, prompt_fingerprint` (from the draft)
       - `field_or_section_name`, `field_type`
       - `ai_value, user_value, diff`
       - `context = {source_chunks: [...], retrieved_at: ..., model_used: ...}` (copied from the draft)
    4. Enqueue `FEW_SHOT_INDEX` job per edit (immediate path)
    5. Update `draft.final_output`, `draft.edited_at`, `draft.status='edited'`
  - `async metrics(template_id, days=30) -> EditMetrics`:
    - Per field: edit_rate = `edits_on_field / drafts_using_template`
    - Per section: same

### Few-shot store

- `app/edits/few_shot_store.py` — `FewShotStore`:
  - `async index(edit_id)`:
    - Loads `Edit`
    - Computes embedding of a "search representation" = `ai_value || user_value || context tags joined`
    - Updates `edits.embedding` and `edits.few_shot_indexed_at`
  - `async retrieve(template_id, field_or_section, current_context, top_k=3) -> list[FewShotExample]`:
    - Builds a query embedding from `current_context` (the retrieved chunks for this field/section)
    - Filters: `template_id` matches, `field_or_section` matches, `few_shot_indexed_at IS NOT NULL`
    - Vector search via pgvector with `LIMIT top_k`
    - Returns `FewShotExample(ai_value, user_value, context_tags)` per result
  - `async backfill_unindexed() -> int` — used by reconciler (NN-11)

### Job handler

- `app/jobs/kinds.py` — register `FEW_SHOT_INDEX` handler that calls `FewShotStore.index(payload["edit_id"])`. Backoff on embedder failures.

### Reconciler integration

- `app/jobs/reconciler.py` — implement `find_unembedded_edits()` (declared in M1):
  ```sql
  SELECT id FROM app.edits
  WHERE few_shot_indexed_at IS NULL
    AND created_at < NOW() - INTERVAL '1 minute'
  LIMIT 100;
  ```
  Re-enqueues `FEW_SHOT_INDEX` for each.

### Generator wiring

- `app/draft/generator.py` — `generate_all` now actually uses few-shots:
  - Before generating a section, call `few_shot_store.retrieve(template_id, section_name, context=retrieved_chunks_for_section, top_k=3)`
  - Inject into the prompt:
    ```
    Past edits relevant to this section (in order of recency):
    
    Example 1:
      AI draft was:
        """<truncated ai_value>"""
      Operator changed it to:
        """<truncated user_value>"""
    
    Apply these patterns when relevant.
    ```
  - Same for field extraction: `few_shot_store.retrieve(template_id, field_name, ...)`

### API

- `app/api/schemas/edits.py`
- `app/api/routes/edits.py`:
  - `POST /api/drafts/{id}/edit` `{final_output}` → returns the saved draft with edit count
  - `GET /api/templates/{id}/edit-metrics` → per-field, per-section edit rates over time

### Tests

- `tests/unit/test_diff.py` — field-level diffs for added/removed/modified items; section text diffs
- `tests/integration/test_edit_save.py` — generate a draft; save an edit; assert `Edit` row exists with correct `prompt_fingerprint`; assert `FEW_SHOT_INDEX` job is enqueued
- `tests/integration/test_few_shot_retrieval.py` — index 3 edits for the same template/field; query the store with a similar context; assert relevant edits returned
- `tests/integration/test_few_shot_reconcile.py` (NN-11) — insert an edit with `few_shot_indexed_at IS NULL` and old `created_at`; run reconciler; assert `FEW_SHOT_INDEX` job enqueued; run worker; assert `few_shot_indexed_at` populated
- `tests/integration/test_loop_closes_stage1.py` — **the rubric demo test**:
  1. Generate draft A for template `case_fact_summary` on doc D1
  2. Edit field `parties` to a specific corrected form (e.g., change "Smith" → "Smith, et al.")
  3. Save the edit; wait for indexing
  4. Generate draft B for the same template on a different doc D2 with similar context
  5. Assert the few-shot retrieval returns the prior edit
  6. Assert the generated `parties` field reflects the pattern (use a deterministic mock generator that echoes its few-shot input)

## Acceptance criteria

- [ ] Saving an edit produces a row in `app.edits` with the right `prompt_fingerprint`
- [ ] The next generation actually retrieves the few-shot example (provable in test)
- [ ] Reconciliation sweep recovers unindexed edits
- [ ] Edit-metrics endpoint returns per-field rates
- [ ] Loop-closes test green (deterministic mock asserts the few-shot influenced output)

## Out of scope

- Persistent rule extraction — M10
- Cross-template signal sharing — out of v1
- Privacy / consent UI — out of v1

## Definition of done

The reviewer can: generate a draft, edit it, generate another draft on similar input, and see the prior edit's pattern reflected. `M9-DONE.md` written.

## Sub-agent delegation

After `app/edits/diff.py` is merged:

- Sub-agent A: `app/edits/few_shot_store.py` + indexing tests
- Sub-agent B: `app/edits/service.py` + edit-save tests
- Sub-agent C: generator wiring + loop-closes test

Three independent tracks. Engine.py touches at the end.
