from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Reranker(Protocol):
    async def rerank(self, query: str, docs: list[str]) -> list[int]: ...


class StubReranker:
    """Returns original order. Real bge-reranker-base wiring in M6."""

    async def rerank(self, query: str, docs: list[str]) -> list[int]:
        return list(range(len(docs)))
