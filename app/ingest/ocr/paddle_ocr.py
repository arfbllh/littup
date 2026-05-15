"""PaddleOCR provider (PP-OCRv5, paddleocr 3.x API).

Lazy-loads the model on first call (CPU by default; GPU if OCR_USE_GPU=1
and paddlepaddle-gpu is installed). The instance is a process-lifetime
singleton: PaddleOCR's underlying inference engine holds C++ predictor
pools that are not fully reclaimed when the Python ref is dropped, so
recreating it per document leaks RSS. release_paddle() is kept for tests
and explicit teardown — do not call it on a normal hot path.
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

    # Mobile det is ~5× faster than server det on CPU with comparable accuracy
    # for normal scans. Override with OCR_DET_MODEL=PP-OCRv5_server_det if you
    # need maximum recall on small/dense text.
    det_model = os.environ.get("OCR_DET_MODEL", "PP-OCRv5_mobile_det")
    rec_model = os.environ.get("OCR_REC_MODEL", "en_PP-OCRv5_mobile_rec")

    logger.info(
        "paddleocr_model_loading",
        use_gpu=use_gpu,
        ocr_version="PP-OCRv5",
        det_model=det_model,
        rec_model=rec_model,
    )
    instance = PaddleOCR(
        ocr_version="PP-OCRv5",
        text_detection_model_name=det_model,
        text_recognition_model_name=rec_model,
        lang="en",
        device="gpu" if use_gpu else "cpu",
        use_textline_orientation=True,
    )
    logger.info("paddleocr_model_ready")
    return instance


def release_paddle() -> None:
    """Unload the PaddleOCR singleton to free memory after a document is processed."""
    global _paddle_instance
    _paddle_instance = None


_FIELD_LOOKUPS = ("rec_texts", "rec_scores", "rec_polys", "rec_boxes", "dt_polys")


def _result_to_dict(page_result: Any) -> dict:
    """Normalise a paddleocr 3.x OCRResult into a plain dict of arrays.

    3.x returns OCRResult objects exposing rec_texts / rec_scores /
    rec_polys (or rec_boxes). Different point releases place them either
    at the top level, under a 'res' key, or behind a .json property/method.
    """
    data: Any = None
    if isinstance(page_result, dict):
        data = page_result
    else:
        attr = getattr(page_result, "json", None)
        if callable(attr):
            try:
                attr = attr()
            except Exception:
                attr = None
        if isinstance(attr, dict):
            data = attr
    if not isinstance(data, dict):
        # Last resort: pull recognised fields straight off the object
        data = {}
        for k in _FIELD_LOOKUPS:
            v = getattr(page_result, k, None)
            if v is not None:
                data[k] = v
    if isinstance(data.get("res"), dict):
        data = data["res"]
    return data


def _field(data: dict, *keys: str) -> Any:
    """Return the first non-None value for any of ``keys``.

    Avoids ``a or b`` because values may be numpy arrays where bool() raises
    ``ValueError: The truth value of an array with more than one element...``.
    """
    for k in keys:
        v = data.get(k)
        if v is not None:
            return v
    return None


def _paddle_result_to_spans(result: list, page_num: int, img_h: int, img_w: int) -> list[OCRSpan]:
    """Convert paddleocr 3.x predict() output to normalised OCRSpan list."""
    spans: list[OCRSpan] = []
    if result is None or len(result) == 0:
        return spans

    for page_result in result:
        data = _result_to_dict(page_result)
        texts = _field(data, "rec_texts")
        scores = _field(data, "rec_scores")
        polys = _field(data, "rec_polys", "dt_polys")
        boxes = _field(data, "rec_boxes")

        if texts is None or len(texts) == 0:
            logger.debug(
                "paddle_result_no_texts",
                page=page_num,
                keys=list(data.keys()) if isinstance(data, dict) else None,
            )
            continue

        n_scores = len(scores) if scores is not None else 0
        n_polys = len(polys) if polys is not None else 0
        n_boxes = len(boxes) if boxes is not None else 0

        for idx in range(len(texts)):
            text = (texts[idx] or "").strip()
            if not text:
                continue
            conf = float(scores[idx]) if idx < n_scores else 0.0

            if idx < n_polys and polys[idx] is not None:
                pts = polys[idx]
                xs = [float(pt[0]) for pt in pts]
                ys = [float(pt[1]) for pt in pts]
                bx0, by0 = min(xs), min(ys)
                bx1, by1 = max(xs), max(ys)
            elif idx < n_boxes and boxes[idx] is not None:
                bx0, by0, bx1, by1 = (float(v) for v in boxes[idx][:4])
            else:
                logger.warning("paddle_result_missing_bbox", page=page_num, idx=idx)
                continue

            x0 = max(0.0, bx0) / max(img_w, 1)
            y0 = max(0.0, by0) / max(img_h, 1)
            x1 = min(float(img_w), bx1) / max(img_w, 1)
            y1 = min(float(img_h), by1) / max(img_h, 1)
            spans.append(
                OCRSpan(
                    text=text,
                    page=page_num,
                    bbox=(x0, y0, x1, y1),
                    confidence=conf,
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
            result = await loop.run_in_executor(None, paddle.predict, page_image)
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
