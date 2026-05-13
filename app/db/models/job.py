from datetime import datetime

from sqlalchemy import TIMESTAMP, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

TZ = TIMESTAMP(timezone=True)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = {"schema": "jobs"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    picked_up_at: Mapped[datetime | None] = mapped_column(TZ)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TZ)
    worker_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    dedup_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


class JobHistory(Base):
    __tablename__ = "job_history"
    __table_args__ = {"schema": "jobs"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    job_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
