"""VLM OCR provider — escalation path for handwriting / very low confidence pages.

Sends a base64-encoded page image to the vision model via LLMRouter and
parses the structured JSON output into OCRSpan objects.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog

from app.core.errors import IngestError
from app.ingest.ocr.base import OCRSpan, PageExtraction

if TYPE_CHECKING:
    from app.llm.router import LLMRouter

logger = structlog.get_logger(__name__)

# JSON schema sent to the model for structured output
_VLM_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "bbox": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                        "description": "Normalised [x0, y0, x1, y1] coordinates 0–1",
                    },
                },
                "required": ["text", "bbox"],
            },
        }
    },
    "required": ["lines"],
}

_SYSTEM_PROMPT = (
    "You are an OCR assistant. Extract all text from the document image "
    "and return it as a JSON array under the key 'lines'. "
    "Each entry represents one line of text and must include 'text' (the full line string) "
    "and 'bbox' ([x0, y0, x1, y1] as fractions of image width/height, 0–1 range). "
    "Group words into natural reading lines. Preserve reading order. Do not add commentary."
)

_DESCRIBE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
    },
    "required": ["description"],
}

_DESCRIBE_SYSTEM_PROMPT = (
    "You are a legal document analyst. Describe this image in comprehensive detail "
    "for use in a legal case management system. Include the type of scene or document, "
    "all visible objects, any damage or injuries, people or vehicles present, "
    "environmental or road conditions, any visible text or signage, dates or reference "
    "numbers if present, and any other legally relevant details. "
    "Be thorough and precise — this description is the only searchable content for this image."
)


class VlmProvider:
    def __init__(self, router: "LLMRouter") -> None:
        self._router = router

    async def extract_page(
        self,
        source_path: Path,
        page_num: int,
        *,
        page_image: np.ndarray | None = None,
    ) -> PageExtraction:
        if page_image is None:
            raise IngestError(
                "VlmProvider requires a page_image (np.ndarray)",
                code="VLM_NO_IMAGE",
            )

        import asyncio
        loop = asyncio.get_running_loop()
        png_bytes = await loop.run_in_executor(None, _encode_png, page_image)
        b64_data = base64.b64encode(png_bytes).decode("ascii")

        from app.llm.types import ImagePart, Message, SamplingParams, TextPart

        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=[
                    ImagePart(
                        type="image",
                        source={
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64_data,
                        },
                    ),
                    TextPart(type="text", text="Extract all text from this document page."),
                ],
            ),
        ]

        try:
            response = await self._router.generate(
                messages,
                task="vision",
                schema=_VLM_OUTPUT_SCHEMA,
                sampling=SamplingParams(max_tokens=4096, temperature=0.0),
                cache=False,  # page images are effectively unique
            )
        except Exception as exc:
            raise IngestError(
                f"VLM OCR failed on page {page_num}: {exc}",
                code="VLM_OCR_ERROR",
            ) from exc

        spans = _parse_vlm_response(response.structured or {}, page_num)
        mean_conf = 0.85  # VLM doesn't return per-word confidence; use a fixed proxy
        full_text = " ".join(s.text for s in spans)

        logger.debug(
            "vlm_page_done",
            page=page_num,
            span_count=len(spans),
        )
        return PageExtraction(
            page_num=page_num,
            spans=spans,
            full_text=full_text,
            mean_confidence=mean_conf,
            source="vlm",
        )

    async def describe_page(
        self,
        source_path: Path,
        page_num: int,
        *,
        page_image: np.ndarray | None = None,
    ) -> PageExtraction:
        """Describe a non-text image (photo, diagram) for retrieval.

        Called when OCR yields very little text — produces a single full-page
        span containing a detailed natural-language description of the image.
        """
        if page_image is None:
            raise IngestError(
                "VlmProvider.describe_page requires a page_image (np.ndarray)",
                code="VLM_NO_IMAGE",
            )

        import asyncio
        loop = asyncio.get_running_loop()
        png_bytes = await loop.run_in_executor(None, _encode_png, page_image)
        b64_data = base64.b64encode(png_bytes).decode("ascii")

        from app.llm.types import ImagePart, Message, SamplingParams, TextPart

        messages = [
            Message(role="system", content=_DESCRIBE_SYSTEM_PROMPT),
            Message(
                role="user",
                content=[
                    ImagePart(
                        type="image",
                        source={
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64_data,
                        },
                    ),
                    TextPart(type="text", text="Describe this image in detail."),
                ],
            ),
        ]

        try:
            response = await self._router.generate(
                messages,
                task="vision",
                schema=_DESCRIBE_OUTPUT_SCHEMA,
                sampling=SamplingParams(max_tokens=1024, temperature=0.0),
                cache=False,
            )
        except Exception as exc:
            raise IngestError(
                f"VLM description failed on page {page_num}: {exc}",
                code="VLM_DESCRIBE_ERROR",
            ) from exc

        description = (response.structured or {}).get("description", "").strip()
        spans = []
        if description:
            spans.append(
                OCRSpan(
                    text=description,
                    page=page_num,
                    bbox=(0.0, 0.0, 1.0, 1.0),
                    confidence=0.90,
                    source="vlm_description",
                )
            )

        logger.debug(
            "vlm_describe_done",
            page=page_num,
            char_count=len(description),
        )
        return PageExtraction(
            page_num=page_num,
            spans=spans,
            full_text=description,
            mean_confidence=0.90 if description else 0.0,
            source="vlm_description",
        )


def _encode_png(image: np.ndarray) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise IngestError("Failed to encode page image as PNG", code="IMAGE_ENCODE_ERROR")
    return bytes(buf)


def _parse_vlm_response(structured: dict[str, Any], page_num: int) -> list[OCRSpan]:
    words = structured.get("lines") or []
    if isinstance(words, str):
        # Some models return JSON-within-JSON; attempt to parse it
        try:
            words = json.loads(words)
        except json.JSONDecodeError:
            return []

    spans: list[OCRSpan] = []
    for w in words:
        text = (w.get("text") or "").strip()
        bbox_raw = w.get("bbox") or []
        if not text or len(bbox_raw) < 4:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in bbox_raw[:4])
        except (TypeError, ValueError):
            continue
        # Clamp to valid range, then reject degenerate boxes (C-4)
        x0, y0 = max(0.0, x0), max(0.0, y0)
        x1, y1 = min(1.0, x1), min(1.0, y1)
        if x0 >= x1 or y0 >= y1:
            continue
        spans.append(
            OCRSpan(
                text=text,
                page=page_num,
                bbox=(x0, y0, x1, y1),
                confidence=0.85,
                source="vlm",
            )
        )
    return spans
