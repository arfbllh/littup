"""Reranker provider interface."""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Protocol, runtime_checkable

import structlog

from app.settings import settings

logger = structlog.get_logger(__name__)


async def _shared_load(
    holder: dict,
    key: str,
    loader: Callable[[], Awaitable],
):
    """Run ``loader`` exactly once across concurrent callers.

    ``holder`` is a dict that owns two keys: ``key`` (the loaded value) and
    ``f"{key}__future"`` (the in-flight Future). Awaiters use asyncio.shield
    so a cancelled caller doesn't cancel the underlying load.
    """
    if holder.get(key) is not None:
        return holder[key]

    fut_key = f"{key}__future"
    task_key = f"{key}__task"
    loop = asyncio.get_running_loop()
    if holder.get(fut_key) is None:
        holder[fut_key] = loop.create_future()

        async def _runner():
            fut = holder[fut_key]
            try:
                value = await loader()
                holder[key] = value
                if not fut.done():
                    fut.set_result(value)
            except Exception as exc:
                holder[fut_key] = None
                if not fut.done():
                    fut.set_exception(exc)

        # Hold a strong reference so the runner task isn't GC'd if all
        # awaiters are cancelled before it finishes.
        holder[task_key] = loop.create_task(_runner())

    return await asyncio.shield(holder[fut_key])


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
    _holder: dict = {"model": None, "model__future": None}

    async def rerank(self, query: str, docs: list[str]) -> list[int]:
        """Rerank documents by relevance to query."""
        model = await self._get_model()
        loop = asyncio.get_running_loop()

        def _predict():
            import torch
            pairs = [(query, doc) for doc in docs]
            with torch.inference_mode():
                return model.predict(pairs)

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
        """Lazy-load the CrossEncoder model (process-level singleton).

        Concurrent first callers all await the same shared load so we only
        download and instantiate the model once, not once per parallel query.
        """
        async def _load():
            try:
                from sentence_transformers import CrossEncoder
            except ImportError:
                raise RuntimeError("sentence_transformers not installed")

            loop = asyncio.get_running_loop()

            def _blocking():
                start = time.time()
                model = CrossEncoder(
                    settings.RERANKER_MODEL, device=settings.TORCH_DEVICE
                )
                elapsed = time.time() - start
                logger.info(
                    "model_loaded",
                    model=settings.RERANKER_MODEL,
                    device=settings.TORCH_DEVICE,
                    elapsed_seconds=elapsed,
                )
                return model

            return await loop.run_in_executor(None, _blocking)

        return await _shared_load(cls._holder, "model", _load)
