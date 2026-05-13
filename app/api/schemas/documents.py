"""Pydantic shapes for the documents API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    document_id: str
    status: str
    was_new: bool
    sha256: str


class DocumentStatus(BaseModel):
    document_id: str
    sha256: str
    filename: str
    mime_type: str | None
    size_bytes: int | None
    page_count: int | None
    status: str
    last_event_seq: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentSummary(BaseModel):
    document_id: str
    filename: str
    status: str
    page_count: int | None
    size_bytes: int | None
    created_at: datetime


class DocumentList(BaseModel):
    items: list[DocumentSummary]
    next_offset: int


class BlocksResponse(BaseModel):
    status: str | None
    blocks: list[dict[str, Any]] = Field(default_factory=list)


class EventOut(BaseModel):
    seq: int
    type: str
    payload: dict[str, Any]
    ts: datetime
