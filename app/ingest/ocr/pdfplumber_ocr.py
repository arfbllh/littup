"""Native-PDF OCR provider via pdfplumber.

Reads the embedded text layer + word-level bounding boxes.
No rasterisation needed; mean_confidence is always 1.0.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from app.core.errors import IngestError
from app.ingest.ocr.base import OCRSpan, PageExtraction

logger = structlog.get_logger(__name__)


class PdfplumberProvider:
    async def extract_page(
        self,
        source_path: Path,
        page_num: int,
        *,
        page_image=None,  # unused; here for protocol compat
    ) -> PageExtraction:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._extract_sync, source_path, page_num)

    def _extract_sync(self, path: Path, page_num: int) -> PageExtraction:
        import pdfplumber

        try:
            with pdfplumber.open(str(path)) as pdf:
                if page_num < 1 or page_num > len(pdf.pages):
                    raise IngestError(
                        f"Page {page_num} out of range (document has {len(pdf.pages)} pages)",
                        code="PAGE_OUT_OF_RANGE",
                    )
                page = pdf.pages[page_num - 1]  # pdfplumber is 0-indexed
                width = float(page.width or 1)
                height = float(page.height or 1)

                words = page.extract_words(x_tolerance=3, y_tolerance=3) or []
                spans: list[OCRSpan] = []
                for w in words:
                    text = (w.get("text") or "").strip()
                    if not text:
                        continue
                    spans.append(
                        OCRSpan(
                            text=text,
                            page=page_num,
                            bbox=(
                                float(w["x0"]) / width,
                                float(w["top"]) / height,
                                float(w["x1"]) / width,
                                float(w["bottom"]) / height,
                            ),
                            confidence=1.0,
                            source="pdfplumber",
                        )
                    )

        except IngestError:
            raise
        except Exception as exc:
            raise IngestError(
                f"pdfplumber failed on page {page_num}: {exc}",
                code="PDFPLUMBER_ERROR",
            ) from exc

        full_text = " ".join(s.text for s in spans)
        logger.debug(
            "pdfplumber_page_done",
            path=str(path),
            page=page_num,
            span_count=len(spans),
        )
        return PageExtraction(
            page_num=page_num,
            spans=spans,
            full_text=full_text,
            mean_confidence=1.0,
            source="pdfplumber",
        )


def has_text_layer(pdf_path: Path, min_chars_per_page: int = 50) -> bool:
    """Return True if the PDF has an embedded text layer with meaningful content.

    Uses pdfplumber (same parser as extraction) so the detection threshold is
    calibrated against the same character counts as the actual extract (C-3).
    Requires ALL sampled pages to have enough characters — a single page with
    lots of text doesn't make a mostly-scanned document 'native' (C-7).
    """
    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            pages_to_sample = min(len(pdf.pages), 5)
            if pages_to_sample == 0:
                return False
            for i in range(pages_to_sample):
                page_text = pdf.pages[i].extract_text() or ""
                if len(page_text) < min_chars_per_page:
                    return False
        return True
    except Exception:
        return False
