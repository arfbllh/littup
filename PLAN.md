# Implementation Plan — M3 (Document Ingestion API)

## Pre-flight state

M0, M1, M2 are complete. `app/ingest/` contains only `__init__.py` and
an empty `ocr/` package. No `app/api/routes/documents.py`, no
`app/api/sse.py`, no `app/api/schemas/documents.py`. `JobQueue` (M1)
already supports `enqueue(dedup_key=...)` and `pending_count()`. The
ingestion state machine is enumerated in `Document.status` (free text);
`Reconciler.find_partial_documents()` (M1) already re-enqueues missing
stages via `_NEXT_STAGE` and uses `reconcile:{doc_id}:{kind}` dedup
keys — M3 inherits that, no changes needed there beyond covering it
with an integration test.

The OCR `JobKind.OCR` is registered in the `JobKind` StrEnum but has no
handler in `HANDLERS`. The worker (M1) calls `get_handler(kind)` which
raises `IngestError("HANDLER_NOT_FOUND")` for unregistered kinds and
non-retryably fails the job. M3 will register an explicit stub handler
so that the **document** transitions to `failed` with
`error_code='OCR_HANDLER_NOT_IMPLEMENTED'` (not just the job) — that's
the visible end-to-end signal the milestone wants.

## Goal

Ship the HTTP surface and orchestration that gets a file from
`curl -F file=@x.pdf localhost:8000/api/documents` into the queue
with idempotency, backpressure, SSE replay, and crash recovery, all
integration-tested. No OCR yet — the document lands in `failed` with
`OCR_HANDLER_NOT_IMPLEMENTED`. M4 wires the real handler.

## Non-negotiables that apply

- **NN-1** — granular state machine; reconciler covers `*_running`
  doc statuses. Tested end-to-end (insert a stale `ocr_running` doc,
  run reconciler, assert OCR job enqueued; re-run, no dup).
- **NN-2** — atomic upsert via single
  `INSERT ... ON CONFLICT (sha256) DO UPDATE SET last_accessed_at = NOW()
   RETURNING id, status, (xmax = 0) AS inserted`. Concurrency test with
  `asyncio.gather` of two uploads of the same bytes → one document,
  one job.
- **NN-3** — `POST /api/documents` checks `JobQueue.pending_count()`
  before doing any work; if `>= settings.JOB_QUEUE_MAX_PENDING` (100),
  return `429` with `Retry-After: 10`.
- **NN-10** — SSE events have integer IDs from a per-document monotonic
  `seq`; endpoint honors `Last-Event-ID`; events persisted in
  `app.document_events` so reconnects can replay; 15s keep-alive
  comments.
- **NN-12** — `structlog` JSON + `X-Request-ID` already wired (M0);
  every ingest log line includes `document_id`, `sha256`, `request_id`.

## Files to create

### Migration

| File | Purpose |
|------|---------|
| `app/db/migrations/versions/0002_document_events.py` | New table `app.document_events` (`document_id UUID FK CASCADE, seq BIGINT, type TEXT, payload JSONB, ts TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (document_id, seq)`). The composite PK is the only index needed. |

### ORM

| File | Purpose |
|------|---------|
| `app/db/models/document.py` (modify) | Add `DocumentEvent` model matching the new table. |

### Ingest service

| File | Purpose |
|------|---------|
| `app/ingest/storage.py` | `LocalBlobStore`: `path_for(sha256, ext)` → `data/uploads/{sha256[:2]}/{sha256}.{ext}`; atomic `os.replace` from `UPLOAD_DIR/.tmp/`; `page_image_path(doc_id, n)` → `data/page_images/{doc_id}/{n}.png`. |
| `app/ingest/hashing.py` | `stream_to_tempfile_with_sha256(upload_file, chunk=1MiB) -> (Path, sha256_hex, size_bytes)` — single pass, no buffering whole file. |
| `app/ingest/mime.py` | `detect_mime(path) -> str` via `python-magic`; `ext_for(mime) -> str`. Allowlist constant; unknown → `IngestError(code='UNSUPPORTED_MIME')`. |
| `app/ingest/events.py` | `DocumentEventBus.emit(session, document_id, type, payload) -> seq` — `UPDATE app.documents SET last_event_seq = last_event_seq + 1 RETURNING last_event_seq` + `INSERT INTO app.document_events` in the same transaction. `replay(session, document_id, after_seq)` for SSE backfill. |
| `app/ingest/service.py` | `IngestService` — see "Service contract" below. |
| `app/ingest/page_render.py` | `render_page_png(pdf_path, page_n, dest_path)` via `pdf2image` (poppler). Cached under `data/page_images/{doc_id}/{n}.png`. |

### API

