"""Tests for claim segmentation — segment_claims() in app/draft/citations.py.

Fixture text deliberately avoids abbreviations like Inc., U.S.C., et al. to
prevent the simple sentence-split regex from producing false boundaries.
"""
from __future__ import annotations

from app.draft.citations import Claim, segment_claims

UUID1 = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
UUID2 = "11111111-2222-3333-4444-555555555555"
UUID3 = "22222222-3333-4444-5555-666666666666"


def test_single_cited_sentence():
    text = f"The defendant signed the contract [chunk:{UUID1}]."
    claims = segment_claims(text, [])
    assert len(claims) >= 1
    cited = [c for c in claims if c.cited_chunk_ids]
    assert any(UUID1 in c.cited_chunk_ids for c in cited)


def test_uncited_sentence_becomes_claim():
    text = "This statement has no citation."
    claims = segment_claims(text, [])
    assert len(claims) == 1
    assert claims[0].cited_chunk_ids == []
    assert "no citation" in claims[0].text


def test_mixed_cited_and_uncited():
    text = (
        f"The parties entered an agreement [chunk:{UUID1}]. "
        "However, no date was established."
    )
    claims = segment_claims(text, [])
    cited_claims = [c for c in claims if c.cited_chunk_ids]
    uncited_claims = [c for c in claims if not c.cited_chunk_ids]
    assert len(cited_claims) >= 1
    assert any(UUID1 in c.cited_chunk_ids for c in cited_claims)
    assert len(uncited_claims) >= 1


def test_multiple_citations():
    text = (
        f"Fact A was established [chunk:{UUID1}]. "
        f"Fact B was also confirmed [chunk:{UUID2}]."
    )
    claims = segment_claims(text, [])
    cited = [c for c in claims if c.cited_chunk_ids]
    chunk_ids_seen = {cid for c in cited for cid in c.cited_chunk_ids}
    assert UUID1 in chunk_ids_seen
    assert UUID2 in chunk_ids_seen


def test_empty_text_returns_empty():
    assert segment_claims("", []) == []


def test_char_spans_are_within_text():
    text = (
        f"Claim one [chunk:{UUID1}]. Claim two without citation. "
        f"Claim three [chunk:{UUID2}]."
    )
    claims = segment_claims(text, [])
    for c in claims:
        start, end = c.char_span
        assert 0 <= start <= end <= len(text)


def test_no_citations_entire_text_is_one_claim_or_split_by_sentences():
    text = "First sentence. Second sentence. Third sentence."
    claims = segment_claims(text, [])
    # Every claim should have no cited_chunk_ids
    assert all(c.cited_chunk_ids == [] for c in claims)
    # All text should be covered
    reconstructed = " ".join(c.text for c in claims)
    for word in ["First", "Second", "Third"]:
        assert word in reconstructed
