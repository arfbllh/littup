from __future__ import annotations
import asyncio
import structlog
from app.retrieval.types import RetrievedChunk
from app.llm.reranker_model import Reranker

logger = structlog.get_logger(__name__)


class RerankerWrapper:
    def __init__(self, reranker: Reranker) -> None:
        self._reranker = reranker

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []

        docs = [c.chunk.text for c in chunks]

        try:
            sorted_indices = await asyncio.wait_for(
                self._reranker.rerank(query, docs), timeout=2.0
            )
            reranked = [chunks[i] for i in sorted_indices]
            return reranked[:top_k]
        except asyncio.TimeoutError:
            logger.warning("reranker_timeout", degraded_mode=True, query=query)
            result = chunks[:top_k]
            for chunk in result:
                chunk.degraded_mode = True
            return result
