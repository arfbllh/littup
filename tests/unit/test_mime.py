"""Unit tests for MIME detection (no DB, no fixtures)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.mime import (
    DOCX,
    JPEG,
    JSON_MIME,
    MARKDOWN,
    PDF,
    PNG,
    TIFF,
    TXT,
    WEBP,
    UnsupportedMimeError,
    detect_mime,
)


def _write(tmp_path: Path, name: str, content: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


# ── Magic-byte formats ───────────────────────────────────────────────────────

def test_pdf_detected_by_magic(tmp_path):
    p = _write(tmp_path, "x.pdf", b"%PDF-1.4\nthe rest does not matter")
    assert detect_mime(p) == PDF


def test_png_detected_by_magic(tmp_path):
    p = _write(tmp_path, "x.png", b"\x89PNG\r\n\x1a\nrest of png bytes here")
    assert detect_mime(p) == PNG


def test_jpeg_detected_by_magic(tmp_path):
    p = _write(tmp_path, "x.jpg", b"\xff\xd8\xff\xe0jfifish bytes")
    assert detect_mime(p) == JPEG


def test_tiff_detected_by_magic(tmp_path):
    p = _write(tmp_path, "x.tiff", b"II*\x00" + b"\x00" * 12)
    assert detect_mime(p) == TIFF


def test_webp_detected_by_magic(tmp_path):
    # WEBP magic = "RIFF" + 4 size bytes + "WEBP"
    p = _write(tmp_path, "x.webp", b"RIFF\x10\x00\x00\x00WEBPVP8 ")
    assert detect_mime(p) == WEBP


# ── Extension-hinted formats ────────────────────────────────────────────────

def test_txt_detected_via_filename_hint(tmp_path):
    p = _write(tmp_path, "letter.txt", b"Hello, world.\nLine two.\n")
    assert detect_mime(p, filename_hint="letter.txt") == TXT


def test_markdown_detected_via_filename_hint(tmp_path):
    p = _write(tmp_path, "notes.md", b"# Heading\n\nbody text\n")
    assert detect_mime(p, filename_hint="notes.md") == MARKDOWN


def test_markdown_dot_markdown_extension(tmp_path):
    p = _write(tmp_path, "x.markdown", b"# Heading\n")
    assert detect_mime(p, filename_hint="x.markdown") == MARKDOWN


def test_json_detected_via_filename_hint(tmp_path):
    p = _write(tmp_path, "x.json", b'{"a": 1, "b": [2, 3]}')
    assert detect_mime(p, filename_hint="x.json") == JSON_MIME


def test_json_hint_rejects_binary_content(tmp_path):
    p = _write(tmp_path, "fake.json", b"\x00\x01\x02\xff")
    with pytest.raises(UnsupportedMimeError):
        detect_mime(p, filename_hint="fake.json")


def test_docx_detected_via_filename_hint(tmp_path):
    """DOCX is a ZIP; magic bytes alone are ambiguous, so we trust the hint."""
    p = _write(tmp_path, "x.docx", b"PK\x03\x04" + b"\x00" * 12)
    assert detect_mime(p, filename_hint="x.docx") == DOCX


def test_text_hint_rejects_binary_content(tmp_path):
    """A binary file renamed .txt must NOT be accepted as text."""
    p = _write(tmp_path, "fake.txt", b"\x00\x01\x02\x03\xff\xfe\xfd")
    with pytest.raises(UnsupportedMimeError):
        detect_mime(p, filename_hint="fake.txt")


def test_text_hint_rejects_invalid_utf8(tmp_path):
    p = _write(tmp_path, "fake.txt", b"\xff\xfe broken utf-8")
    with pytest.raises(UnsupportedMimeError):
        detect_mime(p, filename_hint="fake.txt")


def test_unknown_extension_without_magic_rejected(tmp_path):
    p = _write(tmp_path, "x.bin", b"random non-magic bytes")
    with pytest.raises(UnsupportedMimeError):
        detect_mime(p, filename_hint="x.bin")


def test_no_hint_no_magic_rejected(tmp_path):
    """Plain text with no filename hint must fail rather than guess."""
    p = _write(tmp_path, "x", b"just plain text without a hint")
    with pytest.raises(UnsupportedMimeError):
        detect_mime(p)


def test_magic_beats_hint(tmp_path):
    """A real PDF with a misleading .txt name is still a PDF."""
    p = _write(tmp_path, "misleading.txt", b"%PDF-1.7\n")
    assert detect_mime(p, filename_hint="misleading.txt") == PDF


def test_txt_with_unicode_passes(tmp_path):
    p = _write(tmp_path, "x.txt", "résumé — naïve coöperate ✓".encode())
    assert detect_mime(p, filename_hint="x.txt") == TXT
