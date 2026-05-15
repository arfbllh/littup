# PLAN.md — UI Surface Expansion + OCR Quality Fixes

Owner: Arif · Drafted: 2026-05-15 · Status: proposed

This plan covers two themes that surfaced together:

1. **Fix:** documents reaching `ready` with zero extracted blocks (current symptom: "No blocks found for this document" on the right panel).
2. **Expose:** backend capabilities that already exist but the UI doesn't surface (retry on stuck docs, live ingestion progress, per-section regenerate, edit metrics, OCR block edits, draft pre-flight composition with custom prompt + dynamic document set).

The work is sliced into six workstreams (WS‑A … WS‑F) ordered by risk and dependency. Each ships independently. WS‑A and WS‑B are blockers for the rest — start there.

---

## Constraints & invariants we must not break

Pulled from `CLAUDE.md`. Every change below is checked against these:

- **Inv #1 Recoverable ingestion** — anything new the worker runs must be a `jobs` row with heartbeats, not a `BackgroundTasks`.
- **Inv #2 Atomic hash idempotency** — `POST /api/documents` stays a single `INSERT … ON CONFLICT`.
- **Inv #5 Template snapshot immutable per draft** — `DraftEngine.generate()` resolves the template **once**. The `prompt_fingerprint` is the cache + edit-log identity key. Therefore: **a draft's input set (document_ids, custom prompt) is also immutable post-generation.** Editing inputs = creating a new draft.
- **Inv #6 VLM spend cap** — block-edit-triggered re-embedding stays on the local embedder; it does not invoke the LLM tier.
- **Inv #7 LLM cache is content-addressed** — changing the custom prompt changes the fully-resolved messages, so the cache busts automatically. No special handling needed.
- **Inv #10 SSE has polling fallback** — every event carries an `id`, endpoint honours `Last-Event-ID`, UI falls back to polling after 30 s. WS‑B must preserve this.
- **Inv #12 Observability** — every new endpoint logs structured + emits a `DocumentEvent` if it mutates ingestion state.

---

## WS‑A. Unblock "ready with 0 blocks"

**Symptom.** Doc reaches `status='ready'` but `app.blocks` is empty for that document, so the right panel shows "No blocks found for this document." Retry button is hidden because the doc is not `failed`.

**Root-cause hypothesis.** In `app/ingest/layout.py:90–92`, if `_load_spans()` returns `[]` the pipeline silently writes `0 blocks` and continues to `ready`. Spans are empty when the OCR phase produced nothing — most likely a thin/empty text layer that pdfplumber walked but pulled nothing from (the rasterise path errors out with `PDF2IMAGE_MISSING` and would have marked the doc `failed`, not `ready`).

### Steps

1. **Diagnose first, code second.** For each problem doc, run:
   ```sql
   SELECT status, page_count FROM app.documents WHERE id = :id;
   SELECT count(*) FROM app.pages WHERE document_id = :id;
   SELECT count(*) FROM app.spans s JOIN app.pages p ON s.page_id = p.id
     WHERE p.document_id = :id;
   SELECT count(*) FROM app.blocks WHERE document_id = :id;
   SELECT count(*) FROM app.chunks WHERE document_id = :id;
   ```
   Confirm pages > 0, spans = 0. Save the SHA-256 of one offender — we want it in a regression test (WS‑A.6).

2. **Backend — widen `retry_document`** (`app/ingest/service.py:903`, gate at line 915). Today the gate is `doc.status != 'failed'` and the route at `app/api/routes/documents.py:104-118` calls it with no parameters. We need to thread a `force` flag through both:
   - `POST /api/documents/{id}/retry` → add optional `?force=true` query param (new — the route currently takes none).
   - Service: accept a `force: bool = False` kwarg; when `force=True`, allow retry from any terminal state (`ready`, `failed`). For non-failed states, log a `WARN` with the override reason.
   - Keep the existing cascade-wipe logic (`pages`, `blocks`, `chunks`, `jobs.jobs` rows). It already handles "ready" inputs correctly — only the gate is changing.

