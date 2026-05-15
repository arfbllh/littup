"""Job handler for DRAFT_GENERATION jobs."""
from __future__ import annotations

import asyncio

import structlog
import structlog.contextvars

from app.core.errors import CancelledIngest, DraftError
from app.db.session import async_session_factory
from app.jobs.context import current_job_id
from app.jobs.kinds import HANDLERS, JobKind
from app.jobs.queue import JobQueue

log = structlog.get_logger(__name__)

# Poll interval for the cancel watcher. Short enough for responsive deletes,
# long enough to avoid hammering the jobs table during long generations.
CANCEL_POLL_INTERVAL_S = 2.0


async def _cancel_watcher(job_id: str, gen_task: asyncio.Task) -> None:
    """Poll cancel_requested; cancel the generation task when it flips."""
    try:
        while not gen_task.done():
            await asyncio.sleep(CANCEL_POLL_INTERVAL_S)
            if gen_task.done():
                return
            try:
                async with async_session_factory() as probe:
                    q = JobQueue(probe)
                    flagged = await q.is_cancel_requested(job_id)
            except Exception as exc:
                log.warning("draft_cancel_probe_error", job_id=job_id, error=str(exc))
                continue
            if flagged:
                log.info("draft_cancel_detected", job_id=job_id)
                gen_task.cancel()
                return
    except asyncio.CancelledError:
        return


async def handle_draft_generation(payload: dict, session) -> dict:
    draft_id = payload["draft_id"]
    template_id = payload["template_id"]
    document_ids = payload["document_ids"]
    trace_id = payload.get("trace_id")
    extra_instructions = payload.get("extra_instructions")
    job_id = current_job_id.get()

    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    log.info(
        "draft_generation.started",
        draft_id=draft_id,
        template_id=template_id,
        trace_id=trace_id,
        job_id=job_id,
    )

    try:
        from app.api.deps import get_draft_engine

        engine = await get_draft_engine()
        gen_task = asyncio.create_task(
            engine.generate(
                draft_id=draft_id,
                template_id=template_id,
                document_ids=document_ids,
                trace_id=trace_id,
                extra_instructions=extra_instructions,
            )
        )
        watcher = (
            asyncio.create_task(_cancel_watcher(job_id, gen_task))
            if job_id
            else None
        )
        try:
            await gen_task
        except asyncio.CancelledError as exc:
            log.info("draft_generation.cancelled", draft_id=draft_id, job_id=job_id)
            raise CancelledIngest(
                f"Draft {draft_id} cancelled by user"
            ) from exc
        finally:
            if watcher is not None:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
    except (CancelledIngest, DraftError):
        raise
    except Exception as exc:
        log.error(
            "draft_generation.unexpected_error",
            draft_id=draft_id,
            error=str(exc),
            exc_info=True,
        )
        raise DraftError(
            str(exc), code="DRAFT_GENERATION_UNEXPECTED", retryable=False
        ) from exc

    log.info("draft_generation.completed", draft_id=draft_id)
    return {"draft_id": draft_id, "status": "ready"}


HANDLERS[JobKind.DRAFT_GENERATION] = handle_draft_generation
