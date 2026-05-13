import traceback

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.errors import AppError, RateLimitError
from app.core.ids import new_uuid7

logger = structlog.get_logger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or new_uuid7()

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")

    log = logger.bind(
        event="app_error",
        error_code=exc.code,
        status_code=exc.status_code,
        path=str(request.url.path),
    )

    if exc.status_code >= 500:
        log.error(exc.message)
    else:
        log.warning(exc.message)

    body: dict = {"error": {"code": exc.code, "message": exc.message, "request_id": request_id}}

    headers = {}
    if isinstance(exc, RateLimitError):
        headers["Retry-After"] = str(exc.retry_after)

    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")

    logger.error(
        "unhandled_exception",
        path=str(request.url.path),
        exc_type=type(exc).__name__,
        traceback=traceback.format_exc(),
    )

    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred.",
                "request_id": request_id,
            }
        },
    )
