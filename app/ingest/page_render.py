"""Render a single PDF page to PNG. Lazily imports pdf2image (which needs poppler);
if poppler isn't installed, surfaces a typed 503 error rather than blowing up
at import time."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
            "pdf2image is not installed"
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


def render_text_to_png(
    src_path: Path,
    dest_path: Path,
    *,
    max_chars: int = 20_000,
    width: int = 850,
    margin: int = 40,
    line_height: int = 22,
    font_size: int = 15,
) -> Path:
    """Render the UTF-8 text in ``src_path`` to a PNG at ``dest_path``.

    Used for the document preview when the source is a .txt or .md upload —
    no real page image exists, so we rasterise the text into one tall PNG
    that the existing PageWithBboxes component can show.
    """
    from PIL import Image, ImageDraw, ImageFont

    try:
        text = src_path.read_text("utf-8")
    except UnicodeDecodeError as exc:
        raise PageRenderUnavailableError(
            f"text file {src_path} is not valid UTF-8: {exc}"
        ) from exc

    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n… (preview truncated)"

    font: Any
    try:
        font = ImageFont.truetype("DejaVuSansMono.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    avail = width - 2 * margin
    avg_char_w = max(int(font_size * 0.6), 6)
    wrap_cols = max(avail // avg_char_w, 40)

    import textwrap

    wrapped: list[str] = []
    for raw_line in text.splitlines() or [""]:
        if not raw_line.strip():
            wrapped.append("")
            continue
        wrapped.extend(
            textwrap.wrap(raw_line, width=wrap_cols, replace_whitespace=False) or [""]
        )

    height = max(2 * margin + len(wrapped) * line_height, 200)
    img = Image.new("RGB", (width, height), color=(252, 250, 244))
    draw = ImageDraw.Draw(img)

    y = margin
    for line in wrapped:
        draw.text((margin, y), line, fill=(30, 30, 30), font=font)
        y += line_height

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest_path, format="PNG")
    return dest_path
