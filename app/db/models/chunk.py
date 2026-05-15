from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base

TZ = TIMESTAMP(timezone=True)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("app.documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # text_tsv is a regular column (not GENERATED ALWAYS) so we can use the 'legal_en' config.
    # Populated by a trigger defined in the migration.
    text_tsv: Mapped[str | None] = mapped_column(Text)  # actual DDL type is TSVECTOR
    embedding: Mapped[bytes | None] = mapped_column()   # actual DDL type is VECTOR(1024)
    section_path: Mapped[list | None] = mapped_column(ARRAY(Text))
    chunk_type: Mapped[str | None] = mapped_column(Text)  # paragraph|table|table_rows|list|header_region
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int | None] = mapped_column(Integer)
    block_ids: Mapped[list | None] = mapped_column(ARRAY(Text))
    entities: Mapped[list | None] = mapped_column(ARRAY(Text))  # proper nouns / statute citations
    prompt_fingerprint: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    citations: Mapped[list["Citation"]] = relationship("Citation", back_populates="chunk")  # noqa: F821
