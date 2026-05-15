"""
Worker process — run as: python -m app.jobs.worker

Polls the job queue and dispatches to registered handlers.
Per-kind asyncio.Semaphore bounds concurrency.
Heartbeats every 10s while a job is running.

Signals:
- SIGINT (Ctrl+C) / SIGTERM: graceful — wake sleeping loops, cancel in-flight tasks,
  let already-running handlers finish, then exit.
- Second SIGINT: force-exit (os._exit(130)). Use when a handler is blocked in a
  thread executor (e.g. PaddleOCR predict()) that can't be cancelled.
"""

import asyncio
import contextlib
import os
import signal

import structlog

import app.jobs.handlers  # noqa: F401  — registers handlers into HANDLERS
from app.core.errors import CancelledIngest
from app.core.ids import new_uuid7
from app.core.logging import setup_logging
from app.db.session import async_session_factory
from app.jobs.context import current_job_id
from app.jobs.kinds import JobKind, get_handler
from app.jobs.queue import JobQueue
from app.settings import settings

logger = structlog.get_logger(__name__)

_SHUTDOWN = False
_SHUTDOWN_EVENT: asyncio.Event | None = None

# Per-kind concurrency limits
_SEMAPHORES: dict[str, asyncio.Semaphore] = {}

# Strong references to in-flight tasks so asyncio doesn't GC them mid-execution.
_TASKS: set[asyncio.Task] = set()


async def _sleep_or_shutdown(seconds: float) -> None:
    """Sleep up to ``seconds``; return immediately if shutdown is requested."""
    if _SHUTDOWN or _SHUTDOWN_EVENT is None:
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_SHUTDOWN_EVENT.wait(), timeout=seconds)


def _get_semaphore(kind: str) -> asyncio.Semaphore:
    if kind not in _SEMAPHORES:
        if kind == JobKind.OCR:
            n = settings.WORKER_CONCURRENCY_OCR
        elif kind == JobKind.EMBEDDING:
            n = settings.WORKER_CONCURRENCY_EMBEDDING
        elif kind == JobKind.DRAFT_GENERATION:
            n = settings.WORKER_DRAFT_CONCURRENCY
        elif kind == JobKind.FEW_SHOT_INDEX:
            n = settings.WORKER_CONCURRENCY_FEW_SHOT
        else:
            n = settings.WORKER_CONCURRENCY_DEFAULT
        _SEMAPHORES[kind] = asyncio.Semaphore(n)
    return _SEMAPHORES[kind]


