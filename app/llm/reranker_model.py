"""Reranker provider interface. Real bge-reranker-base wiring lives in M6."""
from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable

import structlog

from app.settings import settings

logger = structlog.get_logger(__name__)


@runtime_checkable
class Reranker(Protocol):
    name: str

    async def rerank(self, query: str, docs: list[str]) -> list[int]: ...

    async def health(self) -> bool: ...


class StubReranker(Reranker):
    name = "stub"

    async def rerank(self, query: str, docs: list[str]) -> list[int]:
        return list(range(len(docs)))

    async def health(self) -> bool:
        return True


class BGEReranker(Reranker):
    """BGE reranker using sentence-transformers CrossEncoder."""

    name = "bge"
    _model = None

    async def rerank(self, query: str, docs: list[str]) -> list[int]:
        """Rerank documents by relevance to query."""
        model = await self._get_model()
        loop = asyncio.get_running_loop()

        def _predict():
            pairs = [(query, doc) for doc in docs]
            scores = model.predict(pairs)
            return scores

        scores = await loop.run_in_executor(None, _predict)
        # Return indices sorted by descending score
        sorted_indices = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
        return sorted_indices

    async def health(self) -> bool:
        """Check if the model can be loaded."""
        try:
            await self._get_model()
            return True
        except Exception:
            return False

    @classmethod
    async def _get_model(cls):
        """Lazy-load the CrossEncoder model (process-level singleton)."""
        if cls._model is not None:
            return cls._model

        loop = asyncio.get_running_loop()

        def _load():
            try:
                from sentence_transformers import CrossEncoder
            except ImportError:
                raise RuntimeError("sentence_transformers not installed")

            start = time.time()
            model = CrossEncoder(settings.RERANKER_MODEL)
            elapsed = time.time() - start
            logger.info("model_loaded", model=settings.RERANKER_MODEL, elapsed_seconds=elapsed)
            return model

        cls._model = await loop.run_in_executor(None, _load)
        return cls._model
