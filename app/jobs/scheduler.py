"""APScheduler-based periodic rule extraction (M10).

Runs only in the worker process. Uses AsyncIOScheduler from APScheduler 3.x.
Advisory lock (via direct connection) prevents duplicate runs across workers.
"""
from __future__ import annotations

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.ids import new_uuid7
from app.edits.rule_extractor import RuleExtractor

logger = structlog.get_logger(__name__)


async def _list_known_template_ids(session) -> list[str]:
    from sqlalchemy import text

    rows = (
        await session.execute(text("SELECT DISTINCT template_id FROM app.templates"))
    ).fetchall()
    return [row[0] for row in rows]


class RuleExtractorScheduler:
    def __init__(
        self,
        session_factory,
        lock_session_factory,
        registry,
        llm_router,
        embedder,
        settings_obj=None,
    ) -> None:
        from app.settings import settings as _settings

        self._session_factory = session_factory
        self._lock_session_factory = lock_session_factory
        self._registry = registry
        self._llm_router = llm_router
        self._embedder = embedder
        self._settings = settings_obj or _settings

        interval_hours = self._settings.RULE_EXTRACTOR_INTERVAL_HOURS
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self._tick,
            IntervalTrigger(hours=interval_hours),
            id="rule_extractor.tick",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

    def start(self) -> None:
        self._scheduler.start()
        logger.info(
            "rule_extractor.scheduler_started",
            interval_hours=self._settings.RULE_EXTRACTOR_INTERVAL_HOURS,
        )

    def stop(self, wait: bool = True) -> None:
        self._scheduler.shutdown(wait=wait)
        logger.info("rule_extractor.scheduler_stopped")

    async def _tick(self) -> None:
        import structlog.contextvars as sv

        trace_id = new_uuid7()
        sv.bind_contextvars(request_id=trace_id, source="scheduler")

        try:
            async with (
                self._session_factory() as session,
                self._lock_session_factory() as lock_session,
            ):
                template_ids = await _list_known_template_ids(session)
                extractor = RuleExtractor(
                    session=session,
                    lock_session=lock_session,
                    registry=self._registry,
                    llm_router=self._llm_router,
                    embedder=self._embedder,
                    settings_obj=self._settings,
                )
                for tid in template_ids:
                    result = await extractor.run(tid, trace_id=trace_id)
                    logger.info(
                        "rule_extractor.tick_result",
                        template_id=tid,
                        new_rules=len(result.new_rules),
                        skipped_reason=result.skipped_reason,
                        trace_id=trace_id,
                    )
                await session.commit()
        except Exception as exc:
            logger.error("rule_extractor.tick_error", error=str(exc), trace_id=trace_id)
        finally:
            sv.clear_contextvars()
