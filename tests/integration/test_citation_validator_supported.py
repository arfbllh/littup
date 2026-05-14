"""Integration: MockProvider returns supported for every pair → all claims supported, groundedness=1.0."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.draft.validator import CitationValidator

UUID1 = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
UUID2 = "11111111-2222-3333-4444-555555555555"


def _make_chunk(chunk_id: str, text: str):
    chunk = MagicMock()
    chunk.id = chunk_id
    chunk.text = text
    return chunk


def _make_router(statuses: list[str]) -> MagicMock:
    results = [{"pair_idx": i, "status": s, "reason": "ok"} for i, s in enumerate(statuses)]
    payload = {"results": results}
    response = MagicMock()
    response.structured = payload
    response.text = json.dumps(payload)
    response.tokens_in = 10
    response.tokens_out = 20
    response.cost_usd = 0.001
    response.model_used = "mock-model"
    router = MagicMock()
    router.generate = AsyncMock(return_value=response)
    return router


@pytest.mark.asyncio
async def test_all_supported_groundedness_one():
    section_text = (
        f"The claimant filed the petition [chunk:{UUID1}]. "
        f"The court accepted jurisdiction [chunk:{UUID2}]."
    )
    chunks_by_id = {
        UUID1: _make_chunk(UUID1, "The claimant filed a petition with the court."),
        UUID2: _make_chunk(UUID2, "The court accepted jurisdiction over the matter."),
    }

    router = _make_router(["supported", "supported"])
    validator = CitationValidator(router)

    report = await validator.validate_section(
        section_text, [], chunks_by_id, "fp123", trace_id=None
    )

    assert report.total_claims >= 1
    assert report.unsupported_count == 0
    assert report.contradicted_count == 0
    # All cited claims should be supported
    cited_claims = [cv for cv in report.per_claim if cv.claim.cited_chunk_ids]
    assert all(cv.status == "supported" for cv in cited_claims)
    # Groundedness = supported / total
    gnd = report.supported_count / report.total_claims if report.total_claims else 0.0
    assert gnd == 1.0


@pytest.mark.asyncio
async def test_no_claims_returns_empty_report():
    router = MagicMock()
    router.generate = AsyncMock()
    validator = CitationValidator(router)

    report = await validator.validate_section("", [], {}, "fp123", trace_id=None)

    assert report.total_claims == 0
    assert report.per_claim == []
    router.generate.assert_not_called()
