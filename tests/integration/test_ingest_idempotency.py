"""NN-2: atomic content-hash idempotency on POST /api/documents."""

import asyncio

import pytest
from sqlalchemy import text


async def _doc_count(session, sha: str) -> int:
    r = await session.execute(
        text("SELECT COUNT(*) FROM app.documents WHERE sha256 = :sha"), {"sha": sha}
    )
    return r.scalar_one()


async def _ocr_job_count(session, doc_id: str) -> int:
    r = await session.execute(
        text(
            """
            SELECT COUNT(*) FROM jobs.jobs
            WHERE payload->>'document_id' = :id AND kind = 'ocr'
            """
        ),
        {"id": doc_id},
    )
    return r.scalar_one()


@pytest.mark.asyncio
async def test_sequential_double_upload_is_idempotent(
    app_client, db_session, sample_pdf_bytes
):
    files = {"file": ("a.pdf", sample_pdf_bytes, "application/pdf")}

    r1 = await app_client.post("/api/documents", files=files)
    assert r1.status_code == 201, r1.text
    body1 = r1.json()
    assert body1["was_new"] is True

    r2 = await app_client.post(
        "/api/documents", files={"file": ("a.pdf", sample_pdf_bytes, "application/pdf")}
    )
    assert r2.status_code == 201
    body2 = r2.json()
    assert body2["was_new"] is False
    assert body2["document_id"] == body1["document_id"]

    assert await _doc_count(db_session, body1["sha256"]) == 1
    assert await _ocr_job_count(db_session, body1["document_id"]) == 1


@pytest.mark.asyncio
async def test_concurrent_double_upload_creates_one_doc_and_one_job(
    app_client, db_session, sample_pdf_bytes
):
    payload1 = {"file": ("a.pdf", sample_pdf_bytes, "application/pdf")}
    payload2 = {"file": ("a.pdf", sample_pdf_bytes, "application/pdf")}

    r1, r2 = await asyncio.gather(
        app_client.post("/api/documents", files=payload1),
        app_client.post("/api/documents", files=payload2),
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    b1, b2 = r1.json(), r2.json()
    assert b1["document_id"] == b2["document_id"]
    # Exactly one of them must have observed the insert
    assert {b1["was_new"], b2["was_new"]} == {True, False}

    assert await _doc_count(db_session, b1["sha256"]) == 1
    assert await _ocr_job_count(db_session, b1["document_id"]) == 1
