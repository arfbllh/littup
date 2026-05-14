from __future__ import annotations
import pytest

from app.retrieval.retriever import _should_run_trigram


def test_multi_word_proper_noun_triggers():
    assert _should_run_trigram("John Smith filed a motion") is True


def test_three_word_proper_noun_triggers():
    assert _should_run_trigram("Pearson Specter Litt v. Harvey Ross") is True


def test_section_symbol_triggers():
    assert _should_run_trigram("rights under § 1983") is True


def test_section_symbol_at_end_triggers():
    assert _should_run_trigram("see § 1983 for details") is True


def test_all_lowercase_does_not_trigger():
    assert _should_run_trigram("who are the parties to the case") is False


def test_single_sentence_start_capital_does_not_trigger():
    # "The" alone doesn't form a multi-word proper noun
    assert _should_run_trigram("The plaintiff filed a motion") is False


def test_single_capitalized_word_does_not_trigger():
    assert _should_run_trigram("Plaintiff seeks damages") is False


def test_empty_query_does_not_trigger():
    assert _should_run_trigram("") is False


def test_always_trigram_flag_overrides(monkeypatch):
    from app.settings import settings
    monkeypatch.setattr(settings, "RETRIEVER_ALWAYS_TRIGRAM", True)
    assert _should_run_trigram("who are the parties") is True


def test_always_trigram_false_uses_detection(monkeypatch):
    from app.settings import settings
    monkeypatch.setattr(settings, "RETRIEVER_ALWAYS_TRIGRAM", False)
    assert _should_run_trigram("who are the parties") is False
    assert _should_run_trigram("Harvey Specter filed") is True
