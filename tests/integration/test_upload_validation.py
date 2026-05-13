"""Upload validation: oversize, unknown MIME, empty."""

import pytest

from app import settings as settings_module


@pytest.mark.asyncio
async def test_oversize_upload_returns_413(app_client, monkeypatch, sample_pdf_bytes):
    monkeypatch.setattr(settings_module.settings, "MAX_UPLOAD_BYTES", 100)
    payload = sample_pdf_bytes + b"\x00" * 1024  # comfortably over 100 bytes
    r = await app_client.post(
        "/api/documents", files={"file": ("big.pdf", payload, "application/pdf")}
    )
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


@pytest.mark.asyncio
async def test_unknown_mime_returns_415(app_client):
    r = await app_client.post(
        "/api/documents",
        files={"file": ("evil.bin", b"\x00\x01\x02junk-bytes", "application/octet-stream")},
    )
    assert r.status_code == 415, r.text
    assert r.json()["error"]["code"] == "UNSUPPORTED_MIME"


@pytest.mark.asyncio
async def test_empty_upload_returns_400(app_client):
    r = await app_client.post(
        "/api/documents", files={"file": ("empty.pdf", b"", "application/pdf")}
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "EMPTY_FILE"
