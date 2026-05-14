"""Tests for SectionGenerator overrun truncation."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.draft.generator import SectionGenerator, _truncate_at_sentence


def test_truncate_at_sentence_basic():
    # 30 words in 3 sentences of 10 words each
    sentence = "This is a sentence with exactly ten words in it."
    text = f"{sentence} {sentence} {sentence}"
    words = text.split()
    assert len(words) >= 25

    truncated = _truncate_at_sentence(text, 15)
    assert len(truncated.split()) <= 15


def test_truncate_at_sentence_short_text_unchanged():
    text = "Short text."
    result = _truncate_at_sentence(text, 100)
    assert result == text


def test_truncate_at_sentence_ends_at_sentence_boundary():
    text = "First sentence ends here. Second sentence ends here. Third sentence ends here."
    truncated = _truncate_at_sentence(text, 8)
    # Should end at a sentence boundary
    assert truncated.endswith(".")


@pytest.mark.asyncio
async def test_section_overrun_triggers_truncation():
    from app.draft.templates.schema import SectionSpec

    section_spec = SectionSpec(
        name="test_section",
        description="A test section",
        retrieval_key="test",
        target_length_min=5,
        target_length_max=10,
    )

    # Generate text well above 1.5x max (>15 words)
    long_text = " ".join(["word"] * 30)

    router = MagicMock()
    response = MagicMock()
    response.text = long_text
    router.generate = AsyncMock(return_value=response)

    template = MagicMock()
    template.system_prompt = "You are helpful."
    template.appended_rules = []

    generator = SectionGenerator(router)
    section_draft, citations = await generator._generate_section(
        section_spec, template, {}, {}, set(), None
    )

    # With max=10 and 1.5x threshold = 15, and text has 30 words → must truncate
    assert section_draft.word_count <= 10 * 1.5
