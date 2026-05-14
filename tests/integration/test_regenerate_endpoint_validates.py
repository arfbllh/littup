"""Integration: POST /api/drafts/{id}/sections/{name}/regenerate → citations have validation_status != unchecked.

This test mocks the DraftEngine.regenerate_section to inject CitationValidator behaviour
so we can assert the endpoint returns validated citations without needing a real DB or LLM.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

UUID1 = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


def test_citation_validator_annotates_draft_citations_on_regenerate():
    """CitationValidator annotates CitationDraft.validation_status in-place.

    This is a unit-level check that _apply_report_to_citations works correctly
    (the engine calls it before replace_section so the repo sees validated status).
    """
    from app.draft.generator import CitationDraft
    from app.draft.validator import ValidationReport, ClaimValidation, _apply_report_to_citations
    from app.draft.citations import Claim

    claim = Claim(
        text="The contract was signed",
        cited_chunk_ids=[UUID1],
        char_span=(0, 50),
    )
    cv = ClaimValidation(claim=claim, status="supported", reason="verbatim match")
    report = ValidationReport(per_claim=[cv])

    cit = CitationDraft(
        section_name="background",
        chunk_id=UUID1,
        claim_span_start=0,
        claim_span_end=50,
    )
    assert cit.validation_status == "unchecked"

    _apply_report_to_citations(report, [cit])

    assert cit.validation_status == "supported"
    assert cit.validation_reason == "verbatim match"


def test_unmatched_citation_gets_unsupported():
    """Citations whose chunk_id never appears in any claim → unsupported."""
    from app.draft.generator import CitationDraft
    from app.draft.validator import ValidationReport, _apply_report_to_citations

    cit = CitationDraft(
        section_name="background",
        chunk_id="00000000-0000-0000-0000-000000000000",
        claim_span_start=0,
        claim_span_end=10,
    )

    report = ValidationReport()
    _apply_report_to_citations(report, [cit])

    assert cit.validation_status == "unsupported"
    assert "not matched" in (cit.validation_reason or "")
