"""Integration: groundedness score calculation.

7 supported / 2 partial / 1 unsupported across 10 claims → groundedness_score = 0.7.
Weighting decision: strict supported/total (partial does not count as half).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.draft.validator import CitationValidator

def _uuid(i: int) -> str:
    return f"a{i:07d}-0000-0000-0000-000000000000"


def _make_chunk(chunk_id: str, text: str):
    chunk = MagicMock()
    chunk.id = chunk_id
    chunk.text = text
    return chunk


def _build_section_and_chunks(n: int) -> tuple[str, dict]:
    parts = []
    chunks_by_id = {}
    for i in range(n):
        uid = _uuid(i)
        parts.append(f"Claim number {i} [chunk:{uid}].")
        chunks_by_id[uid] = _make_chunk(uid, f"Evidence chunk {i}.")
    return " ".join(parts), chunks_by_id


@pytest.mark.asyncio
async def test_groundedness_score_07():
    """7 supported, 2 partial, 1 unsupported → score = 7/10 = 0.7."""
    n = 10
    section_text, chunks_by_id = _build_section_and_chunks(n)

    statuses = (
        ["supported"] * 7
        + ["partial"] * 2
        + ["unsupported"] * 1
    )
    results = [{"pair_idx": i, "status": s, "reason": ""} for i, s in enumerate(statuses)]
    payload = {"results": results}
    response = MagicMock()
    response.structured = payload
    response.text = json.dumps(payload)
    response.tokens_in = 50
    response.tokens_out = 100
    response.cost_usd = 0.001
    response.model_used = "mock"
    router = MagicMock()
    router.generate = AsyncMock(return_value=response)

    validator = CitationValidator(router)
    report = await validator.validate_section(
        section_text, [], chunks_by_id, "fp123", trace_id=None
    )

    assert report.total_claims == n
    assert report.supported_count == 7
    assert report.partial_count == 2
    assert report.unsupported_count == 1

    # Strict weighting: supported / total (partial counts as 0, not 0.5)
    score = report.supported_count / report.total_claims
    assert abs(score - 0.7) < 1e-9
