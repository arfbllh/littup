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


    try:
        from app.api.deps import get_template_registry
        from app.db.session import async_session_factory

        registry = get_template_registry()
        registry.load_from_disk(settings.TEMPLATES_DIR)
        async with async_session_factory() as session:
            synced = await registry.sync_to_db(session)
            await session.commit()
        for entry in synced:
            logger.info(
                "templates.synced",
                template_id=entry["id"],
                version=entry["version"],
                fingerprint=entry["fingerprint"],
            )
    except Exception as exc:
        logger.error("templates.sync_failed", error=str(exc))

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

    # Register job handlers (import side-effect populates HANDLERS).
    import app.jobs.handlers  # noqa: F401
    from app.api.routes.admin import router as admin_router
    from app.api.routes.documents import router as documents_router
    from app.api.routes.drafts import router as drafts_router
    from app.api.routes.edits import router as edits_router
    from app.api.routes.health import router as health_router
    from app.api.routes.templates import router as templates_router

    application.include_router(health_router)
    application.include_router(admin_router)
    application.include_router(documents_router)
    application.include_router(drafts_router)
    application.include_router(edits_router)
    application.include_router(templates_router)

    return application


app = create_app()
