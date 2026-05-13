from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_router
from app.db.session import get_session
from app.llm.router import LLMRouter
from app.settings import settings

router = APIRouter(prefix="/admin", tags=["admin"])


_STATS_SQL = text(
    """
    SELECT
        tier,
        provider,
        COUNT(*)::int AS calls,
        COALESCE(SUM(cost_usd), 0)::float AS total_cost,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_latency_ms,
        PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms,
        SUM(CASE WHEN cache_hit THEN 1 ELSE 0 END)::float
            / NULLIF(COUNT(*), 0) AS cache_hit_rate
    FROM llm_log.llm_requests
    WHERE created_at >= NOW() - INTERVAL '1 hour'
    GROUP BY tier, provider
    ORDER BY tier, provider
    """
)


@router.get("/llm-stats")
async def llm_stats(
    session: AsyncSession = Depends(get_session),
    llm_router: LLMRouter = Depends(get_llm_router),
) -> dict:
    rows = (await session.execute(_STATS_SQL)).mappings().all()

    by_group = [
        {
            "tier": r["tier"],
            "provider": r["provider"],
            "calls": r["calls"],
            "total_cost_usd": float(r["total_cost"] or 0.0),
            "p50_latency_ms": float(r["p50_latency_ms"] or 0),
            "p95_latency_ms": float(r["p95_latency_ms"] or 0),
            "cache_hit_rate": float(r["cache_hit_rate"] or 0.0),
        }
        for r in rows
    ]

    current_spend = await llm_router._budget.current_spend()  # noqa: SLF001
    remaining = await llm_router._budget.remaining()  # noqa: SLF001

    return {
        "window": "1h",
        "by_tier_provider": by_group,
        "totals": {
            "calls": sum(g["calls"] for g in by_group),
            "cost_usd": sum(g["total_cost_usd"] for g in by_group),
        },
        "budget": {
            "limit_usd": settings.LLM_HOURLY_BUDGET_USD,
            "current_spend_usd": current_spend,
            "remaining_usd": remaining,
        },
    }
