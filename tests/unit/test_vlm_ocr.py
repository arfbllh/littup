"""Unit tests for VLM OCR response parsing (no model, no DB) — T-1."""

from __future__ import annotations

import pytest

from app.ingest.ocr.vlm_ocr import _parse_vlm_response


def test_normal_response():
    structured = {
        "lines": [
            {"text": "Hello", "bbox": [0.1, 0.1, 0.3, 0.2]},
            {"text": "World", "bbox": [0.4, 0.1, 0.6, 0.2]},
        ]
    }
    spans = _parse_vlm_response(structured, page_num=1)
    assert len(spans) == 2
    assert spans[0].text == "Hello"
    assert spans[1].text == "World"


def test_missing_words_key():
    spans = _parse_vlm_response({}, page_num=1)
    assert spans == []


def test_null_structured():
    spans = _parse_vlm_response({"lines": None}, page_num=1)
    assert spans == []


def test_words_is_empty_list():
    spans = _parse_vlm_response({"lines": []}, page_num=1)
    assert spans == []


def test_inverted_bbox_is_dropped():
    """A bbox where x0 >= x1 or y0 >= y1 must be silently discarded (C-4)."""
    structured = {
        "lines": [
            {"text": "Bad", "bbox": [0.5, 0.1, 0.1, 0.2]},   # x0 > x1
            {"text": "AlsoBad", "bbox": [0.1, 0.5, 0.3, 0.2]},  # y0 > y1
            {"text": "Good", "bbox": [0.1, 0.1, 0.3, 0.2]},
        ]
    }
    spans = _parse_vlm_response(structured, page_num=1)
    assert len(spans) == 1
    assert spans[0].text == "Good"


def test_out_of_range_coords_are_clamped():
    structured = {
        "lines": [
            {"text": "Clamped", "bbox": [-0.5, -0.1, 1.5, 1.2]},
        ]
    }
    spans = _parse_vlm_response(structured, page_num=1)
    assert len(spans) == 1
    x0, y0, x1, y1 = spans[0].bbox
    assert x0 == 0.0 and y0 == 0.0
    assert x1 == 1.0 and y1 == 1.0


def test_missing_bbox_field():
    structured = {"lines": [{"text": "NoBbox"}]}
    spans = _parse_vlm_response(structured, page_num=1)
    assert spans == []


def test_too_short_bbox():
    structured = {"lines": [{"text": "Short", "bbox": [0.1, 0.1]}]}
    spans = _parse_vlm_response(structured, page_num=1)
    assert spans == []


def test_non_numeric_bbox():
    structured = {"lines": [{"text": "Bad", "bbox": ["a", "b", "c", "d"]}]}
    spans = _parse_vlm_response(structured, page_num=1)
    assert spans == []


def test_empty_text_word_skipped():
    structured = {"lines": [{"text": "   ", "bbox": [0.1, 0.1, 0.3, 0.2]}]}
    spans = _parse_vlm_response(structured, page_num=1)
    assert spans == []


def test_json_within_json_words():
    """Some models return a JSON string instead of a list for 'lines'."""
    import json

    inner = [{"text": "Nested", "bbox": [0.1, 0.1, 0.3, 0.2]}]
    structured = {"lines": json.dumps(inner)}
    spans = _parse_vlm_response(structured, page_num=1)
    assert len(spans) == 1
    assert spans[0].text == "Nested"


def test_equal_bbox_coords_dropped():
    """x0 == x1 or y0 == y1 produces a zero-area box and must be dropped."""
    structured = {
        "lines": [
            {"text": "ZeroWidth", "bbox": [0.2, 0.1, 0.2, 0.3]},
            {"text": "ZeroHeight", "bbox": [0.1, 0.2, 0.3, 0.2]},
        ]
    }
    spans = _parse_vlm_response(structured, page_num=1)
    assert spans == []
