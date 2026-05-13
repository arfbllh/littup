from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

TZ = TIMESTAMP(timezone=True)


class LLMRequest(Base):
    """Partitioned root table — partitioned by RANGE on created_at (monthly). DDL in migration."""

    __tablename__ = "llm_requests"
    __table_args__ = {"schema": "llm_log"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(Text)       # matches X-Request-ID
    tier: Mapped[str | None] = mapped_column(Text)           # extraction|generation|validation|vision|analysis
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_fingerprint: Mapped[str | None] = mapped_column(Text)
    cache_key: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    cache_hit: Mapped[bool | None] = mapped_column(Boolean)
    # created_at is part of composite PK required for declarative partition mapping
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, primary_key=True)


class LLMCache(Base):
    __tablename__ = "llm_cache"
    __table_args__ = {"schema": "llm_log"}

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    response: Mapped[dict | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(TZ)
