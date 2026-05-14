"""Unit tests for RuleExtractor._parse_rule."""
from __future__ import annotations

import json

import pytest

from app.edits.rule_extractor import _parse_rule


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


def _resp(obj) -> _FakeResponse:
    return _FakeResponse(json.dumps(obj))


def test_valid_rule_returned():
    resp = _resp({"rule": "Always include the party role in parentheses.", "evidence_count": 5, "rationale": "Consistent pattern."})
    result = _parse_rule(resp, min_evidence=3, trace_id=None)
    assert result == "Always include the party role in parentheses."


def test_no_rule_returns_none(caplog):
    import logging
    resp = _resp({"rule": "NO_RULE", "evidence_count": 0, "rationale": "No pattern."})
    with caplog.at_level(logging.INFO):
        result = _parse_rule(resp, min_evidence=3, trace_id=None)
    assert result is None
    assert any("rule_extractor.no_rule" in r.message for r in caplog.records)


def test_low_evidence_returns_none(caplog):
    import logging
    resp = _resp({"rule": "Do something.", "evidence_count": 1, "rationale": "rare"})
    with caplog.at_level(logging.WARNING):
        result = _parse_rule(resp, min_evidence=3, trace_id=None)
    assert result is None
    assert any("rule_extractor.evidence_insufficient" in r.message for r in caplog.records)


def test_empty_rule_string_returns_none(caplog):
    import logging
    resp = _resp({"rule": "  ", "evidence_count": 5, "rationale": "blank"})
    with caplog.at_level(logging.WARNING):
        result = _parse_rule(resp, min_evidence=3, trace_id=None)
    assert result is None
    assert any("rule_extractor.parse_failed" in r.message for r in caplog.records)


def test_garbage_text_returns_none(caplog):
    import logging
    resp = _FakeResponse("this is not json at all!!!")
    with caplog.at_level(logging.WARNING):
        result = _parse_rule(resp, min_evidence=3, trace_id=None)
    assert result is None
    assert any("rule_extractor.parse_failed" in r.message for r in caplog.records)


def test_extra_keys_tolerated():
    resp = _resp({"rule": "Be concise.", "evidence_count": 4, "rationale": "ok", "extra": "field"})
    result = _parse_rule(resp, min_evidence=3, trace_id=None)
    assert result == "Be concise."


def test_missing_rule_field_returns_none(caplog):
    import logging
    resp = _resp({"evidence_count": 4, "rationale": "ok"})
    with caplog.at_level(logging.WARNING):
        result = _parse_rule(resp, min_evidence=3, trace_id=None)
    assert result is None
    assert any("rule_extractor.parse_failed" in r.message for r in caplog.records)


def test_non_object_returns_none(caplog):
    import logging
    resp = _FakeResponse("[1, 2, 3]")
    with caplog.at_level(logging.WARNING):
        result = _parse_rule(resp, min_evidence=3, trace_id=None)
    assert result is None
