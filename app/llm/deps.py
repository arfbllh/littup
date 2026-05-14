"""LLM router singleton — usable from workers and API layers alike.

This module is the authoritative home for the shared LLMRouter instance.
Importing from ``app.api.*`` is intentionally avoided so that background
workers can obtain the router without pulling in HTTP-layer dependencies.
"""

from __future__ import annotations

import asyncio

import structlog

from app.db.session import async_session_factory
from app.llm.budget import BudgetTracker
from app.llm.cache import ResponseCache
from app.llm.config import RouterConfig, load_router_config
from app.llm.log_repo import LLMLogRepo
from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.base import LLMProvider
from app.llm.providers.gemini import GeminiProvider
from app.llm.providers.openai import OpenAIProvider
from app.llm.providers.vllm import VLLMProvider
from app.llm.router import LLMRouter
from app.settings import settings

log = structlog.get_logger(__name__)

_router: LLMRouter | None = None
_lock = asyncio.Lock()


def _build_provider(name: str, cfg) -> LLMProvider:
    if cfg.type == "vllm":
        return VLLMProvider(
            name,
            base_url=cfg.base_url or settings.VLLM_BASE_URL,
            model=cfg.model,
            timeout_s=cfg.timeout_s,
            input_cost_per_1k=cfg.input_cost_per_1k,
            output_cost_per_1k=cfg.output_cost_per_1k,
        )
    if cfg.type == "anthropic":
        return AnthropicProvider(
            name,
            model=cfg.model,
            api_key_env=cfg.api_key_env or "ANTHROPIC_API_KEY",
            timeout_s=cfg.timeout_s,
            input_cost_per_1k=cfg.input_cost_per_1k,
            output_cost_per_1k=cfg.output_cost_per_1k,
        )
    if cfg.type == "openai":
        return OpenAIProvider(
            name,
            model=cfg.model,
            api_key_env=cfg.api_key_env or "OPENAI_API_KEY",
            timeout_s=cfg.timeout_s,
            input_cost_per_1k=cfg.input_cost_per_1k,
            output_cost_per_1k=cfg.output_cost_per_1k,
        )
    if cfg.type == "gemini":
        return GeminiProvider(
            name,
            model=cfg.model,
            api_key_env=cfg.api_key_env or "GEMINI_API_KEY",
            timeout_s=cfg.timeout_s,
            input_cost_per_1k=cfg.input_cost_per_1k,
            output_cost_per_1k=cfg.output_cost_per_1k,
        )
    raise ValueError(f"Unknown provider type {cfg.type!r} for {name!r}")


def build_router(cfg: RouterConfig) -> LLMRouter:
    providers: dict[str, LLMProvider] = {}
    for name, pcfg in cfg.providers.items():
        try:
            providers[name] = _build_provider(name, pcfg)
        except Exception as e:
            log.warning("llm.provider.build_failed", provider=name, error=str(e))
    cache = ResponseCache(
        session_factory=async_session_factory,
        ttl_hours=cfg.cache.ttl_hours,
        enabled=cfg.cache.enabled,
    )
    budget = BudgetTracker(
        hourly_usd=settings.LLM_HOURLY_BUDGET_USD or cfg.budget.hourly_usd,
        session_factory=async_session_factory,
    )
    log_repo = LLMLogRepo(session_factory=async_session_factory)
    return LLMRouter(cfg, providers, cache, budget, log_repo)


async def get_llm_router() -> LLMRouter:
    global _router
    if _router is not None:
        return _router
    async with _lock:
        if _router is None:
            cfg = load_router_config(settings.ROUTER_CONFIG_PATH)
            r = build_router(cfg)
            try:
                await r.budget.prime()
            except Exception as e:
                log.warning("budget.prime_failed", error=str(e))
            _router = r
    return _router


def reset_router_for_tests() -> None:
    global _router
    _router = None
