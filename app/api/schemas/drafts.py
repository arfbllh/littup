from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class DraftCreateRequest(BaseModel):
    template_id: str
    document_ids: list[UUID]


class DraftCreateResponse(BaseModel):
    draft_id: str
    status: str


class CitationView(BaseModel):
    chunk_id: str
    claim_span_start: int | None
    claim_span_end: int | None
    validation_status: str
    validation_reason: str | None


class SectionView(BaseModel):
    name: str
    text: str | None
    target_length_min: int | None
    target_length_max: int | None
    citations: list[CitationView]


class DraftResponse(BaseModel):
    draft_id: str
    template_id: str
    template_version: int
    prompt_fingerprint: str
    status: str
    fields: dict[str, Any] | None
    sections: list[SectionView]
    validators: list[dict[str, Any]] | None
    generated_at: datetime | None
    model_used: str | None
    cost_usd: float | None
    error: dict[str, Any] | None


class TemplateInfo(BaseModel):
    id: str
    latest_version: int
    display_name: str
    description: str
    fingerprint: str
