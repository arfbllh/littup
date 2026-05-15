"""Tests for FieldExtractor substring sanity check (batch extraction)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.draft.extractor import FieldExtractor
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
    field_spec = _make_field_spec()

    router = MagicMock()
    response = MagicMock()
    response.structured = {
        field_spec.name: "completely fabricated text not in any chunk"
    }
    router.generate = AsyncMock(return_value=response)

    template = _make_template(field_spec)
    extractor = FieldExtractor(router)
    retrieved = {field_spec.retrieval_key: [chunk]}

    results = await extractor.extract_all(template, retrieved, "fp123", None)
    result = results[field_spec.name]

    assert result.value is None
    assert result.confidence == 0.0
    assert result.error_code == "SUBSTRING_MISMATCH"


@pytest.mark.asyncio
async def test_substring_present_returns_value():
    chunk_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    chunk = _make_chunk(chunk_id, "The defendant is John Smith in this matter.")
    field_spec = _make_field_spec()

    router = MagicMock()
    response = MagicMock()
    response.structured = {field_spec.name: "John Smith"}
    router.generate = AsyncMock(return_value=response)

    template = _make_template(field_spec)
    extractor = FieldExtractor(router)
    retrieved = {field_spec.retrieval_key: [chunk]}

    results = await extractor.extract_all(template, retrieved, "fp123", None)
    result = results[field_spec.name]

    assert result.value == "John Smith"
    assert result.confidence > 0.0
    assert result.error_code is None
    # Supporting chunks default to all retrieved chunks when the model
    # doesn't volunteer any (the flat schema doesn't ask for them).
    assert chunk_id in result.supporting_chunk_ids


@pytest.mark.asyncio
async def test_date_field_skips_substring_check():
    """date-typed fields are routinely normalized by the LLM (e.g.,
    "April 28, 2025" → "2025-04-28"), so they bypass the substring gate."""
    chunk_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    chunk = _make_chunk(chunk_id, "Filed this 28th day of April, 2025.")
    field_spec = _make_field_spec(name="filing_date", field_type="date")

    router = MagicMock()
    response = MagicMock()
    response.structured = {field_spec.name: "2025-04-28"}
    router.generate = AsyncMock(return_value=response)

    template = _make_template(field_spec)
    extractor = FieldExtractor(router)
    retrieved = {field_spec.retrieval_key: [chunk]}

    results = await extractor.extract_all(template, retrieved, "fp123", None)
    result = results[field_spec.name]

    assert result.value == "2025-04-28"
    assert result.error_code is None


@pytest.mark.asyncio
async def test_string_field_token_overlap_passes():
    """String fields accept ≥60% token overlap when an exact substring is
    absent — covers cases like "Circuit Court of Cook County, Illinois — Law
    Division" assembled from a header and subdivision line."""
    chunk_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    chunk = _make_chunk(
        chunk_id,
        "IN THE CIRCUIT COURT OF COOK COUNTY, ILLINOIS\nLAW DIVISION",
    )
    field_spec = _make_field_spec(name="jurisdiction", field_type="string")

    router = MagicMock()
    response = MagicMock()
    response.structured = {
        field_spec.name: "Circuit Court of Cook County, Illinois — Law Division"
    }
    router.generate = AsyncMock(return_value=response)

    template = _make_template(field_spec)
    extractor = FieldExtractor(router)
    retrieved = {field_spec.retrieval_key: [chunk]}

    results = await extractor.extract_all(template, retrieved, "fp123", None)
    result = results[field_spec.name]

    assert result.value == "Circuit Court of Cook County, Illinois — Law Division"
    assert result.error_code is None


@pytest.mark.asyncio
async def test_list_party_returns_structured_objects():
    """list[party] preserves {name, role} structure (was being flattened to
    list[str] before, which dropped the role info downstream)."""
    chunk_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    chunk = _make_chunk(
        chunk_id,
        "JAMES HENDERSON, Plaintiff, v. SWIFT LOGISTICS LLC, Defendant.",
    )
    field_spec = _make_field_spec(name="parties", field_type="list[party]")

    router = MagicMock()
    response = MagicMock()
    response.structured = {
        field_spec.name: [
            {"name": "James Henderson", "role": "Plaintiff"},
            {"name": "Swift Logistics LLC", "role": "Defendant"},
        ]
    }
    router.generate = AsyncMock(return_value=response)

    template = _make_template(field_spec)
    extractor = FieldExtractor(router)
    retrieved = {field_spec.retrieval_key: [chunk]}

    results = await extractor.extract_all(template, retrieved, "fp123", None)
    result = results[field_spec.name]

    assert isinstance(result.value, list)
    assert len(result.value) == 2
    assert result.value[0] == {"name": "James Henderson", "role": "Plaintiff"}


@pytest.mark.asyncio
async def test_stringified_list_value_is_parsed():
    """Anthropic tool-use occasionally JSON-stringifies list values when the
    schema is complex. The extractor recovers by parsing the string."""
    chunk_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    chunk = _make_chunk(chunk_id, "Count I — Negligence.")
    field_spec = _make_field_spec(name="claims", field_type="list[string]")

    router = MagicMock()
    response = MagicMock()
    response.structured = {field_spec.name: '["Count I — Negligence"]'}
    router.generate = AsyncMock(return_value=response)

    template = _make_template(field_spec)
    extractor = FieldExtractor(router)
    retrieved = {field_spec.retrieval_key: [chunk]}

    results = await extractor.extract_all(template, retrieved, "fp123", None)
    result = results[field_spec.name]

    assert result.value == ["Count I — Negligence"]


@pytest.mark.asyncio
async def test_legacy_wrapper_envelope_still_accepted():
    """Backwards compat: if the model emits the old {value, ...} envelope,
    unwrap it instead of treating it as a literal dict value."""
    chunk_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    chunk = _make_chunk(chunk_id, "The defendant is John Smith in this matter.")
    field_spec = _make_field_spec()

    router = MagicMock()
    response = MagicMock()
    response.structured = {
        field_spec.name: {
            "value": "John Smith",
            "supporting_chunk_ids": [chunk_id],
            "confidence": 0.95,
        }
    }
    router.generate = AsyncMock(return_value=response)

    template = _make_template(field_spec)
    extractor = FieldExtractor(router)
    retrieved = {field_spec.retrieval_key: [chunk]}

    results = await extractor.extract_all(template, retrieved, "fp123", None)
    result = results[field_spec.name]

    assert result.value == "John Smith"
    assert result.supporting_chunk_ids == [chunk_id]
    assert result.confidence == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_no_chunks_skips_llm_call():
    field_spec = _make_field_spec()
    router = MagicMock()
    router.generate = AsyncMock()

    template = _make_template(field_spec)
    extractor = FieldExtractor(router)

    results = await extractor.extract_all(template, {}, "fp123", None)
    result = results[field_spec.name]

    assert result.value is None
    assert result.confidence == 0.0
    router.generate.assert_not_called()
