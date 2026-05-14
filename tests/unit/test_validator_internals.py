"""Unit tests for CitationValidator internals.

Covers: _merge_statuses, _apply_report_to_citations, batch-size boundary,
LLM failure fallback, and ValidationReport computed properties.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.draft.citations import Claim
from app.draft.generator import CitationDraft
from app.draft.validator import (
    CitationValidator,
    ClaimValidation,
    ValidationReport,
    _apply_report_to_citations,
    _merge_statuses,
)

UUID1 = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
UUID2 = "11111111-2222-3333-4444-555555555555"


# ── _merge_statuses ──────────────────────────────────────────────────────────

def test_merge_empty_returns_unsupported():
    assert _merge_statuses([]) == "unsupported"


def test_merge_all_supported():
    assert _merge_statuses(["supported", "supported"]) == "supported"


def test_merge_partial_overrides_supported():
    assert _merge_statuses(["supported", "partial"]) == "partial"


def test_merge_unsupported_overrides_partial():
    assert _merge_statuses(["partial", "unsupported"]) == "unsupported"


def test_merge_contradicted_overrides_all():
    assert _merge_statuses(["supported", "partial", "unsupported", "contradicted"]) == "contradicted"


def test_merge_single_contradicted():
    assert _merge_statuses(["contradicted"]) == "contradicted"


# ── ValidationReport computed properties ────────────────────────────────────

def _make_cv(status: str) -> ClaimValidation:
    claim = Claim(text="x", cited_chunk_ids=[UUID1], char_span=(0, 1))
    return ClaimValidation(claim=claim, status=status)


def test_report_counts_match_per_claim():
    report = ValidationReport(per_claim=[
        _make_cv("supported"),
        _make_cv("supported"),
        _make_cv("partial"),
        _make_cv("unsupported"),
        _make_cv("contradicted"),
    ])
    assert report.total_claims == 5
    assert report.supported_count == 2
    assert report.partial_count == 1
    assert report.unsupported_count == 1
    assert report.contradicted_count == 1


def test_empty_report_all_zeros():
    report = ValidationReport()
    assert report.total_claims == 0
    assert report.supported_count == 0


# ── _apply_report_to_citations ───────────────────────────────────────────────

def test_apply_shared_chunk_id_first_claim_wins():
    """Two claims reference the same chunk_id; first claim's status is stored."""
    chunk_id = UUID1
    claim_a = Claim(text="Claim A", cited_chunk_ids=[chunk_id], char_span=(0, 10))
    claim_b = Claim(text="Claim B", cited_chunk_ids=[chunk_id], char_span=(10, 20))
    cv_a = ClaimValidation(claim=claim_a, status="supported", reason="reason A")
    cv_b = ClaimValidation(claim=claim_b, status="unsupported", reason="reason B")

    report = ValidationReport(per_claim=[cv_a, cv_b])

    cit = CitationDraft(
        section_name="background",
        chunk_id=chunk_id,
        claim_span_start=0,
        claim_span_end=10,
    )
    _apply_report_to_citations(report, [cit])

    # First claim wins — "supported" from cv_a
    assert cit.validation_status == "supported"
    assert cit.validation_reason == "reason A"


def test_apply_unmatched_citation_unsupported():
    claim = Claim(text="Claim", cited_chunk_ids=[UUID1], char_span=(0, 5))
    cv = ClaimValidation(claim=claim, status="supported", reason="ok")
    report = ValidationReport(per_claim=[cv])

    unmatched_cit = CitationDraft(
        section_name="s",
        chunk_id=UUID2,  # different UUID, not in any claim
        claim_span_start=0,
        claim_span_end=5,
    )
    _apply_report_to_citations(report, [unmatched_cit])

    assert unmatched_cit.validation_status == "unsupported"
    assert "not matched" in (unmatched_cit.validation_reason or "")


# ── Batch size boundary ──────────────────────────────────────────────────────

def _uuid(i: int) -> str:
    return f"a{i:07d}-0000-0000-0000-000000000000"


def _make_chunk(uid: str, text: str):
    c = MagicMock()
    c.id = uid
    c.text = text
    return c


