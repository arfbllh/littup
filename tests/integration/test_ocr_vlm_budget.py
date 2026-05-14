"""Integration: NN-6 — VLM per-document cap is enforced atomically.

With MAX_VLM_PAGES_PER_DOC=2 and a 3-page document where every page
escalates to VLM, exactly 2 VLM calls happen.  The 3rd page lands with
source='vlm_budget_exceeded'.  The document still reaches 'ocr_done'.
"""

from __future__ import annotations

import pytest
import numpy as np
from sqlalchemy import text


# ── Fixtures / helpers ────────────────────────────────────────────────────────

class _MockPaddleLowConf:
    """PaddleOCR stub that always returns a single low-confidence span."""

    def ocr(self, image, cls=True):
        h, w = image.shape[:2]
        return [
            [
                [[[0, 0], [w, 0], [w, h // 4], [0, h // 4]], ("test text", 0.2)]
            ]
        ]


class _MockVlmRouter:
    """LLMRouter stub that records vision calls and returns a valid word list."""

    def __init__(self) -> None:
        self.vision_calls: int = 0

    async def generate(self, messages, *, task, schema=None, sampling=None, cache=True, trace_id=None):
        from app.llm.types import LLMResponse

        if task == "vision":
            self.vision_calls += 1
        return LLMResponse(
            structured={"lines": [{"text": "handwritten word", "bbox": [0.1, 0.1, 0.4, 0.2]}]},
            text="",
            model_used="mock-vlm",
            provider="mock",
            tokens_in=100,
            tokens_out=30,
        )


# ── Test ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vlm_cap_exactly_two_calls(
    ocr_ingest_service,
    db_session,
    tmp_uploads_dir,
    cleanup_documents_and_jobs,
    monkeypatch,
):
    """NN-6 acceptance test.

    Setup:
    - PaddleOCR always returns confidence 0.2 (triggers VLM escalation)
    - OCRConfig: threshold=1.5 (always escalate), cap=2
    - Classifier config: blurry_laplacian_threshold=10_000 (all pages → BLURRY_SCAN)
    - 3-page document (rasteriser mocked to return 3 synthetic images)

    Assertions:
    - Exactly 2 VLM calls happened
    - Page 3 has source = vlm_budget_exceeded in DB
    - Document status = ocr_done (partial VLM budget does not fail the document)
    - documents.vlm_pages_used = 2
    """
    import app.ingest.ocr.paddle_ocr as paddle_module
    from app.ingest.ocr.base import ClassifyConfig, OCRConfig, reset_ocr_config_cache
    from app.ingest.service import IngestService

    # Install mock PaddleOCR before the test (release any real instance)
    paddle_module.release_paddle()
    monkeypatch.setattr(paddle_module, "_paddle_instance", _MockPaddleLowConf())

    # Config: always escalate, cap=2
    reset_ocr_config_cache()
    test_cfg = OCRConfig(
        max_vlm_pages_per_doc=2,
        paddleocr_confidence_threshold=1.5,
        classify=ClassifyConfig(
            blurry_laplacian_variance_threshold=10_000,  # everything → BLURRY_SCAN
            handwriting_stroke_ratio_threshold=0.35,
        ),
    )

    # 3 synthetic "pages"
    synthetic_pages = [np.ones((300, 200, 3), dtype=np.uint8) * 200] * 3

    async def _mock_rasterise(file_path, *, is_pdf, native=False):  # noqa: ARG001
        return [p.copy() for p in synthetic_pages]

    monkeypatch.setattr(ocr_ingest_service, "_rasterise_pages", _mock_rasterise)

    # Upload a minimal (but valid-enough-for-upload) PDF
    from io import BytesIO
    import uuid
    from fastapi import UploadFile

    unique = uuid.uuid4().hex.encode()
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n0000000010 00000 n \n0000000053 00000 n \n0000000098 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n149\n%%EOF\n%% " + unique + b"\n"
    )
    upload = UploadFile(filename="handwriting_test.pdf", file=BytesIO(pdf_bytes))
    upload_result = await ocr_ingest_service.upload(upload)
    doc_id = upload_result.document_id

    # Wire mock VLM router
    mock_router = _MockVlmRouter()

    # Run OCR
    await ocr_ingest_service.ocr_document(doc_id, llm_router=mock_router, config=test_cfg)

    # ── Assertions ────────────────────────────────────────────────────────────

    # Exactly 2 VLM calls
    assert mock_router.vision_calls == 2, (
        f"Expected exactly 2 VLM calls, got {mock_router.vision_calls}"
    )

    # Document status and vlm_pages_used
    doc_row = await db_session.execute(
        text("SELECT status, vlm_pages_used FROM app.documents WHERE id = :id"),
        {"id": doc_id},
    )
    doc = doc_row.fetchone()
    assert doc.status == "ocr_done", f"Expected ocr_done, got {doc.status}"
    assert doc.vlm_pages_used == 2

    # Page statuses
    pages_q = await db_session.execute(
        text(
            "SELECT page_number, status FROM app.pages WHERE document_id = :d ORDER BY page_number"
        ),
        {"d": doc_id},
    )
    pages = pages_q.fetchall()
    assert len(pages) == 3

    budget_exceeded_pages = [p for p in pages if p.status == "vlm_budget_exceeded"]
    assert len(budget_exceeded_pages) == 1
    assert budget_exceeded_pages[0].page_number == 3
