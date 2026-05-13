"""Embedding provider interface. Real bge-large wiring lives in M6."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


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
