"""Unit tests for app.edits.diff."""
from __future__ import annotations

import unicodedata

import pytest

from app.draft.templates.schema import FieldSpec, SectionSpec
from app.edits.diff import (
    StructuredDiff,
    compute_field_diff,
    compute_section_diff,
)


def _field(name: str, ftype: str) -> FieldSpec:
    return FieldSpec(name=name, type=ftype, description="", retrieval_key=name)


# ─── Field diffs ──────────────────────────────────────────────────────────────

def test_scalar_modified():
    spec = _field("filing_date", "date")
    diff = compute_field_diff(spec, "2025-01-01", "2025-02-02")
    assert diff is not None
    assert diff["operation"] == "modified"
    assert diff["ai"] == "2025-01-01"
    assert diff["user"] == "2025-02-02"


def test_scalar_equal_returns_none():
    spec = _field("jurisdiction", "string")
    assert compute_field_diff(spec, "SDNY", "SDNY") is None


def test_scalar_strip_whitespace_equal():
    spec = _field("jurisdiction", "string")
    assert compute_field_diff(spec, " SDNY ", "SDNY") is None


def test_list_string_added_removed():
    spec = _field("claims", "list[string]")
    ai = ["breach of contract", "fraud"]
    user = ["breach of contract", "negligence"]
    diff = compute_field_diff(spec, ai, user)
    assert diff is not None
    assert diff["operation"] == "modified"
    assert "negligence" in diff["added_items"]
    assert "fraud" in diff["removed_items"]


def test_list_string_equal_returns_none():
    spec = _field("claims", "list[string]")
    assert compute_field_diff(spec, ["a", "b"], ["b", "a"]) is None


def test_list_party_identity_lowercased():
    spec = _field("parties", "list[party]")
    ai = [{"name": "Smith", "role": "plaintiff"}]
    user = [{"name": "smith", "role": "plaintiff"}]
    assert compute_field_diff(spec, ai, user) is None


def test_list_party_added():
    spec = _field("parties", "list[party]")
    ai = [{"name": "Smith", "role": "plaintiff"}]
    user = [{"name": "Smith", "role": "plaintiff"}, {"name": "Jones", "role": "defendant"}]
    diff = compute_field_diff(spec, ai, user)
    assert diff is not None
    assert diff["operation"] == "modified"
    added_names = [p["name"] for p in diff["added_items"]]
    assert "Jones" in added_names
    assert diff["removed_items"] == []


def test_list_party_added_full_object():
    spec = _field("parties", "list[party]")
    new_party = {"name": "Jones Corp", "role": "defendant"}
    ai: list = []
    user = [new_party]
    diff = compute_field_diff(spec, ai, user)
    assert diff is not None
    assert diff["added_items"][0] == new_party


def test_added_from_none():
    spec = _field("damages_sought", "money")
    diff = compute_field_diff(spec, None, "$1,000,000")
    assert diff is not None
    assert diff["operation"] == "added"
    assert diff["user"] == "$1,000,000"


def test_removed_to_none():
    spec = _field("damages_sought", "money")
    diff = compute_field_diff(spec, "$500,000", None)
    assert diff is not None
    assert diff["operation"] == "removed"
    assert diff["ai"] == "$500,000"


# ─── Section diffs ────────────────────────────────────────────────────────────

def test_section_text_equal_returns_none():
    assert compute_section_diff("background", "Same text.", "Same text.") is None


def test_section_text_replace():
    diff = compute_section_diff("background", "The plaintiff filed.", "The defendant filed.")
    assert diff is not None
    assert diff["section"] == "background"
    replace_ops = [c for c in diff["char_changes"] if c["op"] == "replace"]
    assert len(replace_ops) > 0


def test_section_text_normalize_nfc():
    # Two strings that are NFC-equivalent should produce no diff
    s1 = "café"          # é as single code point
    s2 = "café"         # é as e + combining accent
    assert unicodedata.normalize("NFC", s1) == unicodedata.normalize("NFC", s2)
    assert compute_section_diff("summary", s1, s2) is None


def test_section_text_has_ai_user_texts():
    diff = compute_section_diff("background", "old text", "new text")
    assert diff is not None
    assert diff["ai_text"] == "old text"
    assert diff["user_text"] == "new text"


# ─── StructuredDiff helpers ───────────────────────────────────────────────────

def test_top_level_is_empty():
    sd = StructuredDiff(fields={}, sections={})
    assert sd.is_empty() is True


def test_top_level_not_empty_with_field():
    sd = StructuredDiff(fields={"parties": {"operation": "modified"}}, sections={})
    assert sd.is_empty() is False


def test_top_level_not_empty_with_section():
    sd = StructuredDiff(fields={}, sections={"background": {"section": "background"}})
    assert sd.is_empty() is False
