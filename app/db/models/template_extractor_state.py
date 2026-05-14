from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class TemplateExtractorState(Base):
    __tablename__ = "template_extractor_state"
    __table_args__ = {"schema": "app"}

    template_id: Mapped[str] = mapped_column(String, primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_edit_id: Mapped[str | None] = mapped_column(String, nullable=True)
    edits_processed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rules_added: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
