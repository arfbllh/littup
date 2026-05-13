# M3 — Document Ingestion API: DONE

**Date shipped:** 2026-05-14
**NN rules verified:** NN-1, NN-2, NN-3, NN-10, NN-12

## What shipped

### Migration

- `app/db/migrations/versions/0002_document_events.py` — `app.document_events`
  table `(document_id UUID FK CASCADE, seq BIGINT, type TEXT, payload JSONB,
  ts TIMESTAMPTZ, PRIMARY KEY (document_id, seq))` for SSE replay (NN-10).

### ORM

- `app/db/models/document.py` — added `DocumentEvent` model + back-populated
  `Document.events` relationship.

### Ingest service (`app/ingest/`)

- `storage.py` — `LocalBlobStore`: `path_for(sha[:2]/sha.ext)`,
  `tmp_dir`, atomic `commit(tmp, final)` (os.replace on same FS),
  `page_image_path(doc_id, n)`.
- `hashing.py` — `stream_to_tempfile(upload, tmp_dir, max_bytes)`:
  single-pass SHA256 + size check. Typed `FileTooLargeError` (413) and
  `EmptyFileError` (400).
- `mime.py` — magic-byte sniffer for PDF/PNG/JPEG/TIFF (no `python-magic`
  system dep). `UnsupportedMimeError` (415). DOCX rejected until M4 wires
  proper OOXML inspection.
- `events.py` — `DocumentEventBus.emit(session, doc_id, type, payload)`
  atomically bumps `documents.last_event_seq` and inserts
  `document_events`; `replay(session, doc_id, after_seq)` for SSE backfill.
- `page_render.py` — `render_page_png(pdf_path, n, dest, dpi=144)`
  lazily imports `pdf2image` so the route surfaces a typed 503
  (`PAGE_RENDER_UNAVAILABLE`) rather than blowing up at import time when
  poppler isn't installed.
- `service.py` — `IngestService`:
  - `upload(file)` — NN-3 pre-check → stream-hash to tempfile under
    `UPLOAD_DIR/.tmp/` → MIME detect → **single-statement** atomic upsert
    (NN-2: `INSERT ... ON CONFLICT (sha256) DO UPDATE SET last_accessed_at
    = NOW() RETURNING id, (xmax = 0) AS inserted`) → on insert, `os.replace`
    to final path + emit `uploaded` event + enqueue OCR with
    `dedup_key='ocr:{id}'`; on duplicate, unlink tempfile.
  - `get_document`, `list_documents`, `get_blocks`, `replay_events`.

### API (`app/api/`)

- `schemas/documents.py` — `UploadResponse`, `DocumentStatus`,
  `DocumentSummary`, `DocumentList`, `BlocksResponse`, `EventOut`.
- `sse.py` — `parse_last_event_id()` + `stream_document_events(request,
  doc_id, last_event_id, *, session_factory=None, keepalive, poll_interval,
  max_seconds)` — replays first, then polls the events table from
  short-lived sessions (pgbouncer transaction pooling makes LISTEN/NOTIFY
  unsafe). Frames carry `id:`/`event:`/`data:` and `: keepalive` comments
  every `SSE_KEEPALIVE_SECONDS`. Session factory is resolved lazily so
  tests can patch `app.db.session.async_session_factory`.
- `routes/documents.py`:
  - `POST /api/documents` (201) — multipart upload.
  - `GET /api/documents` — paginated list, optional `?status=`.
  - `GET /api/documents/{id}` — `DocumentStatus`.
  - `GET /api/documents/{id}/blocks` — `{status, blocks: []}` until M5.
  - `GET /api/documents/{id}/pages/{n}` — PNG via cached `pdf2image`.
  - `GET /api/documents/{id}/events` — `text/event-stream`, honors
    `Last-Event-ID`.

### Worker / handler wiring

- `app/jobs/handlers/__init__.py` — package import registers handlers.
- `app/jobs/handlers/ocr_stub.py` — `uploaded → ocr_pending →
  ocr_running → failed (OCR_HANDLER_NOT_IMPLEMENTED)`, emits events at
  each step. Non-retryable via `IngestError.retryable=False`; idempotent
  on retries (a failed doc just returns success).
- `app/jobs/worker.py` — imports `app.jobs.handlers` for the registration
  side-effect; honors `getattr(exc, "retryable", True)` when failing jobs.
- `app/main.py` — same registration import; mounts the documents router.

### Errors

- `app/core/errors.py` — `AppError` now carries a `retryable: bool = True`
  flag the worker reads on handler failure.

### Settings

- `MAX_UPLOAD_BYTES=50 MiB`, `UPLOAD_DIR`, `PAGE_IMAGE_DIR`,
  `SSE_KEEPALIVE_SECONDS=15`, `SSE_POLL_INTERVAL_SECONDS=1.0`,
  `SSE_MAX_STREAM_SECONDS=600`.

