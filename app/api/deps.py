from __future__ import annotations

from app.llm.deps import build_router, get_llm_router, reset_router_for_tests
from app.llm.embedder import BGEEmbedder, Embedder, OpenAIEmbedder, StubEmbedder
from app.llm.reranker_model import BGEReranker
from app.retrieval.reranker import RerankerWrapper
from app.retrieval.retriever import HybridRetriever
from app.settings import settings

__all__ = [
    "build_router",
    "get_llm_router",
    "reset_router_for_tests",
    "get_embedder",
    "reset_embedder_for_tests",
    "get_retriever",
    "reset_retriever_for_tests",
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


_retriever: HybridRetriever | None = None


async def get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        from app.db.session import async_session_factory
        embedder = await get_embedder()
        _retriever = HybridRetriever(
            session_factory=async_session_factory,
            reranker=RerankerWrapper(BGEReranker()),
            embedder=embedder,
        )
    return _retriever


def reset_retriever_for_tests() -> None:
    global _retriever
    _retriever = None
