"""Context propagation for the currently-executing job.

Handlers run inside a per-job ContextVar scope so service code (e.g. the OCR
page loop) can poll the job's ``cancel_requested`` flag without threading
``job_id`` through every signature.
"""

from __future__ import annotations

from contextvars import ContextVar

current_job_id: ContextVar[str | None] = ContextVar("current_job_id", default=None)
