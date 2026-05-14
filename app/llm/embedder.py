"""Embedding provider interface. Real bge-large wiring lives in M6."""
from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable

import structlog

from app.settings import settings

logger = structlog.get_logger(__name__)


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def health(self) -> bool: ...


class StubEmbedder(Embedder):
    """Deterministic placeholder. Returns zero vectors of the configured dim."""

    name = "stub"

    def __init__(self, dim: int = 1024):
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]

    async def health(self) -> bool:
        return True


class BGEEmbedder(Embedder):
    """BGE large embedder using sentence-transformers."""

    name = "bge"
    dim = 1024
    _model = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using the BGE large model."""
        loop = asyncio.get_running_loop()
        model = await self._get_model()
        embeddings = await loop.run_in_executor(
            None,
            lambda: model.encode(
                texts,
                batch_size=settings.EMBEDDING_BATCH_SIZE,
                normalize_embeddings=True,
            ),
        )
        return embeddings.tolist()

    async def health(self) -> bool:
        """Check if the model can be loaded."""
        try:
            await self._get_model()
            return True
        except Exception:
            return False

    @classmethod
    async def _get_model(cls):
        """Lazy-load the sentence-transformers model (process-level singleton)."""
        if cls._model is not None:
            return cls._model

        loop = asyncio.get_running_loop()

        def _load():
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise RuntimeError("sentence_transformers not installed")

            start = time.time()
            model = SentenceTransformer(settings.EMBEDDING_MODEL)
            elapsed = time.time() - start
            logger.info("model_loaded", model=settings.EMBEDDING_MODEL, elapsed_seconds=elapsed)
            return model

        cls._model = await loop.run_in_executor(None, _load)
        return cls._model


class OpenAIEmbedder(Embedder):
    """OpenAI embeddings using text-embedding-3-small."""

    name = "openai"
    dim = 1536
    _client = None
    _model = "text-embedding-3-small"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using OpenAI API."""
        client = await self._get_client()
        response = await client.embeddings.create(model=self._model, input=texts)
        return [e.embedding for e in response.data]

    async def health(self) -> bool:
        """Check if the API is reachable."""
        try:
            client = await self._get_client()
            await client.embeddings.create(model=self._model, input=["health check"])
            return True
        except Exception:
            return False

    @classmethod
    async def _get_client(cls):
        """Lazy-init the AsyncOpenAI client (process-level singleton)."""
        if cls._client is not None:
            return cls._client

        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise RuntimeError("openai not installed")

        cls._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        return cls._client
