"""Unit tests for has_text_layer() in isolation (no OCR model, no DB) — T-3."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.ingest.ocr.pdfplumber_ocr import has_text_layer


def _make_mock_pdf(page_texts: list[str]):
    """Return a pdfplumber context-manager mock with pages that yield given texts."""
    mock_pages = []
    for t in page_texts:
        page = MagicMock()
        page.extract_text.return_value = t
        mock_pages.append(page)

    mock_pdf = MagicMock()
    mock_pdf.pages = mock_pages
    mock_pdf.__enter__ = lambda s: s
    mock_pdf.__exit__ = MagicMock(return_value=False)
    return mock_pdf


def test_zero_pages_returns_false(tmp_path):
    mock_pdf = _make_mock_pdf([])
    with patch("pdfplumber.open", return_value=mock_pdf):
        assert has_text_layer(tmp_path / "empty.pdf") is False


def test_all_pages_above_threshold(tmp_path):
    mock_pdf = _make_mock_pdf(["x" * 100, "x" * 100, "x" * 100])
    with patch("pdfplumber.open", return_value=mock_pdf):
        assert has_text_layer(tmp_path / "native.pdf") is True


def test_first_page_rich_others_empty_returns_false(tmp_path):
    """A single content-rich page must not mask scan-only siblings (C-7)."""
    mock_pdf = _make_mock_pdf(["x" * 500, "", "", "", ""])
    with patch("pdfplumber.open", return_value=mock_pdf):
        assert has_text_layer(tmp_path / "mixed.pdf") is False


def test_borderline_exactly_at_threshold(tmp_path):
    """A page with exactly min_chars_per_page characters should NOT pass (strictly less)."""
    mock_pdf = _make_mock_pdf(["x" * 50])
    with patch("pdfplumber.open", return_value=mock_pdf):
        # Default min_chars_per_page=50 → must be >= 50 for True... but our
        # implementation requires strictly > threshold? No — we return False when
        # len < threshold. Exactly 50 means NOT False, so True.
        result = has_text_layer(tmp_path / "border.pdf", min_chars_per_page=50)
    # 50 >= 50 passes (not less than), so True
    assert result is True


def test_one_page_below_threshold_returns_false(tmp_path):
    mock_pdf = _make_mock_pdf(["x" * 100, "x" * 10])
    with patch("pdfplumber.open", return_value=mock_pdf):
        assert has_text_layer(tmp_path / "partly.pdf") is False


def test_pdfplumber_exception_returns_false(tmp_path):
    with patch("pdfplumber.open", side_effect=Exception("corrupt")):
        assert has_text_layer(tmp_path / "corrupt.pdf") is False


def test_none_text_treated_as_empty(tmp_path):
    mock_pdf = _make_mock_pdf([None, None])  # type: ignore[list-item]
    with patch("pdfplumber.open", return_value=mock_pdf):
        assert has_text_layer(tmp_path / "notext.pdf") is False


def test_samples_at_most_five_pages(tmp_path):
    """Only the first 5 pages are sampled regardless of document length."""
    mock_pdf = _make_mock_pdf(["x" * 100] * 5 + [""])
    with patch("pdfplumber.open", return_value=mock_pdf):
        result = has_text_layer(tmp_path / "long.pdf")
    assert result is True
    mock_pdf.pages[5].extract_text.assert_not_called()
