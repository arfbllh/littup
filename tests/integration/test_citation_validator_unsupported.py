"""Integration: unsupported claim detection — semantic pass and trivial (fabricated chunk_id) pass."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.draft.generator import CitationDraft
from app.draft.validator import CitationValidator

UUID1 = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
FAKE_UUID = "ffffffff-ffff-ffff-ffff-ffffffffffff"


def _make_chunk(chunk_id: str, text: str):
    chunk = MagicMock()
    chunk.id = chunk_id
    chunk.text = text
    return chunk


def _make_router_with_status(status: str) -> MagicMock:
    payload = {"results": [{"pair_idx": 0, "status": status, "reason": "does not match"}]}
    response = MagicMock()
    response.structured = payload
    response.text = json.dumps(payload)
    response.tokens_in = 5
    response.tokens_out = 10
    response.cost_usd = 0.0
    response.model_used = "mock"
    router = MagicMock()
    router.generate = AsyncMock(return_value=response)
    return router


@pytest.mark.asyncio
async def test_semantic_unsupported_caught():
    """(a) LLM says unsupported for a real chunk that doesn't support the claim."""
    section_text = f"The defendant admitted guilt [chunk:{UUID1}]."
    chunks_by_id = {
        UUID1: _make_chunk(UUID1, "The court proceedings are scheduled for Monday."),
    }
    router = _make_router_with_status("unsupported")
    validator = CitationValidator(router)

    report = await validator.validate_section(
        section_text, [], chunks_by_id, "fp123", trace_id=None
    )

    cited = [cv for cv in report.per_claim if cv.claim.cited_chunk_ids]
    assert len(cited) >= 1
    assert cited[0].status == "unsupported"
    assert report.unsupported_count >= 1


@pytest.mark.asyncio
async def test_fabricated_chunk_id_trivial_pass_no_llm():
    """(b) Fabricated UUID not in chunks_by_id → trivial pass marks unsupported without LLM call."""
    section_text = f"A made-up fact [chunk:{FAKE_UUID}]."
    chunks_by_id = {
        UUID1: _make_chunk(UUID1, "Some real chunk text."),
    }

    router = MagicMock()
    router.generate = AsyncMock()
    validator = CitationValidator(router)

    report = await validator.validate_section(
        section_text, [], chunks_by_id, "fp123", trace_id=None
    )

    router.generate.assert_not_called()
    cited = [cv for cv in report.per_claim if cv.claim.cited_chunk_ids]
    assert any(cv.status == "unsupported" for cv in cited)
    assert any("chunk_id not in retrieved set" in (cv.reason or "") for cv in cited)


@pytest.mark.asyncio
async def test_uncited_sentence_auto_unsupported():
    """Sentence with no citation → auto unsupported, no LLM call."""
    section_text = "This claim has no citation at all."
    router = MagicMock()
    router.generate = AsyncMock()
    validator = CitationValidator(router)

    report = await validator.validate_section(
        section_text, [], {}, "fp123", trace_id=None
    )

    router.generate.assert_not_called()
    assert report.total_claims >= 1
    uncited = [cv for cv in report.per_claim if not cv.claim.cited_chunk_ids]
    assert all(cv.status == "unsupported" for cv in uncited)
    assert all("claim has no citation" in (cv.reason or "") for cv in uncited)
