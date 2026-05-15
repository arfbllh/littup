"""Embedding provider interface."""
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
    _holder: dict = {"model": None, "model__future": None}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using the BGE large model."""
        loop = asyncio.get_running_loop()
        model = await self._get_model()

        def _encode():
            import torch
            with torch.inference_mode():
                return model.encode(
                    texts,
                    batch_size=settings.EMBEDDING_BATCH_SIZE,
                    normalize_embeddings=True,
                )

        embeddings = await loop.run_in_executor(None, _encode)
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
        """Lazy-load the sentence-transformers model (process-level singleton).

        Concurrent first callers all await the same shared load so we only
        download and instantiate the model once, not once per parallel query.
        """
        from app.llm.reranker_model import _shared_load

        async def _load():
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise RuntimeError("sentence_transformers not installed")

            loop = asyncio.get_running_loop()

            def _blocking():
                start = time.time()
                model = SentenceTransformer(
                    settings.EMBEDDING_MODEL, device=settings.TORCH_DEVICE
                )
                elapsed = time.time() - start
                logger.info(
                    "model_loaded",
                    model=settings.EMBEDDING_MODEL,
                    device=settings.TORCH_DEVICE,
                    elapsed_seconds=elapsed,
                )
                return model

            return await loop.run_in_executor(None, _blocking)

        return await _shared_load(cls._holder, "model", _load)


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
