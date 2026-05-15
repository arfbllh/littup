from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import TIMESTAMP, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base

TZ = TIMESTAMP(timezone=True)


class Edit(Base):
    __tablename__ = "edits"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    draft_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(Text, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    field_or_section_name: Mapped[str] = mapped_column(Text, nullable=False)
    field_type: Mapped[str] = mapped_column(Text, nullable=False)  # 'field' | 'section'
    ai_value: Mapped[dict | None] = mapped_column(JSONB)
    user_value: Mapped[dict | None] = mapped_column(JSONB)
    diff: Mapped[dict | None] = mapped_column(JSONB)
    context: Mapped[dict | None] = mapped_column(JSONB)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    few_shot_indexed_at: Mapped[datetime | None] = mapped_column(TZ)  # NULL until indexed
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    draft: Mapped["Draft"] = relationship("Draft", back_populates="edits")  # noqa: F821
