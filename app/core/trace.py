from __future__ import annotations


def current_trace_id() -> str | None:
    try:
        import structlog.contextvars as sv
        return sv.get_contextvars().get("request_id")
    except Exception:
        return None
