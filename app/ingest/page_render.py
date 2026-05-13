"""Render a single PDF page to PNG. Lazily imports pdf2image (which needs poppler);
if poppler isn't installed, surfaces a typed 503 error rather than blowing up
at import time."""

from __future__ import annotations

from pathlib import Path

from app.core.errors import AppError


class PageRenderUnavailableError(AppError):
    def __init__(self, message: str = "Page rendering is not available") -> None:
        super().__init__(message, code="PAGE_RENDER_UNAVAILABLE", status_code=503)


def render_page_png(
    pdf_path: Path,
    page_n: int,
    dest_path: Path,
    *,
    dpi: int = 144,
) -> Path:
    """Render `page_n` (1-indexed) of pdf_path to dest_path. Returns dest_path."""
    try:
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise PageRenderUnavailableError(
            "pdf2image is not installed (M3 deferred dependency)"
        ) from exc

    try:
        images = convert_from_path(
            str(pdf_path),
            dpi=dpi,
            first_page=page_n,
            last_page=page_n,
        )
    except Exception as exc:
        raise PageRenderUnavailableError(
            f"poppler render failed: {exc}"
        ) from exc

    if not images:
        raise PageRenderUnavailableError(f"page {page_n} not found in PDF")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(dest_path, format="PNG")
    return dest_path
