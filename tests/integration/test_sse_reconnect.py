"""SSE replay honors Last-Event-ID.

Note: we test the streaming generator directly rather than via httpx
ASGI transport, which buffers StreamingResponse chunks and would make
this test flaky."""

import pytest
from fastapi import Request

from app.api.sse import parse_last_event_id, stream_document_events
from app.ingest.events import DocumentEventBus


class _FakeRequest:
    """Quacks like fastapi.Request for is_disconnected()."""

    def __init__(self):
        self._disconnected = False

    async def is_disconnected(self) -> bool:
        return self._disconnected


def _parse_sse_blob(blob: bytes) -> list[dict]:
    frames: list[dict] = []
    for raw in blob.split(b"\n\n"):
        if not raw.strip() or raw.startswith(b":"):
            continue
        frame = {"id": None, "event": None, "data": ""}
        for line in raw.decode("utf-8").splitlines():
            if line.startswith("id:"):
                frame["id"] = int(line.split(":", 1)[1].strip())
            elif line.startswith("event:"):
                frame["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                frame["data"] = line.split(":", 1)[1].strip()
        frames.append(frame)
    return frames


def test_parse_last_event_id():
    assert parse_last_event_id(None) == 0
    assert parse_last_event_id("") == 0
    assert parse_last_event_id("garbage") == 0
    assert parse_last_event_id("3") == 3
    assert parse_last_event_id("-1") == 0


@pytest.mark.asyncio
async def test_sse_replay_with_last_event_id(
    app_client, test_session_factory, sample_pdf_bytes
):
    r = await app_client.post(
        "/api/documents",
        files={"file": ("sse.pdf", sample_pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 201
    doc_id = r.json()["document_id"]

    async with test_session_factory() as s:
        await DocumentEventBus.emit(s, doc_id, "status_changed", {"to": "ocr_pending"})
        await DocumentEventBus.emit(s, doc_id, "status_changed", {"to": "ocr_running"})
        await DocumentEventBus.emit(s, doc_id, "failed", {"to": "failed"})
        await s.commit()

    # Connect from scratch — should replay all 4 events.
    req = _FakeRequest()
    gen = stream_document_events(
        req,
        doc_id,
        last_event_id=0,
        session_factory=test_session_factory,
        keepalive=10.0,
        poll_interval=0.05,
        max_seconds=0.3,
    )
    chunks = b""
    async for chunk in gen:
        chunks += chunk
    frames = _parse_sse_blob(chunks)
    ids = [f["id"] for f in frames if f["id"] is not None]
    assert ids[:4] == [1, 2, 3, 4]
    types = [f["event"] for f in frames if f["id"] is not None][:4]
    assert types == ["uploaded", "status_changed", "status_changed", "failed"]

    # Reconnect with Last-Event-ID=2 → only seq>2 should arrive.
    req2 = _FakeRequest()
    gen2 = stream_document_events(
        req2,
        doc_id,
        last_event_id=2,
        session_factory=test_session_factory,
        keepalive=10.0,
        poll_interval=0.05,
        max_seconds=0.3,
    )
    chunks2 = b""
    async for chunk in gen2:
        chunks2 += chunk
    frames2 = _parse_sse_blob(chunks2)
    ids2 = [f["id"] for f in frames2 if f["id"] is not None]
    assert min(ids2) >= 3
    assert 4 in ids2


@pytest.mark.asyncio
async def test_get_document_matches_last_event(app_client, sample_pdf_bytes):
    r = await app_client.post(
        "/api/documents",
        files={"file": ("status.pdf", sample_pdf_bytes, "application/pdf")},
    )
    doc_id = r.json()["document_id"]

    status_resp = await app_client.get(f"/api/documents/{doc_id}")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] in ("uploaded", "ocr_pending", "ocr_running", "failed")
    assert body["last_event_seq"] >= 1


@pytest.mark.asyncio
async def test_sse_endpoint_returns_eventstream_headers(app_client, sample_pdf_bytes):
    r = await app_client.post(
        "/api/documents",
        files={"file": ("hdr.pdf", sample_pdf_bytes, "application/pdf")},
    )
    doc_id = r.json()["document_id"]

    # We only need to confirm headers; close immediately so the stream
    # doesn't keep us tied up.
    async with app_client.stream("GET", f"/api/documents/{doc_id}/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
