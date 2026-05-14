from __future__ import annotations
import pytest
from app.retrieval.fusion import rrf_fuse


def test_rrf_basic():
    list1 = [("A", 0.9), ("B", 0.8), ("C", 0.7)]
    list2 = [("B", 0.95), ("A", 0.85), ("D", 0.75)]
    result = rrf_fuse([list1, list2], k=60, weights=[1.0, 1.0])
    result_dict = dict(result)
    expected_A = 1.0 / 61 + 1.0 / 62
    expected_B = 1.0 / 62 + 1.0 / 61
    assert abs(result_dict["A"] - expected_A) < 1e-10
    assert abs(result_dict["B"] - expected_B) < 1e-10


def test_rrf_three_list_default_weights():
    list1 = [("A", 0.9)]
    list2 = [("A", 0.8)]
    list3 = [("A", 0.7)]
    result = rrf_fuse([list1, list2, list3], k=60)
    result_dict = dict(result)
    expected_A = 1.0 / 61 + 1.0 / 61 + 0.5 / 61
    assert abs(result_dict["A"] - expected_A) < 1e-10


def test_rrf_empty_list():
    result = rrf_fuse([[], [("A", 0.9)]])
    assert len(result) == 1
    assert result[0][0] == "A"
    assert abs(result[0][1] - 1.0 / 61) < 1e-10


def test_rrf_single_list():
    list1 = [("A", 0.9), ("B", 0.8)]
    result = rrf_fuse([list1], k=60, weights=[1.0])
    result_dict = dict(result)
    assert abs(result_dict["A"] - 1.0 / 61) < 1e-10
    assert abs(result_dict["B"] - 1.0 / 62) < 1e-10


def test_rrf_all_three_beats_one():
    list1 = [("A", 0.9), ("B", 0.5)]
    list2 = [("A", 0.8), ("B", 0.5)]
    list3 = [("A", 0.7), ("B", 0.5)]
    result = rrf_fuse([list1, list2, list3], k=60)
    result_dict = dict(result)
    assert result_dict["A"] > result_dict["B"]


def test_rrf_sorted_descending():
    list1 = [("C", 0.9), ("B", 0.8), ("A", 0.7)]
    list2 = [("A", 0.95), ("B", 0.85), ("C", 0.75)]
    result = rrf_fuse([list1, list2], k=60, weights=[1.0, 1.0])
    scores = [score for _, score in result]
    assert scores == sorted(scores, reverse=True)
