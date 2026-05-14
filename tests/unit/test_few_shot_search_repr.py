"""Unit tests for FewShotStore._search_repr."""
from __future__ import annotations

import pytest

from app.edits.few_shot_store import _search_repr, _BLOCK_MAX, _LINE_MAX


def test_truncation_block():
    """Output must not exceed _BLOCK_MAX chars even with large inputs."""
    big = "x" * 5000
    result = _search_repr("parties", "field", big, big, ["t", "f"], chunk_context=big)
    assert len(result) <= _BLOCK_MAX


def test_truncation_per_line():
    """Each individual line in the output must not exceed _LINE_MAX chars."""
    big = "y" * 3000
    result = _search_repr("parties", "field", big, big, ["t"])
    for line in result.splitlines():
        assert len(line) <= _LINE_MAX, f"Line too long: {len(line)}"


def test_party_render():
    """list[party] values are rendered as 'name (role)' and separated by '; '."""
    parties = [
        {"name": "Smith", "role": "plaintiff"},
        {"name": "Jones Corp", "role": "defendant"},
    ]
    result = _search_repr("parties", "field", parties, parties, ["case_fact_summary", "field"])
    assert "Smith (plaintiff)" in result
    assert "Jones Corp (defendant)" in result


def test_party_render_no_role():
    parties = [{"name": "Acme"}]
    result = _search_repr("parties", "field", parties, [], ["t", "f"])
    assert "Acme" in result


def test_pure_function():
    """Same arguments always produce the same output."""
    args = ("parties", "field", "val", "corr", ["tid", "field"])
    assert _search_repr(*args) == _search_repr(*args)


def test_pure_function_with_chunk_context():
    args = ("background", "section", "draft text", "edit text", ["tid", "section"])
    kw = {"chunk_context": "chunk one\n\nchunk two"}
    assert _search_repr(*args, **kw) == _search_repr(*args, **kw)


def test_chunk_context_appended_when_present():
    result = _search_repr("f", "field", "a", "b", ["t"], chunk_context="some context")
    assert "CONTEXT:" in result
    assert "some context" in result


def test_chunk_context_absent_when_none():
    result = _search_repr("f", "field", "a", "b", ["t"], chunk_context=None)
    assert "CONTEXT:" not in result


def test_section_text_truncated_at_800():
    """Section text values are truncated to 800 chars before embedding."""
    long_text = "w" * 1200
    result = _search_repr("background", "section", long_text, long_text, ["t"])
    # Each AI/USER line contains the rendered value; check it's truncated
    for line in result.splitlines():
        if line.startswith("AI:") or line.startswith("USER:"):
            assert len(line) <= _LINE_MAX


def test_tags_present():
    result = _search_repr("parties", "field", None, None, ["case_fact_summary", "field"])
    assert "TAGS:" in result
    assert "case_fact_summary" in result
    assert "field" in result


def test_label_format():
    result = _search_repr("case_caption", "field", None, None, ["t"])
    assert "[FIELD case_caption | field]" in result


def test_section_label_format():
    result = _search_repr("background", "section", None, None, ["t"])
    assert "[FIELD background | section]" in result
