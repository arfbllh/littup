"""Template listing route (M7)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.drafts import TemplateInfo
from app.db.session import get_session

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("", response_model=list[TemplateInfo])
async def list_templates(
    session: AsyncSession = Depends(get_session),
) -> list[TemplateInfo]:
    from app.api.deps import get_template_registry

    registry = get_template_registry()
    infos = await registry.list_templates(session)
    return [
        TemplateInfo(
            id=info.id,
            latest_version=info.latest_version,
            display_name=info.display_name,
            description=info.description,
            fingerprint=info.fingerprint,
        )
        for info in infos
    ]