def _router_for_n(n: int) -> MagicMock:
    """LLM router that returns 'supported' for every pair, handling multi-batch calls."""
    async def generate(messages, *, task, schema=None, sampling=None,
                       trace_id=None, cache=True, **kw):
        # Parse pair count from message content
        user_content = next(
            (m.content if hasattr(m, "content") else m.get("content", "")
             for m in messages if (m.role if hasattr(m, "role") else m.get("role")) == "user"),
            "",
        )
        n_pairs = user_content.count("Pair ")
        results = [{"pair_idx": i, "status": "supported", "reason": "ok"}
                   for i in range(n_pairs)]
        payload = {"results": results}
        resp = MagicMock()
        resp.structured = payload
        resp.text = json.dumps(payload)
        resp.tokens_in = n_pairs * 10
        resp.tokens_out = n_pairs * 5
        resp.cost_usd = 0.0
        resp.model_used = "mock"
        return resp

    router = MagicMock()
    router.generate = AsyncMock(side_effect=generate)
    return router


@pytest.mark.asyncio
async def test_exactly_batch_size_pairs_single_call():
    """10 claims == _BATCH_SIZE → exactly one LLM call."""
    from app.draft.validator import _BATCH_SIZE
    n = _BATCH_SIZE
    uuids = [_uuid(i) for i in range(n)]
    section_text = " ".join(f"Claim {i} [chunk:{uuids[i]}]." for i in range(n))
    chunks_by_id = {uid: _make_chunk(uid, f"Evidence {i}.") for i, uid in enumerate(uuids)}

    router = _router_for_n(n)
    validator = CitationValidator(router)
    report = await validator.validate_section(section_text, [], chunks_by_id, "fp", trace_id=None)

    assert report.total_claims == n
    assert report.supported_count == n
    router.generate.assert_called_once()


@pytest.mark.asyncio
async def test_batch_size_plus_one_two_calls():
    """11 claims > _BATCH_SIZE=10 → exactly two LLM calls."""
    from app.draft.validator import _BATCH_SIZE
    n = _BATCH_SIZE + 1
    uuids = [_uuid(i) for i in range(n)]
    section_text = " ".join(f"Claim {i} [chunk:{uuids[i]}]." for i in range(n))
    chunks_by_id = {uid: _make_chunk(uid, f"Evidence {i}.") for i, uid in enumerate(uuids)}

    router = _router_for_n(n)
    validator = CitationValidator(router)
    report = await validator.validate_section(section_text, [], chunks_by_id, "fp", trace_id=None)

    assert report.total_claims == n
    assert report.supported_count == n
    assert router.generate.call_count == 2


# ── LLM failure fallback ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_exception_marks_all_claims_unsupported():
    """If the LLM call raises, every claim in the batch is marked unsupported."""
    section_text = f"Claim A [chunk:{UUID1}]."
    chunks_by_id = {UUID1: _make_chunk(UUID1, "Some text.")}

    router = MagicMock()
    router.generate = AsyncMock(side_effect=RuntimeError("network error"))
    validator = CitationValidator(router)

    report = await validator.validate_section(section_text, [], chunks_by_id, "fp", trace_id=None)

    assert report.total_claims == 1
    cited = [cv for cv in report.per_claim if cv.claim.cited_chunk_ids]
    assert len(cited) == 1
    assert cited[0].status == "unsupported"
    assert "unparseable" in (cited[0].reason or "")


@pytest.mark.asyncio
async def test_llm_bad_json_marks_claims_unsupported():
    """If LLM returns non-JSON text, every claim is marked unsupported."""
    section_text = f"Some claim [chunk:{UUID1}]."
    chunks_by_id = {UUID1: _make_chunk(UUID1, "Evidence.")}

    resp = MagicMock()
    resp.structured = None
    resp.text = "I cannot validate this."
    resp.tokens_in = 0
    resp.tokens_out = 0
    resp.cost_usd = 0.0
    resp.model_used = "mock"
    router = MagicMock()
    router.generate = AsyncMock(return_value=resp)
    validator = CitationValidator(router)

    report = await validator.validate_section(section_text, [], chunks_by_id, "fp", trace_id=None)

    cited = [cv for cv in report.per_claim if cv.claim.cited_chunk_ids]
    assert all(cv.status == "unsupported" for cv in cited)
