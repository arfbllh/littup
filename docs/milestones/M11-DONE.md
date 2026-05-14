# M11 — Operator UI — DONE

**Shipped:** 2026-05-14

## What shipped

Complete Next.js 15 (App Router) operator UI at `ui/`.

### Pages (7)
- `/` → redirects to `/documents` (server component)
- `/documents` — SWR-polled documents list with filter, inline dropzone, status pills, dense table
- `/documents/upload` — Full-page upload with per-file progress and links to uploaded docs
- `/documents/[id]` — Split layout: page viewer with bbox canvas overlay + blocks pane + ingestion timeline; SSE real-time status updates with polling fallback
- `/drafts/new` — Template picker (3-col grid, ochre selection), document selector (checkboxes), generate button
- `/drafts/[id]` — Two-pane: draft prose with CitedText inline citation chips + groundedness bar + edit mode / right pane citation source viewer
- `/admin` — Stats grid (4 cards), LLM calls-by-tier table with progress bars, rule extractor trigger, collapsible templates panel with ochre-numbered rules

### Components
- `components/layout/Shell.tsx` — top bar (wordmark, workspace label, spend badge), left nav rail (active state ochre), 28px status bar (api dot, worker slots, p95, cache)
- `components/layout/PageHeader.tsx` — eyebrow + h2 + actions slot
- `components/ui/` — button, status-pill, citation-chip, card, spinner, input, eyebrow, mono
- `components/CitedText.tsx` — parses `[chunk:ID]` markers inline, renders CitationChip with tooltip + selection state
- `components/PageWithBboxes.tsx` — `<img>` + `<canvas>` overlay, scales natural coords to rendered size
- `components/UploadDropzone.tsx` — drag/drop, 3-concurrent uploads, 429 exponential backoff, per-file status

### Lib
- `lib/types.ts` — all TypeScript types matching backend schemas
- `lib/api.ts` — typed async functions for every endpoint (no raw fetch outside this file)
- `lib/sse.ts` — `subscribeToDocumentEvents` using `@microsoft/fetch-event-source`, persists `lastEventId` in sessionStorage, 30s fallback to 5s polling (NN-10)

### Design
- Warm paper neutrals (`#fbf8f1`) background, white surfaces, `#d6cdb8` hairlines
- Single ochre accent (`#b8741a`) for primary actions and active states
- Geist Sans / Geist Mono / Source Serif 4 loaded via `next/font/google`, exposed as CSS vars
- All color tokens defined as CSS custom properties in `globals.css`
- Dense table-first layout, no shadows on resting elements, no emoji

### Tests (Playwright)
- `tests/upload.spec.ts` — upload flow, table render, error state
- `tests/draft_view.spec.ts` — citation click, groundedness bar, edit count badge
- `tests/edit_save.spec.ts` — edit mode toggle, cancel reverts, save calls `/api/edits`
- `tests/sse_reconnect.spec.ts` — SSE connection, Last-Event-ID header, polling fallback (NN-10)

## Deviations
- None. All specified screens, components, and lib files implemented as described in M11 spec.

## Follow-ups
- Add pagination support to the documents list (current implementation loads first page only)
- Add chunk page/bbox metadata display in the citation panel once the backend exposes it via the chunk endpoint
- Consider adding a toast notification system instead of `alert()` for save errors
