from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base

TZ = TIMESTAMP(timezone=True)


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    template_id: Mapped[str] = mapped_column(Text, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    document_ids: Mapped[list | None] = mapped_column(ARRAY(UUID(as_uuid=False)))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="generating")
    ai_output: Mapped[dict | None] = mapped_column(JSONB)
    final_output: Mapped[dict | None] = mapped_column(JSONB)
    model_used: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    groundedness_score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    generated_at: Mapped[datetime | None] = mapped_column(TZ)
    edited_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    extra_instructions: Mapped[str | None] = mapped_column(Text)

    sections: Mapped[list["Section"]] = relationship("Section", back_populates="draft", cascade="all, delete-orphan")
    edits: Mapped[list["Edit"]] = relationship("Edit", back_populates="draft")  # noqa: F821


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    draft_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ai_text: Mapped[str | None] = mapped_column(Text)
    final_text: Mapped[str | None] = mapped_column(Text)
    target_length_min: Mapped[int | None] = mapped_column(Integer)
    target_length_max: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    draft: Mapped["Draft"] = relationship("Draft", back_populates="sections")
    citations: Mapped[list["Citation"]] = relationship(
        "Citation", back_populates="section", cascade="all, delete-orphan"
    )


class Citation(Base):
    __tablename__ = "citations"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    section_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.sections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.chunks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    claim_span_start: Mapped[int | None] = mapped_column(Integer)
    claim_span_end: Mapped[int | None] = mapped_column(Integer)
    validation_status: Mapped[str] = mapped_column(Text, nullable=False, default="unchecked")
    validation_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    section: Mapped["Section"] = relationship("Section", back_populates="citations")
    chunk: Mapped["Chunk"] = relationship("Chunk", back_populates="citations")  # noqa: F821
