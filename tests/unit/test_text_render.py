"""Unit tests for the .txt/.md → PNG preview renderer."""

from __future__ import annotations

import pytest

from app.ingest.page_render import PageRenderUnavailableError, render_text_to_png


def test_renders_plain_text_to_png(tmp_path):
    src = tmp_path / "letter.txt"
    src.write_text("Hello, world.\nSecond line.\n", encoding="utf-8")
    dest = tmp_path / "out.png"

    render_text_to_png(src, dest)

    assert dest.exists()
    # PNG magic bytes
    assert dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert dest.stat().st_size > 200


def test_renders_markdown_to_png(tmp_path):
    src = tmp_path / "notes.md"
    src.write_text(
        "# Heading One\n\nBody paragraph one.\n\nBody paragraph two with **bold**.\n",
        encoding="utf-8",
    )
    dest = tmp_path / "out.png"

    render_text_to_png(src, dest)
    assert dest.exists()
    assert dest.stat().st_size > 200


def test_long_text_truncates_without_overflow(tmp_path):
    src = tmp_path / "long.txt"
    src.write_text("x" * 50_000, encoding="utf-8")
    dest = tmp_path / "out.png"

    render_text_to_png(src, dest, max_chars=2_000)
    assert dest.exists()


def test_invalid_utf8_raises_typed_error(tmp_path):
    src = tmp_path / "bad.txt"
    src.write_bytes(b"\xff\xfe broken")
    dest = tmp_path / "out.png"

    with pytest.raises(PageRenderUnavailableError):
        render_text_to_png(src, dest)


def test_empty_file_still_produces_minimum_canvas(tmp_path):
    src = tmp_path / "empty.txt"
    src.write_text("", encoding="utf-8")
    dest = tmp_path / "out.png"

    render_text_to_png(src, dest)
    assert dest.exists()
    assert dest.stat().st_size > 100
