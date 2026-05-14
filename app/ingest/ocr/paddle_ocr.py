"""PaddleOCR provider.

Lazy-loads the model on first call (CPU by default; GPU if OCR_USE_GPU=1).
Call release_paddle() after a document's pages are fully processed to free
memory between documents.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from app.core.errors import IngestError
from app.ingest.ocr.base import OCRSpan, PageExtraction

logger = structlog.get_logger(__name__)

_paddle_lock = asyncio.Lock()
_paddle_instance: Any | None = None


async def _get_paddle() -> Any:
    global _paddle_instance
    async with _paddle_lock:
        if _paddle_instance is None:
            use_gpu = os.environ.get("OCR_USE_GPU", "0") == "1"
            loop = asyncio.get_running_loop()
            _paddle_instance = await loop.run_in_executor(None, _init_paddle, use_gpu)
    return _paddle_instance


def _init_paddle(use_gpu: bool) -> Any:
    from paddleocr import PaddleOCR  # type: ignore[import]

    logger.info("paddleocr_model_loading", use_gpu=use_gpu)
    instance = PaddleOCR(
        use_angle_cls=True,
        lang="en",
        use_gpu=use_gpu,
        show_log=False,
    )
    logger.info("paddleocr_model_ready")
    return instance


def release_paddle() -> None:
    """Unload the PaddleOCR singleton to free memory after a document is processed."""
    global _paddle_instance
    _paddle_instance = None


def _paddle_result_to_spans(result: list, page_num: int, img_h: int, img_w: int) -> list[OCRSpan]:
    """Convert PaddleOCR raw output to normalised OCRSpan list."""
    spans: list[OCRSpan] = []
    if not result:
        return spans

    # result may be [[lines]] or [lines] depending on version
    inner = result[0]
    if isinstance(inner, list) and inner and isinstance(inner[0], list):
        lines = inner
    else:
        lines = result

    for item in lines:
        if item is None:
            continue
        try:
            box_pts, (text, conf) = item
        except (TypeError, ValueError):
            logger.warning("paddle_result_unpack_failed", item=repr(item)[:200])
            continue
        text = (text or "").strip()
        if not text:
            continue
        xs = [pt[0] for pt in box_pts]
        ys = [pt[1] for pt in box_pts]
        x0 = max(0.0, min(xs)) / max(img_w, 1)
        y0 = max(0.0, min(ys)) / max(img_h, 1)
        x1 = min(img_w, max(xs)) / max(img_w, 1)
        y1 = min(img_h, max(ys)) / max(img_h, 1)
        spans.append(
            OCRSpan(
                text=text,
                page=page_num,
                bbox=(x0, y0, x1, y1),
                confidence=float(conf),
                source="paddleocr",
            )
        )
    return spans


class PaddleProvider:
    async def extract_page(
        self,
        source_path: Path,  # noqa: ARG002 — required by OCRProvider protocol
        page_num: int,
        *,
        page_image: np.ndarray | None = None,
    ) -> PageExtraction:
        if page_image is None:
            raise IngestError(
                "PaddleOCR requires a rasterised page_image (np.ndarray)",
                code="PADDLE_NO_IMAGE",
            )

        paddle = await _get_paddle()
        img_h, img_w = page_image.shape[:2]

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, paddle.ocr, page_image, True)
        except Exception as exc:
            raise IngestError(
                f"PaddleOCR failed on page {page_num}: {exc}",
                code="PADDLE_OCR_ERROR",
            ) from exc

        spans = _paddle_result_to_spans(result, page_num, img_h, img_w)
        mean_conf = (
            float(sum(s.confidence for s in spans) / len(spans)) if spans else 0.0
        )
        full_text = " ".join(s.text for s in spans)

        logger.debug(
            "paddle_page_done",
            page=page_num,
            span_count=len(spans),
            mean_confidence=round(mean_conf, 3),
        )
        return PageExtraction(
            page_num=page_num,
            spans=spans,
            full_text=full_text,
            mean_confidence=mean_conf,
            source="paddleocr",
        )
