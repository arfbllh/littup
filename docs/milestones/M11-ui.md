# M11 — Operator UI

**Estimated time:** 3 hours (Next.js path) / 1.5 hours (Streamlit fallback)
**Dependencies:** M3, M7, M8, M9, M10 (API surface must exist)
**Rubric impact:** Documentation (5 pts via demo-ability) + visible evidence for every other rubric category

## Goal

A minimal but credible operator UI that lets a reviewer demonstrate every part of the system in five minutes: upload documents, see them progress through the pipeline, pick a template, generate a draft, click a citation to see the source span highlighted on the page, edit the draft, save, regenerate against a similar input, and watch the rule extractor pick up a new pattern from the admin page.

The UI does not need to be visually stunning. It needs to make the system *legible*.

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-10**
2. `docs/architecture/03-components/draft-engine.md` (response shape)
3. `docs/architecture/03-components/edit-loop.md`
4. `docs/architecture/project-skeleton.md` (UI section)
5. Every API route created in M3, M7, M8, M9, M10

## Non-Negotiables that apply

- **NN-10** — SSE client honors `Last-Event-ID`; polling fallback kicks in after 30s of SSE disconnection

## Decision: Next.js vs Streamlit

**Default: Next.js 15 (App Router) with shadcn/ui + Tailwind.** Reviewer-grade polish, clean component model, good first impression.

**Fallback: Streamlit.** Faster to build, less polished. Trigger fallback if at Fri 14:00 local the Next.js path is not feature-complete. Skeleton for Streamlit lives in `ui_streamlit/` and is built fresh — no point pre-building both.

Both paths consume the same HTTP API. The integration seam doesn't change.

## Files to create (Next.js path)

### Setup

- `ui/package.json` — Next.js 15, React 19, Tailwind, shadcn/ui core deps, `lucide-react`, `swr` for polling
- `ui/next.config.js` — proxy `/api/*` to `http://api:8000` in dev
- `ui/tsconfig.json`
- `ui/app/globals.css` — Tailwind base + a small custom CSS layer for the citation highlight

### Shared