async def _heartbeat_loop(job_id: str, worker_id: str, interval: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.sleep(interval)
            if stop_event.is_set():
                break
            async with async_session_factory() as session:
                q = JobQueue(session)
                await q.heartbeat(job_id, worker_id)
                await session.commit()
        except Exception as exc:
            logger.warning("heartbeat_error", job_id=job_id, error=str(exc))


async def _handle_job(job, worker_id: str) -> None:
    kind = job.kind
    sem = _get_semaphore(kind)

    async with sem:
        stop_event = asyncio.Event()
        hb_task = asyncio.create_task(
            _heartbeat_loop(job.id, worker_id, settings.WORKER_HEARTBEAT_INTERVAL, stop_event)
        )

        token = current_job_id.set(job.id)
        try:
            handler = get_handler(kind)
            async with async_session_factory() as session:
                result = await handler(job.payload, session)
                q = JobQueue(session)
                await q.complete(job.id, result)
                await session.commit()
            logger.info("job_done", job_id=job.id, kind=kind)
        except CancelledIngest as exc:
            logger.info("job_cancelled", job_id=job.id, kind=kind, reason=str(exc))
            try:
                async with async_session_factory() as session:
                    q = JobQueue(session)
                    await q.cancel(job.id)
                    await session.commit()
            except Exception:
                pass
        except Exception as exc:
            logger.error("job_handler_error", job_id=job.id, kind=kind, error=str(exc))
            retryable = getattr(exc, "retryable", True)
            try:
                async with async_session_factory() as session:
                    q = JobQueue(session)
                    await q.fail(job.id, str(exc), retryable=retryable)
                    await session.commit()
            except Exception:
                pass
        finally:
            current_job_id.reset(token)
            stop_event.set()
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task


async def _poll_loop(worker_id: str) -> None:
    all_kinds = [k.value for k in JobKind]
    backoff = 1.0

    logger.info("worker_polling", worker_id=worker_id)

    while not _SHUTDOWN:
        try:
            async with async_session_factory() as session:
                q = JobQueue(session)
                job = await q.claim_one(all_kinds, worker_id)
                await session.commit()

            if job is not None:
                backoff = 1.0
                task = asyncio.create_task(_handle_job(job, worker_id))
                _TASKS.add(task)
                task.add_done_callback(_TASKS.discard)
            else:
                await _sleep_or_shutdown(backoff)
                backoff = min(backoff * 1.5, 10.0)
        except Exception as exc:
            logger.error("poll_error", error=str(exc))
            await _sleep_or_shutdown(5)


async def _reconcile_loop() -> None:
    from app.jobs.reconciler import Reconciler

    # Run once at startup to reclaim any jobs stuck from before restart,
    # then repeat every 60 seconds.
    while not _SHUTDOWN:
        try:
            async with async_session_factory() as session:
                rec = Reconciler(session)
                await rec.reclaim_stuck_jobs()
                await rec.find_partial_documents()
                await rec.reconcile_unembedded_edits()
                await session.commit()
        except Exception as exc:
            logger.error("reconcile_error", error=str(exc))
        await _sleep_or_shutdown(60)


async def _main() -> None:
    global _SHUTDOWN, _SHUTDOWN_EVENT

    setup_logging(level=settings.LOG_LEVEL, env=settings.ENV)
    worker_id = f"worker-{new_uuid7()[:8]}"

    loop = asyncio.get_running_loop()
    _SHUTDOWN_EVENT = asyncio.Event()
    sigint_count = 0

    def _handle_signal():
        nonlocal sigint_count
        global _SHUTDOWN
        sigint_count += 1
        if sigint_count >= 2:
            logger.warning("force_exit", worker_id=worker_id, signals=sigint_count)
            os._exit(130)
        logger.info("sigterm_received", worker_id=worker_id)
        _SHUTDOWN = True
        if _SHUTDOWN_EVENT is not None:
            _SHUTDOWN_EVENT.set()
        for task in _TASKS:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    logger.info("worker_started", worker_id=worker_id)

    from app.api.deps import get_embedder, get_llm_router, get_template_registry
    from app.db.session import direct_session_factory as _direct_session_factory
    from app.jobs.scheduler import RuleExtractorScheduler

    registry = get_template_registry()
    llm_router = await get_llm_router()
    embedder = await get_embedder()

    # Warm embedder + reranker in the background so the first draft doesn't
    # pay the ~7 s + ~7 s cold-load cost (and the reranker doesn't fall into
    # degraded_mode when 8 retrieval queries hit during a cold load).
    async def _warm_models():
        try:
            from app.llm.embedder import BGEEmbedder
            from app.llm.reranker_model import BGEReranker
            warmups = []
            if isinstance(embedder, BGEEmbedder):
                warmups.append(BGEEmbedder._get_model())
            warmups.append(BGEReranker._get_model())
            await asyncio.gather(*warmups, return_exceptions=True)
            logger.info("models_warmed", worker_id=worker_id)
        except Exception as exc:
            logger.warning("model_warm_failed", error=str(exc))

    _TASKS.add(asyncio.create_task(_warm_models()))

    scheduler = RuleExtractorScheduler(
        session_factory=async_session_factory,
        lock_session_factory=_direct_session_factory,
        registry=registry,
        llm_router=llm_router,
        embedder=embedder,
    )
    scheduler.start()

    try:
        await asyncio.gather(
            _poll_loop(worker_id),
            _reconcile_loop(),
            return_exceptions=True,
        )
    finally:
        scheduler.stop(wait=False)

    logger.info("worker_stopped", worker_id=worker_id)


if __name__ == "__main__":
    asyncio.run(_main())
