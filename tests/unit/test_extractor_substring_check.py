"""Tests for FieldExtractor substring sanity check."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.draft.extractor import FieldExtraction, FieldExtractor
from app.draft.templates.schema import FieldSpec


def _make_chunk(chunk_id: str, text: str):
    chunk = MagicMock()
    chunk.id = chunk_id
    chunk.text = text
    return chunk


def _make_field_spec(name: str = "parties", field_type: str = "string") -> FieldSpec:
    return FieldSpec(
        name=name,
        type=field_type,  # type: ignore[arg-type]
        description="Test field",
        retrieval_key=name,
    )


def _make_template(field_spec: FieldSpec):
    template = MagicMock()
    template.extraction_schema = [field_spec]
    return template


@pytest.mark.asyncio
async def test_substring_mismatch_returns_none_confidence_zero():
    chunk_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    chunk = _make_chunk(chunk_id, "The quick brown fox jumps over the lazy dog.")

    router = MagicMock()
    response = MagicMock()
    response.structured = {
        "value": "completely fabricated text not in any chunk",
        "supporting_chunk_ids": [chunk_id],
        "confidence": 0.9,
    }
    router.generate = AsyncMock(return_value=response)

    field_spec = _make_field_spec()
    template = _make_template(field_spec)

    extractor = FieldExtractor(router)
    retrieved = {field_spec.retrieval_key: [chunk]}

    result = await extractor._extract_field(field_spec, retrieved, "fp123", None)

    assert result.value is None
    assert result.confidence == 0.0
    assert result.error_code == "SUBSTRING_MISMATCH"


@pytest.mark.asyncio
async def test_substring_present_returns_value():
    chunk_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    chunk = _make_chunk(chunk_id, "The defendant is John Smith in this matter.")

    router = MagicMock()
    response = MagicMock()
    response.structured = {
        "value": "John Smith",
        "supporting_chunk_ids": [chunk_id],
        "confidence": 0.95,
    }
    router.generate = AsyncMock(return_value=response)

    field_spec = _make_field_spec()
    template = _make_template(field_spec)

    extractor = FieldExtractor(router)
    retrieved = {field_spec.retrieval_key: [chunk]}

    result = await extractor._extract_field(field_spec, retrieved, "fp123", None)

    assert result.value == "John Smith"
    assert result.confidence > 0.0
    assert result.error_code is None


@pytest.mark.asyncio
async def test_no_chunks_returns_none():
    router = MagicMock()
    field_spec = _make_field_spec()
    extractor = FieldExtractor(router)

    result = await extractor._extract_field(field_spec, {}, "fp123", None)

    assert result.value is None
    assert result.confidence == 0.0
    router.generate.assert_not_called()
