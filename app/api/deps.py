from __future__ import annotations

import asyncio

import structlog

from app.llm.config import ProviderConfig, RouterConfig
from app.llm.providers.base import LLMProvider
from app.llm.router import LLMRouter
from app.settings import settings

log = structlog.get_logger(__name__)

_router: LLMRouter | None = None
_router_lock = asyncio.Lock()


async def get_llm_router() -> LLMRouter:
    global _router
    if _router is not None:
        return _router
    async with _router_lock:
        if _router is not None:
            return _router
        _router = await _build_router()
    return _router


async def shutdown_llm_router() -> None:
    global _router
    if _router is None:
        return
    await _router._log_repo.stop()  # noqa: SLF001
    _router = None


async def _build_router() -> LLMRouter:
    from app.db.session import async_session_factory
    from app.llm.budget import BudgetTracker
    from app.llm.cache import ResponseCache
    from app.llm.config import load_router_config
    from app.llm.log_repo import LLMLogRepo

    config = load_router_config(settings.ROUTER_CONFIG_PATH)
    providers = _build_providers(config)

    cache = ResponseCache(async_session_factory)
    budget = BudgetTracker(hourly_limit_usd=settings.LLM_HOURLY_BUDGET_USD)
    log_repo = LLMLogRepo(async_session_factory)
    log_repo.start()

    try:
        async with async_session_factory() as session:
            await budget.reload_from_db(session)
    except Exception as exc:
        log.warning("budget.reload_skipped", error=str(exc))

    return LLMRouter(
        config=config,
        providers=providers,
        cache=cache,
        budget=budget,
        log_repo=log_repo,
    )


def _build_providers(config: RouterConfig) -> dict[str, LLMProvider]:

    providers: dict[str, LLMProvider] = {}
    for name, pconf in config.providers.items():
        providers[name] = _build_provider(name, pconf)
    return providers


def _build_provider(name: str, pconf: ProviderConfig) -> LLMProvider:
    from app.llm.providers.anthropic import AnthropicProvider
    from app.llm.providers.gemini import GeminiProvider
    from app.llm.providers.openai import OpenAIProvider
    from app.llm.providers.vllm import VLLMProvider

    if pconf.type == "vllm":
        return VLLMProvider(
            name=name,
            base_url=pconf.base_url or settings.VLLM_BASE_URL,
            model=pconf.model,
            timeout_s=pconf.timeout_s,
        )
    if pconf.type == "anthropic":
        return AnthropicProvider(name=name, model=pconf.model, api_key=pconf.api_key)
    if pconf.type == "openai":
        return OpenAIProvider(name=name, model=pconf.model, api_key=pconf.api_key)
    if pconf.type == "gemini":
        return GeminiProvider(name=name, model=pconf.model, api_key=pconf.api_key)
    raise ValueError(f"Unknown provider type: {pconf.type}")
