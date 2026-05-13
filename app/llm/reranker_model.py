"""Reranker provider interface. Real bge-reranker-base wiring lives in M6."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


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
