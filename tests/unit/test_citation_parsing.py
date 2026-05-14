"""Tests for citation parsing and dangling-strip helpers."""
from __future__ import annotations

import pytest

from app.draft.citations import CitationSpan, parse_citations, strip_dangling

VALID_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
VALID_UUID2 = "11111111-2222-3333-4444-555555555555"


def test_parse_citations_extracts_valid_uuids():
    text = f"Some claim [chunk:{VALID_UUID}] and another fact."
    spans = parse_citations(text)
    assert len(spans) == 1
    assert spans[0].chunk_id == VALID_UUID


def test_parse_citations_extracts_multiple():
    text = f"Fact A [chunk:{VALID_UUID}]. Fact B [chunk:{VALID_UUID2}]."
    spans = parse_citations(text)
    assert len(spans) == 2
    assert {s.chunk_id for s in spans} == {VALID_UUID, VALID_UUID2}


def test_parse_citations_ignores_malformed():
    text = "Bad ref [chunk:foo] and [chunk:not-a-uuid]."
    spans = parse_citations(text)
    assert spans == []


def test_parse_citations_span_positions():
    text = f"ABC [chunk:{VALID_UUID}] XYZ"
    spans = parse_citations(text)
    assert len(spans) == 1
    assert text[spans[0].start:spans[0].end] == f"[chunk:{VALID_UUID}]"


def test_strip_dangling_removes_unknown():
    text = f"Fact [chunk:{VALID_UUID}] and fabricated [chunk:{VALID_UUID2}]."
    cleaned, dangling = strip_dangling(text, {VALID_UUID})
    assert VALID_UUID2 in dangling
    assert f"[chunk:{VALID_UUID2}]" not in cleaned
    assert f"[chunk:{VALID_UUID}]" in cleaned


def test_strip_dangling_no_dangling():
    text = f"Fact [chunk:{VALID_UUID}]."
    cleaned, dangling = strip_dangling(text, {VALID_UUID})
    assert dangling == []
    assert cleaned == text


def test_strip_dangling_all_dangling():
    text = f"Made up [chunk:{VALID_UUID}]."
    cleaned, dangling = strip_dangling(text, set())
    assert VALID_UUID in dangling
    assert f"[chunk:{VALID_UUID}]" not in cleaned
