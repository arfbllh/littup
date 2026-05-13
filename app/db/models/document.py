from datetime import datetime

from sqlalchemy import TIMESTAMP, BigInteger, Float, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base

TZ = TIMESTAMP(timezone=True)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    sha256: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)
    doc_type: Mapped[str | None] = mapped_column(Text)  # native | scan | mixed | unknown
    status: Mapped[str] = mapped_column(Text, nullable=False, default="uploaded")
    embedded_at: Mapped[datetime | None] = mapped_column(TZ)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    vlm_pages_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    last_accessed_at: Mapped[datetime | None] = mapped_column(TZ, server_default=func.now())

    pages: Mapped[list["Page"]] = relationship("Page", back_populates="document", cascade="all, delete-orphan")
    blocks: Mapped[list["Block"]] = relationship("Block", back_populates="document", cascade="all, delete-orphan")
    events: Mapped[list["DocumentEvent"]] = relationship(
        "DocumentEvent", back_populates="document", cascade="all, delete-orphan"
    )


class DocumentEvent(Base):
    __tablename__ = "document_events"
    __table_args__ = {"schema": "app"}

    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.documents.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    ts: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    document: Mapped["Document"] = relationship("Document", back_populates="events")


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    document: Mapped["Document"] = relationship("Document", back_populates="pages")
    spans: Mapped[list["Span"]] = relationship("Span", back_populates="page", cascade="all, delete-orphan")


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_block_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.blocks.id", ondelete="SET NULL")
    )
    block_type: Mapped[str] = mapped_column(Text, nullable=False)  # section|paragraph|table|figure|list|header|footer
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    bbox_x0: Mapped[float | None] = mapped_column(Float)
    bbox_y0: Mapped[float | None] = mapped_column(Float)
    bbox_x1: Mapped[float | None] = mapped_column(Float)
    bbox_y1: Mapped[float | None] = mapped_column(Float)
    text: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    reading_order: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    document: Mapped["Document"] = relationship("Document", back_populates="blocks")
    spans: Mapped[list["Span"]] = relationship("Span", back_populates="block", cascade="all, delete-orphan")


class Span(Base):
    __tablename__ = "spans"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    page_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    block_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.blocks.id", ondelete="SET NULL")
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    bbox_x0: Mapped[float | None] = mapped_column(Float)
    bbox_y0: Mapped[float | None] = mapped_column(Float)
    bbox_x1: Mapped[float | None] = mapped_column(Float)
    bbox_y1: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(Text)  # pdfplumber|paddleocr|vlm|docling
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    page: Mapped["Page"] = relationship("Page", back_populates="spans")
    block: Mapped["Block | None"] = relationship("Block", back_populates="spans")
