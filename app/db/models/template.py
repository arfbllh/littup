from datetime import datetime

from sqlalchemy import TIMESTAMP, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

TZ = TIMESTAMP(timezone=True)


class TemplateVersion(Base):
    __tablename__ = "templates"
    __table_args__ = {"schema": "app"}

    template_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    yaml_body: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    appended_rules: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="'[]'::jsonb")
    prompt_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
