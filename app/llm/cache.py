from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.llm_log import LLMCache
from app.llm.types import LLMResponse, Message, SamplingParams


def _canonicalize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if isinstance(m.content, str):
            content: Any = m.content
        else:
            content = [p.model_dump() for p in m.content]
        out.append({"role": m.role, "content": content})
    return out


def build_cache_key(
    model_id: str,
    messages: list[Message],
    schema: dict[str, Any] | None,
    sampling: SamplingParams,
) -> str:
    payload = {
        "model_id": model_id,
        "messages": _canonicalize_messages(messages),
        "schema": schema,
        "sampling": sampling.model_dump(),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ResponseCache:
    """Postgres-backed LLM response cache. Content-addressed (NN-7)."""

    def __init__(
        self,
        session_factory: async_sessionmaker | None,
        ttl_hours: int = 24,
        enabled: bool = True,
    ):
        self._sf = session_factory
        self.ttl_hours = ttl_hours
        self.enabled = enabled
        # Process-local fallback when no DB factory is provided (tests).
        self._mem: dict[str, tuple[datetime, LLMResponse]] = {}

    def build_key(
        self,
        model_id: str,
        messages: list[Message],
        schema: dict[str, Any] | None,
        sampling: SamplingParams,
    ) -> str:
        return build_cache_key(model_id, messages, schema, sampling)

    async def get(self, key: str) -> LLMResponse | None:
        if not self.enabled:
            return None
        if self._sf is None:
            row = self._mem.get(key)
            if not row:
                return None
            expires, resp = row
            if expires < datetime.now(timezone.utc):
                self._mem.pop(key, None)
                return None
            return resp.model_copy(update={"cached_hit": True})

        async with self._sf() as session:
            row = (
                await session.execute(select(LLMCache).where(LLMCache.cache_key == key))
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.expires_at and row.expires_at < datetime.now(timezone.utc):
                return None
            resp = LLMResponse.model_validate(row.response)
            resp.cached_hit = True
            return resp

    async def put(self, key: str, response: LLMResponse) -> None:
        if not self.enabled:
            return
        expires = datetime.now(timezone.utc) + timedelta(hours=self.ttl_hours)
        if self._sf is None:
            self._mem[key] = (expires, response.model_copy(update={"cached_hit": False}))
            return
        async with self._sf() as session:
            stmt = insert(LLMCache).values(
                cache_key=key,
                response=response.model_dump(mode="json"),
                model=response.model_used,
                expires_at=expires,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[LLMCache.cache_key],
                set_={
                    "response": stmt.excluded.response,
                    "model": stmt.excluded.model,
                    "expires_at": stmt.excluded.expires_at,
                },
            )
            await session.execute(stmt)
            await session.commit()
