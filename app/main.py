from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.core.errors import AppError
from app.core.middleware import RequestIDMiddleware, app_error_handler, unhandled_error_handler
from app.settings import settings


def _setup_logging() -> None:
    from app.core.logging import setup_logging

    setup_logging(level=settings.LOG_LEVEL, env=settings.ENV)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = structlog.get_logger(__name__)
    logger.info("startup", env=settings.ENV)
    yield
    logger.info("shutdown")


def create_app() -> FastAPI:
    _setup_logging()

    application = FastAPI(
        title="littup",
        description="Legal PDF → grounded first draft",
        version="0.1.0",
        lifespan=lifespan,
    )

    application.add_middleware(RequestIDMiddleware)

    application.add_exception_handler(AppError, app_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)

    from app.api.routes.admin import router as admin_router
    from app.api.routes.health import router as health_router

    application.include_router(health_router)
    application.include_router(admin_router)

    return application


app = create_app()
