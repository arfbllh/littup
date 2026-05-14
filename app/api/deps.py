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
    "get_template_registry",
    "reset_template_registry_for_tests",
    "get_draft_engine",
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


from app.draft.templates.registry import TemplateRegistry  # noqa: E402

_template_registry: TemplateRegistry | None = None


def get_template_registry() -> TemplateRegistry:
    global _template_registry
    if _template_registry is None:
        _template_registry = TemplateRegistry()
    return _template_registry


def reset_template_registry_for_tests() -> None:
    global _template_registry
    _template_registry = None


_draft_engine = None


async def get_draft_engine():
    global _draft_engine
    if _draft_engine is None:
        from app.db.session import async_session_factory
        from app.draft.engine import DraftEngine

        router = await get_llm_router()
        retriever = await get_retriever()
        registry = get_template_registry()
        embedder = await get_embedder()
        _draft_engine = DraftEngine(
            retriever=retriever,
            llm_router=router,
            registry=registry,
            session_factory=async_session_factory,
            embedder=embedder,
        )
    return _draft_engine


def reset_draft_engine_for_tests() -> None:
    global _draft_engine
    _draft_engine = None


async def get_rule_extractor(
    session=None,
    registry=None,
    router=None,
    embedder=None,
):
    """Yield a RuleExtractor with both pgbouncer session and direct lock_session."""
    from app.db.session import direct_session_factory
    from app.edits.rule_extractor import RuleExtractor

    if registry is None:
        registry = get_template_registry()
    if router is None:
        router = await get_llm_router()
    if embedder is None:
        embedder = await get_embedder()

    async with direct_session_factory() as lock_session:
        yield RuleExtractor(
            session=session,
            lock_session=lock_session,
            registry=registry,
            llm_router=router,
            embedder=embedder,
        )
