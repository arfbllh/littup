"""Tests for DraftTemplate.compute_fingerprint() stability and sensitivity."""
from __future__ import annotations

import copy

import pytest

from app.draft.templates.schema import DraftTemplate, FieldSpec, SectionSpec


def _base_template() -> dict:
    return {
        "id": "test",
        "display_name": "Test",
        "system_prompt": "You are a legal analyst.",
        "appended_rules": ["Rule A", "Rule B"],
        "retrieval_queries": {"parties": "party names"},
        "extraction_schema": [
            {"name": "parties", "type": "list[party]", "description": "Parties", "retrieval_key": "parties"},
            {"name": "date", "type": "date", "description": "Date", "retrieval_key": "parties"},
        ],
        "sections": [
            {
                "name": "summary",
                "description": "Summary",
                "retrieval_key": "parties",
                "target_length_min": 50,
                "target_length_max": 200,
            }
        ],
    }


def test_round_trip_stability():
    data = _base_template()
    t1 = DraftTemplate.model_validate(data)
    t2 = DraftTemplate.model_validate(data)
    assert t1.compute_fingerprint() == t2.compute_fingerprint()


def test_system_prompt_change_flips_fingerprint():
    data = _base_template()
    t1 = DraftTemplate.model_validate(data)

    data2 = copy.deepcopy(data)
    data2["system_prompt"] = "You are a different analyst."
    t2 = DraftTemplate.model_validate(data2)

    assert t1.compute_fingerprint() != t2.compute_fingerprint()


def test_appended_rules_reorder_flips_fingerprint():
    data = _base_template()
    t1 = DraftTemplate.model_validate(data)

    data2 = copy.deepcopy(data)
    data2["appended_rules"] = ["Rule B", "Rule A"]  # reversed
    t2 = DraftTemplate.model_validate(data2)

    assert t1.compute_fingerprint() != t2.compute_fingerprint()


def test_extraction_schema_field_reorder_does_not_flip_fingerprint():
    data = _base_template()
    t1 = DraftTemplate.model_validate(data)

    data2 = copy.deepcopy(data)
    # Reverse the field order — dict in fingerprint is sorted by name, so order shouldn't matter
    data2["extraction_schema"] = list(reversed(data2["extraction_schema"]))
    t2 = DraftTemplate.model_validate(data2)

    assert t1.compute_fingerprint() == t2.compute_fingerprint()
