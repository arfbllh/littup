"""Shared bootstrap for all eval scripts.

build_context() wires the full app graph from scratch — no FastAPI lifespan needed.
mock_llm=True replaces every LLM tier and the embedder with in-memory stubs so
integration tests can verify script structure without running real models.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.llm.budget import BudgetTracker
from app.llm.config import CacheConfig, ProviderConfig, RouterConfig, TierConfig
from app.llm.embedder import StubEmbedder
from app.llm.providers.mock import MockProvider
from app.llm.reranker_model import StubReranker
from app.llm.router import LLMRouter
from app.retrieval.reranker import RerankerWrapper
from app.retrieval.retriever import HybridRetriever
from app.settings import settings

log = structlog.get_logger(__name__)

REPORTS_DIR = Path(__file__).parent / "reports"


# ── Lightweight in-memory cache / log for mock mode ──────────────────────────

class _NullCache:
    async def get(self, key: str): return None
    async def put(self, key, response, *, ttl_hours, model=""): pass


class _NullLog:
    def start(self): pass
    async def stop(self): pass
    def record(self, **kwargs): pass


class _NullQueue:
    """No-op job queue — used so EditService can be instantiated without a real queue."""
    async def enqueue(self, *args, **kwargs): pass


# ── EvalContext ───────────────────────────────────────────────────────────────

@dataclass
class EvalContext:
    session_factory: async_sessionmaker
    llm_router: LLMRouter
    retriever: HybridRetriever
    draft_engine: Any  # DraftEngine
    registry: Any  # TemplateRegistry
    embedder: Any
    edit_service_factory: Callable[[AsyncSession], Any]  # -> EditService
    rule_extractor_factory: Callable[[AsyncSession, AsyncSession], Any]  # -> RuleExtractor


# ── Mock router ───────────────────────────────────────────────────────────────

def _build_mock_router() -> LLMRouter:
    provider = MockProvider(name="mock", model="mock-1", default_text="ok")
    config = RouterConfig(
        default_locale="local",
        tiers={
            "extraction": TierConfig(providers=["mock"]),
            "generation": TierConfig(providers=["mock"]),
            "validation": TierConfig(providers=["mock"]),
            "vision": TierConfig(providers=["mock"]),
            "analysis": TierConfig(providers=["mock"]),
        },
        providers={"mock": ProviderConfig(type="mock", model="mock-1")},
        cache=CacheConfig(enabled=False),
    )
    return LLMRouter(
        config=config,
        providers={"mock": provider},
        cache=_NullCache(),  # type: ignore[arg-type]
        budget=BudgetTracker(hourly_usd=1_000_000.0),
        log_repo=_NullLog(),  # type: ignore[arg-type]
    )


# ── build_context ─────────────────────────────────────────────────────────────

async def build_context(
    *,
    db_url: str | None = None,
    mock_llm: bool = False,
) -> EvalContext:
    """Bootstrap full app graph for eval scripts.

    db_url: defaults to settings.DATABASE_URL; integration tests pass TEST_DATABASE_URL.
    mock_llm: wire stubs instead of real models (no GPU, no API keys needed).
    """
    from app.draft.engine import DraftEngine
    from app.draft.templates.registry import TemplateRegistry
    from app.edits.rule_extractor import RuleExtractor
    from app.edits.service import EditService

    resolved_url = db_url or settings.DATABASE_URL

    engine = create_async_engine(
        resolved_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        echo=False,
        connect_args={"statement_cache_size": 0},
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    if mock_llm:
        llm_router = _build_mock_router()
        embedder = StubEmbedder(dim=1024)
        reranker = RerankerWrapper(StubReranker())
    else:
        from app.llm.config import load_router_config
        from app.llm.deps import build_router
        from app.llm.embedder import BGEEmbedder
        from app.llm.reranker_model import BGEReranker

        cfg = load_router_config(settings.ROUTER_CONFIG_PATH)
        llm_router = build_router(cfg)
        try:
            await llm_router.budget.prime()
        except Exception as exc:
            log.warning("eval.budget_prime_failed", error=str(exc))

        embedder = BGEEmbedder()
        reranker = RerankerWrapper(BGEReranker())

    retriever = HybridRetriever(
        session_factory=session_factory,
        reranker=reranker,
        embedder=embedder,
    )

    registry = TemplateRegistry()
    registry.load_from_disk(settings.TEMPLATES_DIR)
    async with session_factory() as s:
        await registry.sync_to_db(s)
        await s.commit()

    draft_engine = DraftEngine(
        retriever=retriever,
        llm_router=llm_router,
        registry=registry,
        session_factory=session_factory,
        embedder=embedder,
    )

    null_queue = _NullQueue()

    def edit_service_factory(session: AsyncSession) -> EditService:
        return EditService(session, null_queue, registry)

    def rule_extractor_factory(session: AsyncSession, lock_session: AsyncSession) -> RuleExtractor:
        return RuleExtractor(
            session=session,
            lock_session=lock_session,
            registry=registry,
            llm_router=llm_router,
            embedder=embedder,
        )

    return EvalContext(
        session_factory=session_factory,
        llm_router=llm_router,
        retriever=retriever,
        draft_engine=draft_engine,
        registry=registry,
        embedder=embedder,
        edit_service_factory=edit_service_factory,
        rule_extractor_factory=rule_extractor_factory,
    )


# ── Report writer ─────────────────────────────────────────────────────────────

def write_report(name: str, data: dict, markdown: str) -> None:
    """Write eval/reports/{name}.json and eval/reports/{name}.md."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / f"{name}.json"
    md_path = REPORTS_DIR / f"{name}.md"
    json_path.write_text(json.dumps(data, indent=2, default=str))
    md_path.write_text(markdown)
    log.info("eval.report_written", name=name, json=str(json_path), md=str(md_path))
