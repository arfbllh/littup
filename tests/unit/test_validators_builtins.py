"""Tests for ValidatorRegistry built-in validators."""
from __future__ import annotations

import pytest

from app.draft.templates.validators import ValidatorRegistry


class TestNonEmpty:
    def test_non_empty_string(self):
        assert ValidatorRegistry.non_empty("hello") is True

    def test_empty_string_fails(self):
        assert ValidatorRegistry.non_empty("") is False

    def test_whitespace_string_fails(self):
        assert ValidatorRegistry.non_empty("   ") is False

    def test_none_fails(self):
        assert ValidatorRegistry.non_empty(None) is False

    def test_non_empty_list(self):
        assert ValidatorRegistry.non_empty(["a"]) is True

    def test_empty_list_fails(self):
        assert ValidatorRegistry.non_empty([]) is False


class TestIsDate:
    def test_iso_date(self):
        assert ValidatorRegistry.is_date("2024-01-15") is True

    def test_us_date(self):
        assert ValidatorRegistry.is_date("January 15, 2024") is True

    def test_short_date(self):
        assert ValidatorRegistry.is_date("01/15/2024") is True

    def test_not_a_date(self):
        assert ValidatorRegistry.is_date("not a date") is False

    def test_empty_string_is_not_date(self):
        assert ValidatorRegistry.is_date("") is False


class TestIsMoney:
    def test_dollar_amount(self):
        assert ValidatorRegistry.is_money("$1,000,000.00") is True

    def test_no_dollar_sign(self):
        assert ValidatorRegistry.is_money("500") is True

    def test_with_cents(self):
        assert ValidatorRegistry.is_money("$99.99") is True

    def test_word_fails(self):
        assert ValidatorRegistry.is_money("five hundred dollars") is False

    def test_negative_fails(self):
        assert ValidatorRegistry.is_money("-100") is False


class TestLengthBetween:
    def test_within_range(self):
        text = " ".join(["word"] * 10)
        assert ValidatorRegistry.length_between(text, 5, 20) is True

    def test_too_short(self):
        text = "short text"
        assert ValidatorRegistry.length_between(text, 10, 100) is False

    def test_too_long(self):
        text = " ".join(["word"] * 50)
        assert ValidatorRegistry.length_between(text, 1, 20) is False

    def test_exact_min(self):
        text = " ".join(["word"] * 5)
        assert ValidatorRegistry.length_between(text, 5, 10) is True

    def test_exact_max(self):
        text = " ".join(["word"] * 10)
        assert ValidatorRegistry.length_between(text, 5, 10) is True


class TestCitesAtLeastOne:
    def test_with_citations(self):
        assert ValidatorRegistry.cites_at_least_one(["citation1"]) is True

    def test_empty_list_fails(self):
        assert ValidatorRegistry.cites_at_least_one([]) is False

    def test_multiple_citations(self):
        assert ValidatorRegistry.cites_at_least_one(["a", "b", "c"]) is True
