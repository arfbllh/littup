import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.retrieval.reranker import RerankerWrapper
from app.retrieval.types import RetrievedChunk


@pytest.mark.asyncio
async def test_reranker_timeout_degrades():
    async def slow_rerank(query, docs):
        await asyncio.sleep(5)
        return list(range(len(docs)))

    mock_reranker = MagicMock()
    mock_reranker.rerank = slow_rerank

    chunks = []
    for i in range(5):
        fake_chunk = SimpleNamespace(text=f"chunk text {i}", id=f"id-{i}")
        chunks.append(RetrievedChunk(chunk=fake_chunk, score=float(i)))

    wrapper = RerankerWrapper(mock_reranker)
    result = await wrapper.rerank("test query", chunks, top_k=3)

    assert len(result) == 3
    assert all(r.degraded_mode for r in result)
    assert result[0].chunk.text == "chunk text 0"


@pytest.mark.asyncio
async def test_reranker_reorders_correctly():
    async def fast_rerank(query, docs):
        return list(reversed(range(len(docs))))

    mock_reranker = MagicMock()
    mock_reranker.rerank = fast_rerank

    chunks = [
        RetrievedChunk(chunk=SimpleNamespace(text=f"text {i}", id=f"id-{i}"), score=float(i))
        for i in range(4)
    ]

    wrapper = RerankerWrapper(mock_reranker)
    result = await wrapper.rerank("query", chunks, top_k=3)

    assert len(result) == 3
    assert not any(r.degraded_mode for r in result)
    assert result[0].chunk.text == "text 3"


@pytest.mark.asyncio
async def test_reranker_empty_chunks():
    mock_reranker = MagicMock()

    wrapper = RerankerWrapper(mock_reranker)
    result = await wrapper.rerank("query", [], top_k=3)

    assert result == []
