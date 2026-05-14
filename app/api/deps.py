from __future__ import annotations

from app.llm.deps import build_router, get_llm_router, reset_router_for_tests
from app.llm.embedder import BGEEmbedder, Embedder, OpenAIEmbedder, StubEmbedder
from app.settings import settings

__all__ = [
    "build_router",
    "get_llm_router",
    "reset_router_for_tests",
    "get_embedder",
    "reset_embedder_for_tests",
]

_embedder: Embedder | None = None


async def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        if settings.EMBEDDER_PROVIDER == "openai":
            _embedder = OpenAIEmbedder()
        elif settings.EMBEDDER_PROVIDER == "bge":
            _embedder = BGEEmbedder()
        else:
            _embedder = StubEmbedder()
    return _embedder


def reset_embedder_for_tests() -> None:
    global _embedder
    _embedder = None
