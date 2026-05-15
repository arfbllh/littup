from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

EXTRA_INSTRUCTIONS_MAX = 2000


class DraftCreateRequest(BaseModel):
    template_id: str
    document_ids: list[UUID]
    extra_instructions: str | None = Field(default=None, max_length=EXTRA_INSTRUCTIONS_MAX)

    @field_validator("extra_instructions", mode="before")
    @classmethod
    def _strip_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


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
    groundedness: float | None = None


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
    groundedness_score: float | None = None
    edit_count: int = 0
    error: dict[str, Any] | None
    document_ids: list[str] = []
    extra_instructions: str | None = None


class DraftSummary(BaseModel):
    draft_id: str
    template_id: str
    template_version: int
    status: str
    document_count: int
    model_used: str | None
    cost_usd: float | None
    groundedness_score: float | None
    edit_count: int
    error_code: str | None
    generated_at: datetime | None
    created_at: datetime
    has_extra_instructions: bool = False


class DraftListResponse(BaseModel):
    items: list[DraftSummary]


class TemplateInfo(BaseModel):
    id: str
    latest_version: int
    display_name: str
    description: str
    fingerprint: str
