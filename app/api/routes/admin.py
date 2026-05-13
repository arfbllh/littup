from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import Integer, case, cast, func, select

from app.api.deps import get_llm_router
from app.db.models.llm_log import LLMRequest
from app.db.session import async_session_factory
from app.llm.router import LLMRouter

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/llm-stats")
async def llm_stats(llm_router: LLMRouter = Depends(get_llm_router)) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    by_tier: list[dict[str, Any]] = []
    by_provider: list[dict[str, Any]] = []
    totals = {
        "calls": 0,
        "cache_hits": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
    }

    try:
        async with async_session_factory() as session:
            tier_rows = (
                await session.execute(
                    select(
                        LLMRequest.tier,
                        func.count().label("calls"),
                        func.coalesce(func.sum(LLMRequest.tokens_in), 0),
                        func.coalesce(func.sum(LLMRequest.tokens_out), 0),
                        func.coalesce(func.sum(LLMRequest.cost_usd), 0),
                        func.coalesce(
                            func.sum(cast(case((LLMRequest.cache_hit.is_(True), 1), else_=0), Integer)),
                            0,
                        ),
                        func.percentile_cont(0.5).within_group(LLMRequest.latency_ms.asc()),
                        func.percentile_cont(0.95).within_group(LLMRequest.latency_ms.asc()),
                    )
                    .where(LLMRequest.created_at >= cutoff)
                    .group_by(LLMRequest.tier)
                )
            ).all()
            for tier, calls, ti, to, cost, hits, p50, p95 in tier_rows:
                by_tier.append(
                    {
                        "tier": tier,
                        "calls": int(calls),
                        "tokens_in": int(ti),
                        "tokens_out": int(to),
                        "cost_usd": float(cost),
                        "cache_hits": int(hits),
                        "p50_ms": float(p50) if p50 is not None else 0.0,
                        "p95_ms": float(p95) if p95 is not None else 0.0,
                    }
                )
                totals["calls"] += int(calls)
                totals["cache_hits"] += int(hits)
                totals["tokens_in"] += int(ti)
                totals["tokens_out"] += int(to)
                totals["cost_usd"] += float(cost)

            prov_rows = (
                await session.execute(
                    select(
                        LLMRequest.provider,
                        func.count().label("calls"),
                        func.coalesce(func.sum(LLMRequest.cost_usd), 0),
                        func.percentile_cont(0.5).within_group(LLMRequest.latency_ms.asc()),
                        func.percentile_cont(0.95).within_group(LLMRequest.latency_ms.asc()),
                    )
                    .where(LLMRequest.created_at >= cutoff)
                    .group_by(LLMRequest.provider)
                )
            ).all()
            for provider, calls, cost, p50, p95 in prov_rows:
                by_provider.append(
                    {
                        "provider": provider,
                        "calls": int(calls),
                        "cost_usd": float(cost),
                        "p50_ms": float(p50) if p50 is not None else 0.0,
                        "p95_ms": float(p95) if p95 is not None else 0.0,
                    }
                )
    except Exception as e:  # DB unavailable in some test paths
        totals["error"] = str(e)

    cache_hit_rate = (
        (totals["cache_hits"] / totals["calls"]) if totals["calls"] else 0.0
    )

    return {
        "window": "1h",
        "totals": {**totals, "cache_hit_rate": cache_hit_rate},
        "by_tier": by_tier,
        "by_provider": by_provider,
        "budget": {
            "hourly_usd": llm_router.budget.hourly_usd,
            "current_spend_usd": llm_router.budget.current_spend(),
            "remaining_usd": max(
                0.0, llm_router.budget.hourly_usd - llm_router.budget.current_spend()
            ),
        },
    }
