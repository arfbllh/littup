from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import Integer, case, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_embedder, get_llm_router, get_template_registry
from app.api.schemas.admin import (
    AdminTemplateRow,
    AdminTemplatesResponse,
    ExtractionResultRow,
    RuleExtractorRunRequest,
    RuleExtractorRunResponse,
    TemplateVersionRow,
    TemplateVersionsResponse,
)
from app.core.trace import current_trace_id
from app.db.models.llm_log import LLMRequest
from app.db.session import async_session_factory, direct_session_factory
from app.llm.router import LLMRouter
from app.db.session import get_session

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


async def _list_known_template_ids(session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(text("SELECT DISTINCT template_id FROM app.templates"))
    ).fetchall()
    return [row[0] for row in rows]


@router.post("/rule-extractor/run", response_model=RuleExtractorRunResponse)
async def run_rule_extractor(
    body: RuleExtractorRunRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    registry: Any = Depends(get_template_registry),
    llm_router: Any = Depends(get_llm_router),
    embedder: Any = Depends(get_embedder),
) -> RuleExtractorRunResponse:
    from app.edits.rule_extractor import RuleExtractor
    from app.settings import settings

    trace_id = current_trace_id()

    if body.template_id is not None:
        row = (
            await session.execute(
                text("SELECT 1 FROM app.templates WHERE template_id = :tid LIMIT 1"),
                {"tid": body.template_id},
            )
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "TEMPLATE_NOT_FOUND", "message": f"Template '{body.template_id}' not found."})
        template_ids = [body.template_id]
    else:
        template_ids = await _list_known_template_ids(session)

    deadline = time.monotonic() + settings.RULE_EXTRACTOR_ADMIN_MAX_DURATION_S
    results: list[ExtractionResultRow] = []
    partial = False

    for tid in template_ids:
        if time.monotonic() > deadline:
            partial = True
            break

        async with direct_session_factory() as lock_session:
            extractor = RuleExtractor(
                session=session,
                lock_session=lock_session,
                registry=registry,
                llm_router=llm_router,
                embedder=embedder,
            )
            result = await extractor.run(tid, trace_id=trace_id)

        if body.template_id is not None and result.skipped_reason == "locked":
            raise HTTPException(
                status_code=409,
                headers={"Retry-After": "60"},
                detail={"code": "RULE_EXTRACTOR_BUSY", "message": "Rule extractor is already running."},
            )

        results.append(
            ExtractionResultRow(
                template_id=result.template_id,
                skipped_reason=result.skipped_reason,
                edits_processed=result.edits_processed,
                groups_evaluated=result.groups_evaluated,
                groups_skipped_min_edits=result.groups_skipped_min_edits,
                new_rules=result.new_rules,
                new_version=result.new_version,
                prompt_fingerprint=result.prompt_fingerprint,
                trace_id=result.trace_id,
            )
        )

    await session.commit()
    return RuleExtractorRunResponse(results=results, partial=partial)


@router.get("/templates", response_model=AdminTemplatesResponse)
async def list_admin_templates(
    session: AsyncSession = Depends(get_session),
) -> AdminTemplatesResponse:
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (t.template_id)
                    t.template_id,
                    t.version,
                    t.prompt_fingerprint,
                    jsonb_array_length(t.appended_rules) AS rules_count,
                    es.last_run_at
                FROM app.templates t
                LEFT JOIN app.template_extractor_state es
                    ON es.template_id = t.template_id
                ORDER BY t.template_id, t.version DESC
                """
            )
        )
    ).fetchall()

    templates = [
        AdminTemplateRow(
            template_id=row[0],
            latest_version=row[1],
            prompt_fingerprint=row[2],
            appended_rules_count=row[3] or 0,
            last_run_at=row[4],
        )
        for row in rows
    ]
    return AdminTemplatesResponse(templates=templates)


@router.get("/templates/{template_id}/versions", response_model=TemplateVersionsResponse)
async def get_template_versions(
    template_id: str,
    session: AsyncSession = Depends(get_session),
) -> TemplateVersionsResponse:
    rows = (
        await session.execute(
            text(
                "SELECT version, prompt_fingerprint, appended_rules, system_prompt, created_at "
                "FROM app.templates WHERE template_id = :tid ORDER BY version ASC"
            ),
            {"tid": template_id},
        )
    ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail={"code": "TEMPLATE_NOT_FOUND", "message": f"Template '{template_id}' not found."})

    version_rows: list[TemplateVersionRow] = []
    prev_rules: list[str] = []

    for row in rows:
        version, fp, appended_rules, system_prompt, created_at = row
        appended_rules = appended_rules or []
        rules_added = list(set(appended_rules) - set(prev_rules))
        resolved = system_prompt
        if appended_rules:
            resolved = system_prompt + "\n\n" + "\n".join(appended_rules)
        version_rows.append(
            TemplateVersionRow(
                version=version,
                prompt_fingerprint=fp,
                appended_rules=appended_rules,
                rules_added_vs_previous=rules_added,
                resolved_system_prompt=resolved,
                created_at=created_at,
            )
        )
        prev_rules = appended_rules

    return TemplateVersionsResponse(template_id=template_id, versions=version_rows)