| File | Purpose |
|------|---------|
| `app/api/schemas/documents.py` | `UploadResponse`, `DocumentStatus`, `DocumentSummary`, `BlocksResponse`, `EventOut`. |
| `app/api/routes/documents.py` | The 6 endpoints listed below. Routes parse input, call `IngestService`, return responses — no business logic. |
| `app/api/sse.py` | `sse_response(generator, last_event_id, *, keepalive=15.0)`: framing helper yielding `id:`/`event:`/`data:` lines + `: keepalive` comments. |
| `app/api/deps.py` (modify) | `get_ingest_service(session) -> IngestService`. |
| `app/main.py` (modify) | `include_router(documents_router)`. |

### Job wiring

| File | Purpose |
|------|---------|
| `app/jobs/handlers/__init__.py` | New package. |
| `app/jobs/handlers/ocr_stub.py` | `async def handle_ocr(payload, session)`: load doc → set `status='ocr_running'` (emit `status_changed`) → set `status='failed', error_code='OCR_HANDLER_NOT_IMPLEMENTED'` (emit `failed`) → raise `IngestError(non-retryable=True)` so the job row is also marked failed. M4 replaces this. |
| `app/jobs/kinds.py` (modify) | `HANDLERS[JobKind.OCR] = handle_ocr` at import time. |
| `app/jobs/worker.py` & `app/main.py` (verify) | Both processes import `app.jobs.handlers.ocr_stub` so the registration side-effect runs. |

### Settings

| File | Purpose |
|------|---------|
| `app/settings.py` (modify) | Add `MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024`, `UPLOAD_DIR: str = "data/uploads"`, `PAGE_IMAGE_DIR: str = "data/page_images"`. (`JOB_QUEUE_MAX_PENDING` already exists.) |

### Tests

| File | Purpose |
|------|---------|
| `tests/integration/test_ingest_idempotency.py` | (a) Sequential same-file double upload → same id, `was_new` `True/False`, one job. (b) Concurrent `asyncio.gather` of two uploads with the **same bytes** through two `httpx.AsyncClient`s → same id, exactly one job, one file on disk. |
| `tests/integration/test_ingest_recovery.py` | Insert `Document(status='ocr_running', updated_at=NOW()-20min)` → `Reconciler.find_partial_documents()` enqueues OCR with `dedup_key='reconcile:{id}:ocr'`. Re-run → no dup. |
| `tests/integration/test_concurrency_cap.py` | Override `JOB_QUEUE_MAX_PENDING=5`; insert 6 pending OCR jobs directly; 7th upload → 429 + `Retry-After: 10`. Sanity: no document row created on 429. |
| `tests/integration/test_sse_reconnect.py` | Drive an upload, run the OCR stub to produce events. Open SSE, read up to seq=2, disconnect; reconnect with `Last-Event-ID: 2`; assert next events have seq>=3. Verify `GET /api/documents/{id}` matches the last emitted state. |
| `tests/integration/test_upload_validation.py` | (a) > `MAX_UPLOAD_BYTES` → 413. (b) Unknown MIME → 415 `UNSUPPORTED_MIME`. (c) Empty file → 400. |
| `tests/integration/conftest.py` (modify) | `httpx.AsyncClient` fixture against `ASGITransport(app=create_app())`; `tmp_uploads_dir` fixture redirecting settings to a tmp path. |

## Service contract

```python
class IngestService:
    async def upload(self, file: UploadFile) -> tuple[Document, bool]:
        # 1. Backpressure pre-check: pending_count() >= MAX
        #    → raise BackpressureError (→ 429 in route).
        # 2. Stream → tempfile in UPLOAD_DIR/.tmp/, compute SHA256, size.
        # 3. Validate size, detect MIME, validate against allowlist.
        # 4. Atomic upsert:
        #       INSERT INTO app.documents (sha256, filename, mime_type,
        #                                  size_bytes, status)
        #       VALUES (:sha, :name, :mime, :size, 'uploaded')
        #       ON CONFLICT (sha256) DO UPDATE
        #          SET last_accessed_at = NOW()
        #       RETURNING id, status, (xmax = 0) AS inserted
        # 5. If `inserted`:
        #       - os.replace(tempfile, data/uploads/{sha[:2]}/{sha}.{ext})
        #       - DocumentEventBus.emit(..., 'uploaded')
        #       - JobQueue.enqueue(OCR, {document_id}, dedup_key=f'ocr:{id}')
        #    Else:
        #       - tempfile.unlink()
        # 6. Return (Document, inserted).

    async def get_status(self, document_id: str) -> DocumentStatus: ...
    async def get_blocks(self, document_id: str) -> BlocksResponse:
        # {status, blocks: []} until M5.
    async def list_documents(self, limit, offset, status_filter) -> ListResp: ...
    async def stream_events(self, document_id, last_event_id) -> AsyncIterator[Event]:
        # Replay app.document_events WHERE seq > last_event_id, then
        # poll the table every 1s from a fresh short-lived session
        # (LISTEN/NOTIFY through pgbouncer transaction pooling is
        # unreliable). Cap stream at 10 min; client reconnects.
```

