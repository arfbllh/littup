"""MIME detection via magic-byte sniffing for the supported document types.

We avoid the `python-magic` system dependency (libmagic). The
allowlist is small and the magic numbers are stable; if more
exotic types are needed we can swap to `python-magic`.

Plain-text formats have no magic bytes, so detection falls back to the
caller's filename hint plus a UTF-8 / no-NUL-byte sniff.
"""

from __future__ import annotations

from pathlib import Path

from app.core.errors import AppError

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
TIFF = "image/tiff"
WEBP = "image/webp"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT = "text/plain"
MARKDOWN = "text/markdown"
JSON_MIME = "application/json"

ALLOWED: dict[str, str] = {
    PDF: ".pdf",
    PNG: ".png",
    JPEG: ".jpg",
    TIFF: ".tiff",
    WEBP: ".webp",
    DOCX: ".docx",
    TXT: ".txt",
    MARKDOWN: ".md",
    JSON_MIME: ".json",
}

# Filename-extension hints for formats without reliable magic bytes
# (plain text) or whose magic is shared with other formats (DOCX = ZIP).
_EXT_HINTS: dict[str, str] = {
    ".txt": TXT,
    ".md": MARKDOWN,
    ".markdown": MARKDOWN,
    ".json": JSON_MIME,
    ".docx": DOCX,
}


class UnsupportedMimeError(AppError):
    def __init__(self, message: str = "Unsupported media type") -> None:
        super().__init__(message, code="UNSUPPORTED_MIME", status_code=415)


def detect_mime(path: Path, *, filename_hint: str | None = None) -> str:
    """Sniff the first 16 bytes to identify supported types.

    Falls back to ``filename_hint`` extension for formats without reliable
    magic bytes (plain text, Markdown). Raises ``UnsupportedMimeError`` for
    anything outside ALLOWED.
    """
    with path.open("rb") as f:
        head = f.read(16)

    if head.startswith(b"%PDF-"):
        return PDF
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head[:3] == b"\xff\xd8\xff":
        return JPEG
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return TIFF
    # WEBP: "RIFF" then 4 size bytes then "WEBP"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP

    # Extension-hinted formats: DOCX (which is a ZIP, magic alone is ambiguous)
    # and plain-text formats which have no magic at all.
    if filename_hint:
        ext = Path(filename_hint).suffix.lower()
        hinted = _EXT_HINTS.get(ext)
        if hinted is not None:
            # Sanity-check text hints: file must decode as UTF-8 and have no NUL bytes,
            # so a renamed binary doesn't slip through.
            if hinted in (TXT, MARKDOWN, JSON_MIME) and not _looks_like_text(path):
                raise UnsupportedMimeError(
                    f"File hinted as {hinted!r} but content is not valid UTF-8 text"
                )
            return hinted

    raise UnsupportedMimeError(f"Unrecognized file content (magic={head[:8]!r})")


def ext_for(mime: str) -> str:
    try:
        return ALLOWED[mime]
    except KeyError as exc:
        raise UnsupportedMimeError(f"No extension registered for {mime!r}") from exc


def _looks_like_text(path: Path, *, sample_bytes: int = 4096) -> bool:
    """Return True if the file is plausibly UTF-8 text with no NUL bytes."""
    with path.open("rb") as f:
        sample = f.read(sample_bytes)
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