3. **Backend — emit a recoverable failure on empty spans.** In `app/ingest/layout.py` after `_load_spans`, if `len(spans) == 0` and `doc.page_count > 0`, **do not** silently advance. Instead set `status='failed'`, `error_code='EMPTY_OCR_OUTPUT'`, emit a `failed` event, and stop. The failure event payload must include both `page_count` and `span_count` so the operator can distinguish a real OCR bug (e.g. PaddleOCR misconfigured) from a legitimately empty source (e.g. a 100% blank scan). The doc then surfaces the retry button and the operator sees a clear reason.

4. **Frontend — show retry on stuck `ready` docs too.** `ui/app/documents/[id]/page.tsx:206`:
   ```ts
   const stuckEmpty = status === 'ready' && (blocks?.length ?? 0) === 0;
   {(status === 'failed' || stuckEmpty) && (
     <Button ... onClick={() => handleRetry({ force: stuckEmpty })}>
       Retry
     </Button>
   )}
   ```
   And on the list page (`ui/app/documents/page.tsx`) — same condition, but we'd need a `block_count` field on `DocumentSummary` to know without an extra fetch. Cheapest: add `has_blocks: bool` to the summary (one extra column in the list query, no schema change).

5. **Banner.** When `stuckEmpty`, show a yellow status banner above the page viewer: "This document finished processing but no text blocks were extracted — likely a scanned PDF with no usable text layer. Retry to re-run OCR."

6. **Regression test.** `tests/unit/test_layout_empty_spans.py` — feed `parse_layout` a doc with zero spans, assert the doc lands in `failed` with `EMPTY_OCR_OUTPUT`, not `ready`.

**Acceptance.**
- Re-uploading the offender from step 1 ends in `failed` with `EMPTY_OCR_OUTPUT`, not silent `ready`.
- The retry button appears on the offender's detail page.
- Hitting retry re-runs ingestion and either succeeds (PaddleOCR has another go now that poppler is installed) or fails again with a clear reason.

**Risk.** Tightening the layout gate could surface other docs that previously slid through. That's the point, but warn the user it'll be visible.

**Effort.** ~half a day.

---

## WS‑B. Live ingestion progress (SSE in the UI)

**Why now.** WS‑A makes "stuck in some intermediate state" visible. The detail page currently polls (`useSWR` with adaptive `refreshInterval`); the SSE endpoint exists (`GET /api/documents/{id}/events`) but is unused. Inv #10 mandates polling fallback — we keep both, but SSE leads.

### Steps

1. **API client.** Add `streamDocumentEvents(id, { lastEventId }): EventSource` to `ui/lib/api.ts`. Server already honours `Last-Event-ID`.

