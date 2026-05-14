"""Unit tests for RuleExtractor._is_similar."""
from __future__ import annotations

import pytest
import pytest_asyncio

from app.edits.rule_extractor import _is_similar


class _CountingEmbedder:
    """Records how many times embed() is called and returns supplied vectors."""

    def __init__(self, vecs: list[list[float]]) -> None:
        self._vecs = vecs
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return self._vecs[: len(texts)]


class _UniformEmbedder:
    """All texts get the same unit vector → cosine = 1."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0]] * len(texts)


class _OrthogonalEmbedder:
    """Rule gets [1,0], all existing get [0,1] → cosine = 0."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0]] + [[0.0, 1.0]] * (len(texts) - 1)


@pytest.mark.asyncio
async def test_empty_existing_returns_false_no_embedder():
    emb = _CountingEmbedder([])
    result = await _is_similar("some rule", [], embedder=emb, threshold=0.88)
    assert result is False
    assert emb.calls == 0


@pytest.mark.asyncio
async def test_casefold_equal_short_circuits():
    emb = _CountingEmbedder([])
    result = await _is_similar("Always use LLC.", ["always use llc."], embedder=emb, threshold=0.88)
    assert result is True
    assert emb.calls == 0


@pytest.mark.asyncio
async def test_uniform_embedder_above_threshold():
    result = await _is_similar("rule A", ["rule B"], embedder=_UniformEmbedder(), threshold=0.88)
    assert result is True


@pytest.mark.asyncio
async def test_orthogonal_embedder_below_threshold():
    result = await _is_similar("rule A", ["rule B"], embedder=_OrthogonalEmbedder(), threshold=0.88)
    assert result is False
