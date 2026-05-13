from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models.llm_log import LLMCache
from app.llm.types import LLMResponse, Message, SamplingParams

if TYPE_CHECKING:
    pass


def build_key(
    *,
    model_id: str,
    messages: list[Message],
    schema: type[BaseModel] | None,
    sampling: SamplingParams,
) -> str:
    """NN-7: content-addressed sha256 of all prompt inputs."""
    payload = {
        "model_id": model_id,
        "messages": [m.model_dump() for m in messages],
        "schema": schema.model_json_schema() if schema else None,
        "sampling": sampling.model_dump(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ResponseCache:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def get(self, key: str) -> LLMResponse | None:
        async with self._session_factory() as session:
            now = datetime.now(UTC)
            result = await session.execute(
                select(LLMCache).where(
                    LLMCache.cache_key == key,
                    (LLMCache.expires_at.is_(None)) | (LLMCache.expires_at > now),
                )
            )
            row = result.scalar_one_or_none()
            if row and row.response:
                response = LLMResponse.model_validate(row.response)
                response.cache_hit = True
                return response
            return None

    async def put(self, key: str, response: LLMResponse, *, ttl_hours: int, model: str = "") -> None:
        async with self._session_factory() as session:
            expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
            stmt = (
                insert(LLMCache)
                .values(
                    cache_key=key,
                    response=response.model_dump(),
                    model=model,
                    expires_at=expires,
                )
                .on_conflict_do_update(
                    index_elements=["cache_key"],
                    set_={"response": response.model_dump(), "expires_at": expires},
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def evict_expired(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(LLMCache).where(LLMCache.expires_at < datetime.now(UTC))
            )
            await session.commit()
            return result.rowcount
