"""MIME detection via magic-byte sniffing for the supported document types.

We avoid the `python-magic` system dependency (libmagic) for M3. The
allowlist is small and the magic numbers are stable; if M4 needs more
exotic types we can swap to `python-magic`.
"""

from __future__ import annotations

from pathlib import Path

from app.core.errors import AppError

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
TIFF = "image/tiff"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

ALLOWED: dict[str, str] = {
    PDF: ".pdf",
    PNG: ".png",
    JPEG: ".jpg",
    TIFF: ".tiff",
    DOCX: ".docx",
}


class UnsupportedMimeError(AppError):
    def __init__(self, message: str = "Unsupported media type") -> None:
        super().__init__(message, code="UNSUPPORTED_MIME", status_code=415)


def detect_mime(path: Path) -> str:
    """Sniff the first 16 bytes to identify supported types.

    Raises UnsupportedMimeError for anything outside ALLOWED.
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
    # DOCX is a ZIP with a specific marker; rely on the .docx caller's extension hint
    # to avoid misclassifying generic .zip files as DOCX. Magic bytes alone for ZIP
    # are PK\x03\x04 — for M3 we only treat it as DOCX if the caller intends DOCX,
    # which here means filename-based hinting at the route level. For now, reject
    # generic ZIPs and let M4 add proper OOXML inspection if/when we need DOCX.
    raise UnsupportedMimeError(f"Unrecognized file content (magic={head[:8]!r})")


def ext_for(mime: str) -> str:
    try:
        return ALLOWED[mime]
    except KeyError as exc:
        raise UnsupportedMimeError(f"No extension registered for {mime!r}") from exc