- `ui/lib/api.ts` — typed client wrapping every backend route used. One function per endpoint, returning typed Promises. Single source of API truth.
- `ui/lib/sse.ts` — `subscribeToDocumentEvents(documentId, onEvent, onClose)`:
  - Opens an `EventSource` to `/api/documents/{id}/events`
  - Persists last seen event ID in `sessionStorage`
  - On reconnect, sends `Last-Event-ID` header (via a custom fetch-based SSE shim; native `EventSource` doesn't expose this — use `@microsoft/fetch-event-source` which does)
  - After 30s of disconnection, **falls back to polling** `GET /api/documents/{id}` every 5s; resumes SSE on next successful connection (NN-10)
- `ui/lib/types.ts` — TypeScript types mirroring the API schemas
- `ui/components/Layout.tsx` — top nav, footer; nothing fancy
- `ui/components/DocumentStatusPill.tsx` — color-coded pill (`uploaded → ... → ready/failed`)

### Upload + documents list

- `ui/app/page.tsx` — landing redirects to `/documents`
- `ui/app/documents/page.tsx` — list view; table with status pill; click row → detail page
- `ui/app/documents/upload/page.tsx` — drag-and-drop multi-file upload; uses `<input type="file" multiple />`; for each file, POSTs to `/api/documents` in sequence (or with `Promise.all` capped at 3 concurrent); shows per-file status as it streams; handles 429 by exponential-backoff retry

### Document detail (debug page)

- `ui/app/documents/[id]/page.tsx` — left pane shows the rendered page image (calls `/api/documents/{id}/pages/{n}`); right pane shows the extracted blocks for that page. Useful for the reviewer to verify OCR quality. Also shows status timeline.

### Drafts

- `ui/app/drafts/new/page.tsx` — choose template + select documents from a list; submit calls `POST /api/drafts`; redirects to `/drafts/{id}`
- `ui/app/drafts/[id]/page.tsx` — the main artifact view:
  - Left pane: rendered draft with fields at top, sections below
  - Each section rendered as prose with `[chunk:CHUNK_ID]` markers replaced by `<CitedText>` components
  - Right pane: source viewer (page image with bbox overlay when a citation is hovered/clicked)
  - **Edit mode toggle** — switches sections to `<textarea>` and fields to editable inputs; "Save" button submits to `POST /api/drafts/{id}/edit`
  - Groundedness score visible at the top
- `ui/components/CitedText.tsx`:
  - Renders the section text with citation markers as superscript chips
  - Each chip is colored by `validation_status` (green=supported, yellow=partial, red=unsupported, gray=unchecked)
  - On click → fires a state update with `(chunk_id, page, bbox)` → right pane highlights the bbox on the page image
  - On hover → tooltip with the chunk's first 200 chars + validation reason
- `ui/components/DraftEditor.tsx` — handles the edit-mode form state; debounced auto-save (or just a "Save" button — pick simplest)
- `ui/components/PageWithBboxes.tsx` — `<canvas>` overlay on the page image to draw the highlighted bbox

### Admin

- `ui/app/admin/page.tsx`:
  - LLM stats card — calls `/admin/llm-stats`: total cost in last hour, calls per tier, p50/p95 latency, cache hit rate
  - Edit rates card — calls `/api/templates/{id}/edit-metrics` for each template: edit rate per field/section, trend sparkline
  - Templates card — version history per template (collapsible per template); shows `appended_rules` for the current version
  - **"Re-extract rules now" button** — POSTs to `/admin/rule-extractor/run`; shows live result (new rules added, edits processed); refreshes the templates card on completion
  - Budget remaining: `current_spend / hourly_budget` progress bar

### Tests (Playwright, lightweight)

- `ui/tests/upload.spec.ts` — drag-drop a fixture PDF; assert it appears in the list with a status pill
- `ui/tests/draft_view.spec.ts` — mock the API; render a draft view; click a citation; assert the right pane updates with the bbox
- `ui/tests/edit_save.spec.ts` — toggle edit mode; change a field; save; assert POST went to the right endpoint
- `ui/tests/sse_reconnect.spec.ts` (NN-10) — mock the SSE endpoint to close after 1s; assert the client reconnects with `Last-Event-ID` and falls back to polling after 30s

## Files to create (Streamlit fallback)

Only if invoked.

- `ui_streamlit/app.py` — sidebar nav: Upload / Documents / Drafts / Admin
- `ui_streamlit/pages/1_Upload.py` — `st.file_uploader(multiple=True)`, posts to API
- `ui_streamlit/pages/2_Documents.py` — list + status polling
- `ui_streamlit/pages/3_Drafts.py` — template picker, draft generation, draft display with inline `[chunk:...]` markers as clickable buttons that expand to show the chunk
- `ui_streamlit/pages/4_Admin.py` — stats + rule-extractor trigger
- No bbox highlighting (Streamlit can't do bidirectional canvas-text linking cleanly) — instead, citation click expands the chunk text inline. Less impressive, still legible.

## Acceptance criteria

- [ ] Upload page accepts multiple files and shows per-file progress
- [ ] Document detail page shows OCR'd text alongside the page image
- [ ] Draft view renders inline citation markers
- [ ] Clicking a citation highlights the source bbox (Next.js path) or expands the chunk (Streamlit path)
- [ ] Citation marker color reflects `validation_status`
- [ ] Edit mode → Save round-trips correctly
- [ ] Admin page shows LLM cost, edit rates, and a button that triggers rule extraction live
- [ ] After rule extraction, the templates card refreshes to show the new rule
- [ ] SSE reconnect + polling fallback test green

## Out of scope

- Authentication, user accounts
- Real-time collaborative editing
- Mobile layout — desktop only
- Visual polish beyond shadcn defaults
- Diff visualization of edits (operator sees the AI draft, edits it, saves; no inline diff UI in v1)

## Definition of done

A reviewer can open the app, follow the demo script in `DEMO.md`, and complete every step without running into a UI bug. `M11-DONE.md` written with a screen-recording link (or screenshots in `docs/screenshots/`).

## Sub-agent delegation

**Yes, after `lib/api.ts` and `lib/sse.ts` are merged**, the screens are independent:

- Sub-agent A: Upload + Documents pages
- Sub-agent B: Document detail + page-image viewer
- Sub-agent C: Drafts new + draft view + `CitedText` + `PageWithBboxes`
- Sub-agent D: Admin page + rule-extractor button + edit-metrics card

The `DraftEditor` and edit-save flow stay sequential after Sub-agent C lands.

## Fallback decision point

At **Fri 14:00 local** check progress:
- All four screens working in Next.js → continue, add polish
- 2–3 screens working → continue with Next.js, drop the document-detail debug page (lowest demo value)
- 0–1 screens working → switch to Streamlit fallback immediately; 1.5h to a complete UI

Document the switch in `M11-DONE.md` if it happens; the reviewer will not penalize Streamlit, only an incomplete UI.
