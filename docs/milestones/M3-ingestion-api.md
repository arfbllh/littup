# M3 — Document Ingestion API

**Estimated time:** 1.5 hours
**Dependencies:** M1, M2 (M2 not strictly required, but routes share `deps.py`)
**Rubric impact:** Document Processing (foundation); also where NN-1/NN-2/NN-3/NN-10 all visibly land

## Goal

The HTTP surface and orchestration that gets a file from a user into the pipeline safely. After this milestone, you can `curl -F file=@doc.pdf localhost:8000/api/documents`, get back a `document_id`, watch its status via SSE or polling, and re-upload the same file to confirm idempotency. No OCR yet — the job is enqueued and immediately marked `failed` ("no handler") by the worker. That's expected; M4 wires the handler.

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-1, NN-2, NN-3, NN-10**
2. `docs/architecture/03-components/ingestion-ocr.md` — interface section
3. `docs/architecture/project-skeleton.md`

## Non-Negotiables that apply

- **NN-1** — document state machine; reconciliation sweeper integrates here
- **NN-2** — atomic `INSERT ... ON CONFLICT ... RETURNING (xmax = 0) AS inserted`
- **NN-3** — backpressure on upload when queue depth exceeds threshold
- **NN-10** — SSE has `Last-Event-ID` support; status endpoint is the source of truth

## Files to create / modify

### Ingest service

- `app/ingest/__init__.py`
- `app/ingest/service.py` — `IngestService`:
  - `async upload(file: UploadFile, content_hash_known: str | None = None) -> tuple[Document, bool]`
    - Streams the file to a temp path, computes SHA256 while streaming (don't load into memory)
    - Detects MIME type via `python-magic`
    - Validates size against `MAX_UPLOAD_BYTES`
    - Runs the atomic upsert (NN-2)
    - If newly inserted, writes file to local storage at `data/uploads/{sha256[:2]}/{sha256}.{ext}`, enqueues the first stage job (`OCR`), and bumps `last_event_seq` with an `uploaded` event
    - Returns `(document, was_new)`
  - `async get_status(document_id) -> DocumentStatus`
  - `async get_blocks(document_id) -> list[Block]` (returns empty list until OCR is done; structured response includes `status`)
  - `async list_documents(limit, offset, status_filter)` for the dashboard

### API routes

- `app/api/schemas/documents.py` — Pydantic request/response shapes
- `app/api/routes/documents.py`:
  - `POST /api/documents` — multipart upload. Checks `JobQueue.pending_count()` first; if over threshold, returns 429 with `Retry-After: 10`. Calls `IngestService.upload`. Returns `{document_id, status, was_new}`.
  - `GET /api/documents` — paginated list
  - `GET /api/documents/{id}` — current state, status, page count, error info
  - `GET /api/documents/{id}/blocks` — block tree (empty until M5)
  - `GET /api/documents/{id}/pages/{n}` — returns the rendered page image as PNG (used by UI for citation highlighting; for now, generates from PDF via `pdf2image` on demand — caches in `data/page_images/`)
  - `GET /api/documents/{id}/events` (SSE) — see below
- `app/api/sse.py` — `sse_stream(generator, last_event_id: int)`:
  - Honors `Last-Event-ID` header
  - Replays events from `documents.last_event_seq` history (kept in a small `app.document_events` table — add to M1 if missed, otherwise compute on-the-fly from status transitions; pragmatic choice: a `document_events` table with `(document_id, seq, type, payload, ts)` columns; cheapest to ship)
  - Emits keep-alive comments every 15s

### Reconciler handler wiring

- `app/jobs/reconciler.py` — implement `find_partial_documents()` to re-enqueue the missing stage:
  ```
  status='ocr_running'      → re-enqueue OCR job
  status='layout_running'   → re-enqueue LAYOUT job
  status='chunking_running' → re-enqueue CHUNKING job
  status='embedding_running'→ re-enqueue EMBEDDING job
  ```
  Each re-enqueue uses a `dedup_key` derived from `(document_id, kind)` so the same recovery doesn't double-enqueue.

### Worker handler stub

- `app/jobs/kinds.py` — register an OCR handler that just sets `status='failed'` with `error_code='OCR_HANDLER_NOT_IMPLEMENTED'`. M4 replaces this. The point is to prove the queue + worker + state-machine + SSE all work end-to-end before the OCR work begins.

### Tests

- `tests/integration/test_ingest_idempotency.py` (NN-2):
  - Upload the same file twice sequentially; assert same `document_id`, `was_new` is `True` then `False`, only one job enqueued
  - Upload the same file twice **concurrently** using `asyncio.gather`; assert same `document_id`, exactly one job in `jobs.jobs`. This is the critical test — implement it with two concurrent `httpx.AsyncClient` calls.
- `tests/integration/test_ingest_recovery.py` (NN-1):
  - Insert a `Document` with `status='ocr_running'` and `updated_at = NOW() - INTERVAL '20 minutes'`
  - Run `Reconciler.find_partial_documents()`
  - Assert a new OCR job was enqueued with the right `document_id`
  - Run reconciler again; assert no duplicate job (dedup_key)
- `tests/integration/test_concurrency_cap.py` (NN-3):
  - Set the backpressure threshold to 5; insert 6 pending jobs; assert 7th upload returns 429 with `Retry-After`
- `tests/integration/test_sse_reconnect.py` (NN-10):
  - Subscribe to SSE for a document; receive events 1–3; disconnect; reconnect with `Last-Event-ID: 2`; assert event 3 is replayed (or the next event if 3 already passed)
  - Verify `GET /api/documents/{id}` returns identical state to the latest SSE event

## Acceptance criteria

- [ ] `curl -F file=@x.pdf localhost:8000/api/documents` returns `{document_id, status:"uploaded", was_new:true}`
- [ ] Same call again returns `was_new:false` with the same `document_id`; only one row in `app.documents`
- [ ] After ~1s, status transitions to `ocr_pending`, then to `failed` with `OCR_HANDLER_NOT_IMPLEMENTED` (intentional — M4 fixes)
- [ ] `GET /api/documents/{id}/events` streams the transitions
- [ ] Reconnecting to SSE with `Last-Event-ID` replays missed events
- [ ] 7th upload over the backpressure threshold returns 429
- [ ] Reconciler integration test green

## Out of scope

- Real OCR — that's M4
- Per-document VLM budget (NN-6) — surface in M4
- Document deletion — v1.1
- Soft delete — v1.1

## Definition of done

The pipeline plumbing carries a file from `curl` to a `failed` state via the queue, with idempotency, backpressure, SSE replay, and crash recovery all integration-tested. `M3-DONE.md` written.

## Sub-agent delegation

Not recommended — the routes, service, and tests are tightly coupled. Sequential build is faster.
