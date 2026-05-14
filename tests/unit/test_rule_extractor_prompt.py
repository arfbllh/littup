"""Unit tests for RuleExtractor prompt building and case rendering."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from app.edits.rule_extractor import _build_analysis_prompt, _render_case


def _make_row(ai_value, user_value, field_type="field", field_or_section_name="parties"):
    return SimpleNamespace(
        ai_value=ai_value,
        user_value=user_value,
        field_type=field_type,
        field_or_section_name=field_or_section_name,
    )


def test_prompt_contains_numbered_cases():
    cases = [
        _make_row({"value": "Smith"}, {"value": "Smith, LLC"}),
        _make_row({"value": "Jones"}, {"value": "Jones Corp"}),
        _make_row({"value": "Brown"}, {"value": "Brown Inc"}),
    ]
    messages = _build_analysis_prompt("parties", "field", cases, min_evidence=2)
    user_content = messages[1]["content"]
    assert "1." in user_content
    assert "2." in user_content
    assert "3." in user_content


def test_long_ai_value_truncated():
    long_str = "x" * 1000
    row = _make_row({"value": long_str}, {"value": "short"})
    ai_repr, user_repr = _render_case(row, max_chars=400)
    assert len(ai_repr) <= 400
    assert user_repr == "short"


def test_min_evidence_formula():
    for n, expected in [(3, 2), (5, 3), (12, 8)]:
        assert max(2, math.ceil(n * 0.6)) == expected


def test_render_case_section_uses_text_key():
    row = _make_row(
        {"text": "AI draft text"},
        {"text": "User revision"},
        field_type="section",
    )
    ai, user = _render_case(row)
    assert ai == "AI draft text"
    assert user == "User revision"


def test_render_case_field_uses_value_key():
    row = _make_row({"value": "Smith"}, {"value": "Smith, LLC"})
    ai, user = _render_case(row)
    assert ai == "Smith"
    assert user == "Smith, LLC"


def test_prompt_has_system_message():
    cases = [_make_row({"value": "a"}, {"value": "b"})] * 3
    messages = _build_analysis_prompt("field1", "string", cases, 2)
    assert messages[0]["role"] == "system"
    assert "JSON" in messages[0]["content"]
