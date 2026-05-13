"""
Worker process — run as: python -m app.jobs.worker

Polls the job queue and dispatches to registered handlers.
Per-kind asyncio.Semaphore bounds concurrency (NN-3).
Heartbeats every 10s while a job is running.
Graceful SIGTERM: finishes current job then exits.
"""

import asyncio
import contextlib
import signal

import structlog

from app.core.ids import new_uuid7
from app.core.logging import setup_logging
from app.db.session import async_session_factory
from app.jobs.kinds import JobKind, get_handler
from app.jobs.queue import JobQueue
from app.settings import settings

logger = structlog.get_logger(__name__)

_SHUTDOWN = False

# Per-kind concurrency limits (NN-3)
_SEMAPHORES: dict[str, asyncio.Semaphore] = {}

# Strong references to in-flight tasks so asyncio doesn't GC them mid-execution.
_TASKS: set[asyncio.Task] = set()


def _get_semaphore(kind: str) -> asyncio.Semaphore:
    if kind not in _SEMAPHORES:
        if kind == JobKind.OCR:
            n = settings.WORKER_CONCURRENCY_OCR
        elif kind == JobKind.EMBEDDING:
            n = settings.WORKER_CONCURRENCY_EMBEDDING
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

        try:
            handler = get_handler(kind)
            async with async_session_factory() as session:
                result = await handler(job.payload, session)
                q = JobQueue(session)
                await q.complete(job.id, result)
                await session.commit()
            logger.info("job_done", job_id=job.id, kind=kind)
        except Exception as exc:
            logger.error("job_handler_error", job_id=job.id, kind=kind, error=str(exc))
            try:
                async with async_session_factory() as session:
                    q = JobQueue(session)
                    await q.fail(job.id, str(exc), retryable=True)
                    await session.commit()
            except Exception:
                pass
        finally:
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
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 10.0)
        except Exception as exc:
            logger.error("poll_error", error=str(exc))
            await asyncio.sleep(5)


async def _reconcile_loop() -> None:
    from app.jobs.reconciler import Reconciler

    # Run once at startup to reclaim any jobs stuck from before restart (NN-1),
    # then repeat every 60 seconds.
    while not _SHUTDOWN:
        try:
            async with async_session_factory() as session:
                rec = Reconciler(session)
                await rec.reclaim_stuck_jobs()
                await rec.find_partial_documents()
                await session.commit()
        except Exception as exc:
            logger.error("reconcile_error", error=str(exc))
        await asyncio.sleep(60)


async def _main() -> None:
    global _SHUTDOWN

    setup_logging(level=settings.LOG_LEVEL, env=settings.ENV)
    worker_id = f"worker-{new_uuid7()[:8]}"

    def _handle_sigterm(*_):
        global _SHUTDOWN
        logger.info("sigterm_received", worker_id=worker_id)
        _SHUTDOWN = True

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    logger.info("worker_started", worker_id=worker_id)

    await asyncio.gather(
        _poll_loop(worker_id),
        _reconcile_loop(),
    )

    logger.info("worker_stopped", worker_id=worker_id)


if __name__ == "__main__":
    asyncio.run(_main())
