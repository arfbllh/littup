from __future__ import annotations
import pytest
from app.retrieval.fusion import entity_overlap_bonus


def test_entity_overlap_bonus_match():
    bonus = entity_overlap_bonus(
        query_entities=["Pearson Specter Litt"],
        chunk_entities_map={"chunk1": ["Pearson Specter Litt", "Harvey Specter"], "chunk2": ["Louis Litt"]},
        bonus_per_match=0.1,
    )
    assert bonus.get("chunk1", 0) == pytest.approx(0.1)
    assert "chunk2" not in bonus


def test_entity_overlap_bonus_case_insensitive():
    bonus = entity_overlap_bonus(
        query_entities=["pearson specter litt"],
        chunk_entities_map={"chunk1": ["Pearson Specter Litt"]},
    )
    assert bonus.get("chunk1", 0) == pytest.approx(0.1)


def test_entity_overlap_bonus_multiple_matches():
    bonus = entity_overlap_bonus(
        query_entities=["Harvey Specter", "Donna Paulsen"],
        chunk_entities_map={"chunk1": ["Harvey Specter", "Donna Paulsen", "Mike Ross"]},
    )
    assert bonus.get("chunk1", 0) == pytest.approx(0.2)


def test_entity_overlap_no_match():
    bonus = entity_overlap_bonus(
        query_entities=["Harvey Specter"],
        chunk_entities_map={"chunk1": ["Mike Ross"]},
    )
    assert bonus == {}