Route exception mapping:
- `BackpressureError → 429 + Retry-After: 10`
- `IngestError('UNSUPPORTED_MIME') → 415`
- `IngestError('FILE_TOO_LARGE')   → 413`
- `IngestError('EMPTY_FILE')        → 400`
- everything else falls through to the M0 `AppError` handler.

## Endpoint contract

| Method | Path | Returns |
|--------|------|---------|
| `POST` | `/api/documents` | `201 {document_id, status, was_new}` (or 429/4xx) |
| `GET`  | `/api/documents` | `{items: [...DocumentSummary], next_offset}` — query `?limit=20&offset=0&status=ready` |
| `GET`  | `/api/documents/{id}` | `DocumentStatus` with `error_code/error_message/page_count` |
| `GET`  | `/api/documents/{id}/blocks` | `{status, blocks: []}` (real blocks land in M5) |
| `GET`  | `/api/documents/{id}/pages/{n}` | PNG bytes; lazily renders via `pdf2image`, cached |
| `GET`  | `/api/documents/{id}/events` | `text/event-stream`, honors `Last-Event-ID`, 15s keepalive |

## Event types (initial)

- `uploaded` — emitted by `IngestService.upload` after insert
- `status_changed` — emitted by handlers on each status transition (`{from, to}`)
- `failed` — terminal, emitted by `ocr_stub` for M3

The event `type` is mirrored to the SSE `event:` field.

## Concurrency / atomicity notes

1. **Upsert race (NN-2):** `INSERT ... ON CONFLICT DO UPDATE
   RETURNING (xmax = 0) AS inserted` guarantees exactly one concurrent
   caller sees `inserted=true`. Enqueue is gated on that flag; the
   additional `dedup_key=f'ocr:{doc_id}'` is defense in depth. Note
   the reconciler uses a different `reconcile:{id}:ocr` key — both
   can coexist; only one will run because the worker claims one and
   the doc transition makes the other a no-op.
2. **Filesystem ordering:** stream → tempfile in `UPLOAD_DIR/.tmp` →
   `os.replace` to final path **after** DB insert succeeds. If
   `inserted=false`, unlink the tempfile without touching final
   storage. If a crash lands between insert and replace, the
   reconciler re-enqueues OCR and the OCR handler will see a missing
   source file → fail non-retryably with `SOURCE_FILE_MISSING`. That's
   an M4 concern; note in M3-DONE follow-ups.
3. **Backpressure window:** `pending_count()` is read before the
   upsert. Under contention a few uploads can squeak past the
   threshold — the threshold is approximate by design.
4. **SSE seq monotonicity:** `last_event_seq` increment + event insert
   share a transaction; concurrent emitters serialize on the
   row-level lock of the document.

## Risks / open questions

- **`python-magic` needs `libmagic1`** — confirm presence in the API
  Dockerfile; otherwise M3 will fail at runtime rather than test time.
- **`pdf2image` needs `poppler-utils`** — same concern; without it the
  page-image endpoint 500s. Add to Dockerfile; if dep is too heavy
  for M3 timeline, gate the endpoint behind a feature flag and skip
  its test.
- **SSE through pgbouncer (transaction pooling)** — long-lived
  connections don't fit; we poll the events table from short-lived
  sessions instead of `LISTEN/NOTIFY`. Document in code.
- **`app.document_events` growth** — append-only, never pruned for the
  demo; add a TODO to partition or TTL in v1.1.
- **OCR stub vs. unregistered handler** — the M1 worker already
  non-retryably fails unregistered kinds, but the acceptance
  criterion requires the *document* to land in `failed` with
  a **specific error code**. Only the stub can set that.

## Acceptance check matrix

| Criterion (from M3 spec) | Verified by |
|--------------------------|-------------|
| `curl` upload → `{document_id, status:"uploaded", was_new:true}` | manual smoke + `test_ingest_idempotency` |
| Re-upload → `was_new:false`, same id, one row | `test_ingest_idempotency` (seq + concurrent) |
| Status transitions to `ocr_pending → failed` with `OCR_HANDLER_NOT_IMPLEMENTED` | `test_sse_reconnect` (events) + `test_ingest_recovery` |
| `/events` streams transitions | `test_sse_reconnect` |
| `Last-Event-ID` replay | `test_sse_reconnect` |
| 7th upload over threshold → 429 | `test_concurrency_cap` |
| Reconciler re-queues stuck doc, idempotent | `test_ingest_recovery` |

## Out of scope (per spec)

- Real OCR (M4), per-document VLM budget (M4), document deletion
  (v1.1), soft delete (v1.1), `docling` layout, anything beyond a
  basic poppler call for page images.

## Definition of done

All acceptance tests green, ruff clean, `make up` + `curl` smoke
matches the criteria, `docs/milestones/M3-DONE.md` written mirroring
the M1/M2 format.