## Tests

All M3 acceptance criteria covered by integration tests:

- `test_ingest_idempotency.py` (NN-2)
  - `test_sequential_double_upload_is_idempotent` — same id, `was_new`
    flips True→False, one doc row, one OCR job.
  - `test_concurrent_double_upload_creates_one_doc_and_one_job` —
    `asyncio.gather` of two uploads with the same bytes; exactly one
    `was_new=True` returned; one doc row, one OCR job.
- `test_ingest_recovery.py` (NN-1) — stale `ocr_running` doc → reconciler
  enqueues OCR with `dedup_key='reconcile:{id}:ocr'`; second sweep is a
  no-op.
- `test_concurrency_cap.py` (NN-3) — `JOB_QUEUE_MAX_PENDING=5` + 6
  pending OCR jobs → 7th upload `429 QUEUE_SATURATED` + `Retry-After: 10`.
- `test_sse_reconnect.py` (NN-10)
  - `test_parse_last_event_id` — parser robustness.
  - `test_sse_replay_with_last_event_id` — full replay from seq 0, then
    reconnect with `Last-Event-ID: 2` returns only seq>2. Drives the
    generator directly (ASGITransport buffers `StreamingResponse` chunks
    and would make a live-streaming test flaky).
  - `test_get_document_matches_last_event` — `GET /api/documents/{id}`
    matches the last emitted event's state.
  - `test_sse_endpoint_returns_eventstream_headers` — content-type +
    200 from the route.
- `test_upload_validation.py` — 413/415/400 for oversize/unknown-MIME/empty.

`ruff check` is clean on all M3 additions.

## Deviations from spec

- **`python-magic` swapped for header sniffing.** The allowlist is small
  and the magic numbers are stable, so requiring `libmagic1` in every
  container felt premature. Easy to swap in for M4 if heterogeneous
  document inputs justify it.
- **DOCX deferred to M4.** The header-sniffer rejects raw ZIPs because
  generic `.zip` would otherwise misclassify; proper OOXML inspection
  belongs with the rest of M4's parsing work.
- **`pdf2image` is a lazy import.** The pages endpoint returns 503
  `PAGE_RENDER_UNAVAILABLE` if poppler isn't present, instead of failing
  at app boot. Spec called this an acceptable v1.x trade-off.
- **SSE polls instead of LISTEN/NOTIFY.** pgbouncer transaction-pooling
  mode breaks long-lived connections; polling the events table from
  short-lived sessions is the pragmatic alternative.
- **OCR stub is non-retryable.** The worker previously failed every
  handler retryably (3 attempts). Added `AppError.retryable` and made
  the worker honor it so the stub's failure doesn't produce three event
  bursts per upload. Stub also short-circuits on already-`failed` docs.

## Post-ship fixes (same session)

- `app/api/sse.py` initially captured `async_session_factory` at import
  time, which made the test fixture's monkeypatch a no-op. Changed to a
  lazy module-level lookup so `monkeypatch.setattr(app.db.session,
  "async_session_factory", ...)` is honored.
- Integration test conftest added a `cleanup_documents_and_jobs` fixture
  that truncates `app.documents`, `jobs.jobs`, and `jobs.job_history`
  after each ingest test; without it `test_job_queue` (which runs
  alphabetically after) saw residual OCR jobs and failed `claim.id ==
  job.id` assertions.
- `sample_pdf_bytes` randomized per call (uuid in a trailing comment) so
  the shared integration DB doesn't dedup across tests.

## Follow-ups

- **M4:** real OCR handler replaces `app/jobs/handlers/ocr_stub.py`; add
  per-document VLM page cap (NN-6); detect missing source file →
  `SOURCE_FILE_MISSING` non-retryable.
- **M4:** add `libmagic1` + `poppler-utils` to the API Dockerfile if/when
  M4 needs them.
- **v1.1:** partition or TTL `app.document_events` (currently
  append-only, never pruned).
- **v1.1:** document deletion + soft delete endpoints.

## Acceptance check matrix

| Criterion | Verified |
|-----------|----------|
| `curl` upload returns `{document_id, status, was_new}` | `test_sequential_double_upload_is_idempotent` |
| Re-upload same file: `was_new=false`, same id, one row | same test |
| Concurrent re-upload: one row, one job | `test_concurrent_double_upload_creates_one_doc_and_one_job` |
| Status transitions `uploaded → ocr_pending → ocr_running → failed (OCR_HANDLER_NOT_IMPLEMENTED)` | `test_sse_replay_with_last_event_id` (event sequence) + stub idempotency in `ocr_stub.py` |
| `/events` streams transitions | same |
| `Last-Event-ID` replay | same |
| 7th upload above threshold → 429 | `test_upload_returns_429_when_queue_above_threshold` |
| Reconciler re-queues stuck doc, idempotent | `test_stuck_doc_is_requeued_idempotently` |
