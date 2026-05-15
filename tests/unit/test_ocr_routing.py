"""Unit tests for OCR routing decisions (no DB, no real OCR model).

Covers:
- CLEAN_SCAN low-confidence pages retry once with preprocessing before
  considering VLM (Fix 2).
- VLM escalation fires on low confidence regardless of page type (Fix 3:
  loosened gate). Previously only BLURRY_SCAN / HANDWRITING_LIKELY escalated.
- DEGRADED_SCAN pages get preprocessed up front (Fix 1).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from app.ingest.classifier import PageType
from app.ingest.ocr.base import (
    OCRConfig,
    PageExtraction,
    reset_ocr_config_cache,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_config_cache():
    reset_ocr_config_cache()
    yield
    reset_ocr_config_cache()


@pytest.fixture
def fake_session():
    """Async session stub: claim_vlm_page always succeeds."""
    session = MagicMock()
    result = MagicMock()
    result.rowcount = 1
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _page_image() -> np.ndarray:
    return np.ones((300, 200, 3), dtype=np.uint8) * 200


def _extraction(conf: float, source: str = "paddleocr") -> PageExtraction:
    return PageExtraction(
        page_num=1, spans=[], full_text="", mean_confidence=conf, source=source
    )


class _PaddleStub:
    """Captures the page_image passed in across successive calls."""

    def __init__(self, confidences: list[float]) -> None:
        self._confidences = list(confidences)
        self.calls: list[np.ndarray] = []

    async def extract_page(self, source_path, page_num, *, page_image=None):
        self.calls.append(page_image)
        conf = self._confidences.pop(0) if self._confidences else 0.95
        return _extraction(conf)


class _RecordingVlmRouter:
    def __init__(self) -> None:
        self.vision_calls: int = 0

    async def generate(self, messages, *, task, schema=None, sampling=None, cache=True, trace_id=None):
        from app.llm.types import LLMResponse

        if task == "vision":
            self.vision_calls += 1
        return LLMResponse(
            structured={"lines": [{"text": "ok", "bbox": [0.1, 0.1, 0.4, 0.2]}]},
            text="",
            model_used="mock-vlm",
            provider="mock",
            tokens_in=10,
            tokens_out=3,
        )


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clean_scan_low_confidence_retries_with_preprocessing(
    fake_session, monkeypatch
):
    """A CLEAN_SCAN that PaddleOCR underperforms on should be retried once
    with full preprocessing. If the retry beats the initial pass, its result
    is returned (and no VLM is needed)."""
    from app.ingest.ocr import routing

    # First call: low conf (0.3). Retry: above threshold (0.9).
    paddle = _PaddleStub(confidences=[0.3, 0.9])
    monkeypatch.setattr(
        routing, "classify_page", lambda *a, **kw: (PageType.CLEAN_SCAN, 0.9)
    )
    monkeypatch.setattr(
        "app.ingest.ocr.paddle_ocr.PaddleProvider", lambda: paddle
    )
    # Pin OCR_PREPROCESS_ALL=False so CLEAN_SCAN doesn't preprocess on the first
    # pass — that's the scenario this test is designed to verify.
    import app.settings as _app_settings
    monkeypatch.setattr(_app_settings.settings, "OCR_PREPROCESS_ALL", False)

    result = await routing.route_and_extract(
        page_image=_page_image(),
        source_path=Path("/tmp/whatever.pdf"),
        page_num=1,
        has_text_layer=False,
        doc_id="doc-1",
        session=fake_session,
        config=OCRConfig(paddleocr_confidence_threshold=0.7),
        llm_router=None,
    )

    assert len(paddle.calls) == 2, "Expected one initial pass + one retry"
    assert result.mean_confidence == pytest.approx(0.9)
    # Retry input should differ from the initial (unprocessed) image.
    assert not np.array_equal(paddle.calls[0], paddle.calls[1])


@pytest.mark.asyncio
async def test_clean_scan_escalates_to_vlm_when_retry_still_fails(
    fake_session, monkeypatch
):
    """Loosened gate (Fix 3): CLEAN_SCAN whose retry is still below threshold
    must escalate to VLM. Old code refused escalation for CLEAN_SCAN even at
    confidence 0.1."""
    from app.ingest.ocr import routing

    # Initial 0.2, retry 0.25 — both well below 0.7 threshold.
    paddle = _PaddleStub(confidences=[0.2, 0.25])
    monkeypatch.setattr(
        routing, "classify_page", lambda *a, **kw: (PageType.CLEAN_SCAN, 0.9)
    )
    monkeypatch.setattr(
        "app.ingest.ocr.paddle_ocr.PaddleProvider", lambda: paddle
    )

    vlm_extracted = _extraction(0.95, source="vlm")

    class _VlmStub:
        def __init__(self, router):
            self.router = router

        async def extract_page(self, source_path, page_num, *, page_image=None):
            return vlm_extracted

    monkeypatch.setattr("app.ingest.ocr.vlm_ocr.VlmProvider", _VlmStub)

    result = await routing.route_and_extract(
        page_image=_page_image(),
        source_path=Path("/tmp/whatever.pdf"),
        page_num=1,
        has_text_layer=False,
        doc_id="doc-2",
        session=fake_session,
        config=OCRConfig(paddleocr_confidence_threshold=0.7),
        llm_router=_RecordingVlmRouter(),
    )

    assert result is vlm_extracted, "VLM result should win when PaddleOCR can't recover"
    assert len(paddle.calls) == 2, "Initial pass + retry, then VLM"


@pytest.mark.asyncio
async def test_degraded_scan_preprocessed_upfront(fake_session, monkeypatch):
    """DEGRADED_SCAN pages should be preprocessed before the first PaddleOCR
    pass — same path as BLURRY_SCAN / HANDWRITING_LIKELY."""
    from app.ingest.ocr import routing

    paddle = _PaddleStub(confidences=[0.95])  # one call, high confidence
    monkeypatch.setattr(
        routing, "classify_page", lambda *a, **kw: (PageType.DEGRADED_SCAN, 0.4)
    )
    monkeypatch.setattr(
        "app.ingest.ocr.paddle_ocr.PaddleProvider", lambda: paddle
    )

    original = _page_image()
    await routing.route_and_extract(
        page_image=original,
        source_path=Path("/tmp/whatever.pdf"),
        page_num=1,
        has_text_layer=False,
        doc_id="doc-3",
        session=fake_session,
        config=OCRConfig(),
        llm_router=None,
    )

    assert len(paddle.calls) == 1
    # The image handed to Paddle should be the *preprocessed* one, not the raw
    # page. After deskew+denoise+binarize the array contents differ from the
    # original constant-200 image.
    assert not np.array_equal(paddle.calls[0], original)


@pytest.mark.asyncio
async def test_low_confidence_no_router_does_not_call_vlm(
    fake_session, monkeypatch
):
    """If llm_router is None, low confidence still returns the PaddleOCR
    extraction — the gate is unchanged on the router-missing side."""
    from app.ingest.ocr import routing

    paddle = _PaddleStub(confidences=[0.1, 0.1])
    monkeypatch.setattr(
        routing, "classify_page", lambda *a, **kw: (PageType.CLEAN_SCAN, 0.9)
    )
    monkeypatch.setattr(
        "app.ingest.ocr.paddle_ocr.PaddleProvider", lambda: paddle
    )

    result = await routing.route_and_extract(
        page_image=_page_image(),
        source_path=Path("/tmp/whatever.pdf"),
        page_num=1,
        has_text_layer=False,
        doc_id="doc-4",
        session=fake_session,
        config=OCRConfig(paddleocr_confidence_threshold=0.7),
        llm_router=None,
    )

    assert result.source == "paddleocr"
    assert result.mean_confidence == pytest.approx(0.1)
    # No DB writes when no router is provided.
    fake_session.execute.assert_not_called()