2. **Hook.** New `ui/lib/hooks/useDocumentEvents.ts`:
   - Opens `EventSource(url)` on mount.
   - Tracks `lastEventId` in a ref; on close-with-error, reconnects after 1 s with the header set.
   - After 30 s of being unable to reconnect, fall back to SWR polling at 5 s (Inv #10).
   - Exposes `events: DocumentEvent[]` (capped at last 50, ring buffer) plus the latest `status`.

3. **UI surface.** On `ui/app/documents/[id]/page.tsx`:
   - Replace the polling-derived status with the SSE-derived one (SWR keeps running as a safety net, slower interval — 10 s).
   - Below the existing state-machine timeline, add a collapsible **Event log** rendering the ring buffer (event type, timestamp, payload one-liner). Default collapsed.
   - When an event of type `status_changed` arrives, mutate the SWR cache directly so the timeline animates without an extra GET.

4. **Reconnect storms.** If the same `EventSource` disconnects > 3 times in 60 s, stop reconnecting and surface a toast: "Live updates unavailable; falling back to polling." Don't fight a permanently broken connection.

**Acceptance.**
- Uploading a fresh doc shows live timeline transitions without polling for the first 30 s (verify in DevTools — no `GET /api/documents/{id}` until SSE drops).
- Killing the API mid-ingest gracefully falls back to polling. Status eventually reaches `ready` (or `failed`).
- Replay works: refreshing the page mid-ingest with a populated `lastEventId` resumes without duplicate events.

**Risk.** Browser SSE behaviour through proxies (nginx etc.) — already mitigated by `X-Accel-Buffering: no` on the route. None for local dev.

**Effort.** ~half a day.

---

## WS‑C. Per-section regenerate UI

**Why now.** Endpoint exists (`POST /api/drafts/{id}/sections/{name}/regenerate`, see `app/api/routes/drafts.py:223`) and accepts both `ready` and `edited` drafts (M9). No backend work needed.

### Steps

1. **API client.** Add `regenerateSection(draftId, sectionName)` to `ui/lib/api.ts`.

2. **UI surface.** On `ui/app/drafts/[id]/page.tsx`, next to each section header add a small "Regenerate" button (icon-only, `RotateCw`). On click:
   - Optimistic: blur the section body + spinner overlay.
   - Call `regenerateSection`; on success, replace the section in-place using the returned `SectionView`.
   - On `DRAFT_NOT_READY` / `SECTION_NOT_FOUND`, surface a toast and refetch the full draft.

3. **Edit-aware UX.** If the operator has unsaved edits in that section, prompt "Regenerating will discard your in-progress edit. Continue?" — `app/db/models/edit.py` has `created_at` but no `generated_at`. Compare `Edit.created_at > Draft.generated_at` (or the section's last regenerate timestamp, if we track it) for that `(draft_id, section)` pair.

**Acceptance.**
- Click "Regenerate" on one section; only that section's prose + citations change.
- Cost on the draft summary increases by the section's incremental cost (verify via `GET /admin/llm-stats`).
- The Regenerate button is disabled when `status='generating'` or `status='regenerating'`.

**Effort.** ~2 hours.

---

## WS‑D. Surface unused backend (small wins, batched)

Single PR; all small, all pure UI.

| # | Endpoint | UI surface |
|---|---|---|
| D1 | `GET /api/templates/{id}/edit-metrics` | Badge next to each section header on the draft page: "edited 3× in last 30d". Pulled once on draft mount. |
| D2 | `GET /admin/llm-stats` | Pill in `Shell.tsx` header showing rolling spend. **Note:** the endpoint currently aggregates the **last hour only** (`admin.py:32`, `timedelta(hours=1)`). Either (a) ship the pill as "last hr: $0.04" and label it accurately, or (b) extend the handler to take a `window=today\|hour` query param and add a daily aggregation. Auto-refresh every 60 s. Click → admin page. |
| D3 | `GET /admin/templates/{id}/versions` | On the draft detail page, replace the static "Template v3" text with a clickable chip → modal listing recent versions, fingerprints, and appended rules. |
| D4 | Block "copy text" button | Per-block in the right panel: clipboard icon → copies `block.text`. Pure client. |
| D5 | Citation hover → block scroll | Hovering `[chunk:abc]` in a section text scrolls the right panel and highlights the source block. Block IDs are already in the chunk's `block_ids[]`. |

**Effort.** ~1 day total, can be split across PRs.

**Risk.** D2 polls admin stats every 60 s from every UI session — fine for single-operator, but if we ever multi-tenant we throttle.

---

## WS‑E. Inline OCR block edit (operator fixes a typo)

**Why now.** Cascading nightly issue: the PaddleOCR output occasionally drops a word or mis-segments a phrase. Today the only fix is re-ingest. Goal: operator clicks a block, edits the text inline, saves, and the change propagates through chunks/embeddings/citations.

**Cascade overview.**

```
block.text changed
  → chunks where block_id ∈ chunks.block_ids[]   (re-synth chunk.text)
    → re-embed those chunks                       (local BGE-large, no LLM cost)
      → mark dependent citations as needing revalidation
        → next regenerate (or explicit re-validate) re-checks them
```

### Steps

1. **Schema.** `citations.validation_status` is a plain `Text` column (`app/db/models/draft.py:71`, default `"unchecked"`) — not a Postgres enum. The actual string values produced by the validator are `unchecked | supported | partial | unsupported | contradicted` (`app/draft/validator.py:27,40-45`). Add a new value `stale` meaning "underlying chunk text changed since this citation was validated". No DDL or migration needed since the column is unconstrained, but update the validator's allowed-values list and any UI rendering that switches on status.

2. **New endpoint.** `PATCH /api/documents/{id}/blocks/{block_id}` body `{ text: string }`:
   - Verify the doc is `ready`.
   - In one transaction:
     1. Update `app.blocks.text` (and emit a `block_edited` document event with old/new hashes).
     2. Find chunks where `:block_id = ANY(block_ids)`. For each, re-build `text` by concatenating current block texts in `block_ids[]` order.
     3. Mark all citations referencing those chunks as `validation_status='stale'`.
     4. Enqueue a `reembed_chunks` job with the affected chunk IDs (don't re-embed inline — the PATCH should return fast).
   - Return `{ affected_chunks: int, stale_citations: int }`.

3. **New job kind.** Add `REEMBED_CHUNKS = "reembed_chunks"` to the `JobKind` StrEnum in `app/jobs/kinds.py` (current members: `OCR, LAYOUT, CHUNKING, EMBEDDING, RULE_EXTRACTION, FEW_SHOT_INDEX, DRAFT_GENERATION` — no reembed kind today). Register a handler in `app/jobs/handlers/reembed_chunks.py` mirroring `handlers/embedding.py`. Payload `{ chunk_ids: [...] }`; the handler calls the existing embedder over the chunk batch, updates `chunks.embedding`, no other side effects. Bounded concurrency = 1. **Retry policy:** on transient failure (embedder OOM, vLLM 503), retry up to 3× with exponential backoff; on permanent failure leave the affected citations as `stale` and emit a `reembed_failed` document event so the UI can surface a "X chunks failed to re-embed" banner instead of silently leaving stale citations forever.

4. **Service module.** `app/ingest/block_edits.py` holds the transaction logic so the route stays thin (Inv: routes have no business logic).

5. **Frontend.**
   - In `ui/app/documents/[id]/page.tsx`'s block list, add an Edit icon per block. Click → block text becomes a `<textarea>` with Save/Cancel. Save calls the PATCH, then re-fetches blocks.
   - On the draft page, render citations with `validation_status='stale'` in a muted yellow with a "needs re-validation" tooltip.

6. **Re-validation path.** Add `POST /api/drafts/{id}/revalidate` — re-runs the existing Pass-3 citation validator for any citation in `('stale', 'unchecked')`. Cheap (only re-validates flagged citations, not the whole draft). Button on the draft page when at least one stale citation exists.

7. **Tests.**
   - Unit: `test_block_edit_cascades.py` — edit a block, assert affected chunks rebuilt, citations marked stale, no re-embed yet, job enqueued.
   - Integration: hit the PATCH, wait for the re-embed job, assert chunk vectors changed (cosine distance > some ε).

**Acceptance.**
- Operator edits "Plantiff" → "Plaintiff" in a block, saves, the block list shows the new text immediately.
- The two chunks containing that block have rebuilt text within 5 s.
- All citations pointing at those chunks are visually flagged on the draft page.
- Clicking "Re-validate" on the draft re-runs the validator for just those citations and clears the flag.

**Risk.**
- **Drift between blocks and the rendered PDF page image.** The page PNG is not re-rendered; only the OCR transcript changes. Make that clear in the UI ("PDF stays as scanned; only the extracted text is updated").
- **Cache invalidation for the LLM response cache (Inv #7).** The chunk text is part of the prompt that goes through the cache; since the cache key is `sha256(messages)`, an edited chunk naturally busts its cache entry on next generate. No code needed.

**Effort.** ~1.5 days.

---

## WS‑F. Draft pre-flight editing — add/remove documents, custom prompt

**The shape.** Operators want, before hitting Generate:

- Pick documents from an existing list (multi-select), **or** upload new ones inline (drag-drop → kicks off ingest → enabled once `status=ready`).
- Remove documents that were initially selected.
- Add a free-text "extra instruction" prompt (e.g. "Focus on the indemnification clause and ignore Schedule B.")

**Where it gets tricky.** Inv #5 says the template snapshot is immutable per draft, and the `prompt_fingerprint` is the cache + edit-log identity key. The custom prompt and document_ids are inputs to the prompt that goes into the cache → they're effectively part of identity. So:

- **Pre-generation:** mutate freely. The draft is still in a "draft of a draft" state on the client; nothing has been persisted as a `Draft` row yet.
- **Post-generation:** **never mutate inputs of an existing draft.** Instead, "Edit inputs and regenerate" creates a **new** draft row with the new inputs. The original is preserved (you can always go back to it). This matches how the rule extractor and edit store reason about `prompt_fingerprint`.

### Steps

1. **Backend — extend `DraftCreateRequest`** (`app/api/schemas/drafts.py:10`):
   ```python
   class DraftCreateRequest(BaseModel):
       template_id: str
       document_ids: list[UUID]
       extra_instructions: str | None = None   # NEW, ≤ 2000 chars
   ```
   Validation: trim, reject if > 2000 chars. `SectionGenerator.generate()` already accepts `extra_instructions` (`app/draft/generator.py:81,134-135` — confirmed wired into the user prompt); thread it from the API request through `DraftEngine.generate()` (currently doesn't take it) and persist it on the Draft row.

2. **Backend — persist the custom prompt.** Add column `app.drafts.extra_instructions TEXT NULL`. Migration. Include it in `DraftResponse` so the UI can show what was used.

3. **Backend — fingerprint must include the custom prompt.** Today `DraftTemplate.compute_fingerprint()` (`app/draft/templates/schema.py:50-67`) hashes only `system_prompt + appended_rules + extraction_schema + sections` — **`extra_instructions` is not in the hash**. Without a change, two drafts with different custom prompts would share a `prompt_fingerprint` and collide in the LLM response cache (violating Inv #7) and the edit log (violating Inv #5). Either (a) extend `compute_fingerprint()` to take an optional `extra_instructions` argument and fold it in, or (b) compute the per-draft fingerprint in `DraftEngine.generate()` as `sha256(template.compute_fingerprint() + extra_instructions)`. Option (b) keeps the template-level fingerprint stable for template-keyed lookups (e.g. edit-metrics) and is recommended.

4. **Frontend — redesign `/drafts/new`.**

   ```
   ┌─ New draft ─────────────────────────────────────┐
   │                                                 │
   │  Template:  [Settlement Agreement v3 ▾]         │
   │                                                 │
   │  Documents (3 selected):                        │
   │    ✓ smith_v_doe_complaint.pdf       ✕ remove   │
   │    ✓ smith_v_doe_exhibit_a.pdf       ✕ remove   │
   │    ✓ smith_v_doe_exhibit_b.pdf       ✕ remove   │
   │    + Add from library      + Upload new         │
   │                                                 │
   │  Custom instructions (optional):                │
   │   ┌─────────────────────────────────────────┐   │
   │   │ Focus on the indemnification clause…    │   │
   │   └─────────────────────────────────────────┘   │
   │   0 / 2000                                      │
   │                                                 │
   │              [Cancel]  [Generate draft]         │
   └─────────────────────────────────────────────────┘
   ```

   Components needed:
   - `<DocumentPicker>` modal: searches `GET /api/documents?status=ready` (the endpoint already filters; if not, add the filter — cheap).
   - `<DocumentUploader>`: drag-drop, calls existing `POST /api/documents`, then polls/SSE for `ready`, then auto-adds to the selection.
   - `<ExtraInstructions>`: textarea with char counter.

5. **Frontend — Edit + Regenerate from an existing draft.** On `ui/app/drafts/[id]/page.tsx`, add a "Regenerate with changes…" button that prefills the `/drafts/new` form with the existing template, documents, and custom prompt. Generating creates a **new** Draft row (different ID, different `prompt_fingerprint`); the old one is preserved.

6. **Wire it.**
   - `listDrafts()` already returns `document_count`; expose `extra_instructions` boolean (or length) on `DraftSummary` so the list page can show "📝 has custom prompt".
   - On `/drafts/[id]`, display the `extra_instructions` in a collapsible section.

7. **Tests.**
   - Unit: `test_draft_engine_extra_instructions.py` — generate twice, identical inputs but different `extra_instructions`, assert the prompt sent to the LLM contains the instructions and the fingerprints differ.
   - Integration: hit `POST /api/drafts` with `extra_instructions`, assert the persisted draft has it and the response echoes it.

**Acceptance.**
- I can compose a draft from a mix of library + freshly uploaded documents, write a custom instruction, generate it, and see the instruction echoed on the draft page.
- Clicking "Regenerate with changes" gives me the same form pre-filled.
- Two drafts with different `extra_instructions` have different `prompt_fingerprint` values.
- The original draft is **not** mutated when I create a "regenerate" version.

**Risk.** Operators may expect Generate to be instant after upload, but freshly uploaded docs need to reach `ready` first. The uploader has to gate the Generate button (`disabled` until every selected doc is `ready`) and show per-doc status pills.

**Effort.** ~2 days (the form is the bulk; backend changes are <100 lines).

---

## Sequencing

```
WS-A  ─┐
        ├─► WS-B ─► WS-C ─► WS-D
WS-A  ─┘                       │
                                ├─► WS-E
                                └─► WS-F
```

WS‑A and WS‑B unblock everything else (you can't trust draft generation if you can't trust ingestion finishing cleanly, and you can't iterate on the draft UI without live status). After that, ship in parallel:

| Day | WS  | What |
|-----|-----|------|
| 1   | A   | Diagnose, widen retry, fail-on-empty-spans, banner |
| 1.5 | B   | SSE hook + event log |
| 2   | C   | Per-section regenerate button |
| 2.5 | D   | Batch the small surfacings |
| 3–4 | E   | Block edit + cascade + re-validate |
| 4–6 | F   | Draft pre-flight composer |

Total: ~6 working days for one engineer, assuming no surprises.

---

## Cross-cutting concerns (apply to multiple workstreams)

- **Auth / authorization.** The plan adds several mutating endpoints (`/retry?force`, `PATCH /blocks/{id}`, `/revalidate`, draft create with custom prompt). The product is currently single-operator, so we keep the same trust model — but every new write route still goes through the existing request-ID middleware and writes structured logs naming the action (Inv #12). No new ACL layer; revisit if/when multi-operator lands.
- **Concurrent block edits.** Two operator sessions editing different blocks whose `block_ids[]` overlap on the same chunk would race when rebuilding `chunks.text`. WS‑E's transaction takes a row-level lock on the chunk row when rebuilding (`SELECT ... FOR UPDATE`) so the second edit serializes correctly. Citations get marked `stale` by whichever transaction commits last — fine, the operator re-validates once at the end.
- **`stuckEmpty` vs legitimately empty source.** A scanned PDF with no usable text is *expected* to be 0-block after our OCR path. WS‑A's `EMPTY_OCR_OUTPUT` failure conflates "OCR bug" with "no text to extract." Mitigations: (a) emit `page_count` and `span_count` in the failure event (WS‑A step 3), (b) the UI banner reads "no usable text layer — retry only if you believe this is wrong" rather than implying a transient failure.
- **Re-embed reliability (Inv #1, #12).** WS‑E enqueues a job; if it fails permanently, citations stay `stale` indefinitely. Mitigation in WS‑E step 3: bounded retries + a `reembed_failed` event the UI surfaces. Without this the system silently degrades.

---

## Out of scope (for this plan)

- Multi-operator concurrency or RBAC.
- Versioning the PDF source itself (we only edit the OCR transcript, not the source).
- VLM-based block correction (we only support manual operator edits).
- Auto-revalidation of all citations after a block edit (we only mark them `stale`; the operator triggers re-validation).
- Renaming / re-ordering blocks. Only `text` is editable in v1.
- Cross-draft "fork from existing draft" — WS‑F creates a new draft but doesn't link it to the parent. If we want lineage, add `Draft.parent_draft_id` later.

---

## Open questions to confirm before kickoff

1. **WS‑A step 3** — fail-on-empty-spans is a behaviour change. Existing `ready`-but-empty docs in the database will not be retroactively re-marked. Do we want a one-shot backfill script that re-marks them `failed` so they show the retry button without per-doc intervention?
2. **WS‑F step 5 — decision required before coding.** Should "Regenerate with changes" preserve the original draft's edit history when the new draft is generated? Edits are keyed on `(draft_id, section, prompt_fingerprint)` (`app/db/models/edit.py`), so edits won't carry over automatically. Three options: (a) accept the break — different inputs = different draft, edits are bound to their draft (recommended, simplest, matches Inv #5 semantics); (b) copy edits to the new draft with the new `(draft_id, prompt_fingerprint)` — preserves the "improving over time" loop but muddies the edit-store signal; (c) introduce `Draft.parent_draft_id` and have the few-shot retriever walk the lineage at query time. Pick before WS‑F kicks off; the choice changes the migration.
3. **WS‑E re-embed job concurrency.** Single worker is safest, but if an operator edits 10 blocks across a doc the queue depth grows. Worth a small UX: "X chunks pending re-embed" indicator on the doc page.

Once you sign off on these, I'll start with WS‑A.
