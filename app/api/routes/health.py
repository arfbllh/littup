import structlog
from fastapi import APIRouter, Response
from sqlalchemy import text

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response):
    try:
        from app.db.session import async_session_factory

        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        logger.warning("readyz_db_check_failed", error=str(exc))
        response.status_code = 503
        return {"status": "unavailable", "detail": "database unreachable"}
