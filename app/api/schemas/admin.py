from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class RuleExtractorRunRequest(BaseModel):
    template_id: str | None = None


class ExtractionResultRow(BaseModel):
    template_id: str
    skipped_reason: Literal["locked", "no_template", "no_edits", None]
    edits_processed: int
    groups_evaluated: int
    groups_skipped_min_edits: int
    new_rules: list[str]
    new_version: int | None
    prompt_fingerprint: str
    trace_id: str | None


class RuleExtractorRunResponse(BaseModel):
    results: list[ExtractionResultRow]
    partial: bool


class AdminTemplateRow(BaseModel):
    template_id: str
    latest_version: int
    prompt_fingerprint: str
    appended_rules_count: int
    last_run_at: datetime | None


class AdminTemplatesResponse(BaseModel):
    templates: list[AdminTemplateRow]


class TemplateVersionRow(BaseModel):
    version: int
    prompt_fingerprint: str
    appended_rules: list[str]
    rules_added_vs_previous: list[str]
    resolved_system_prompt: str
    created_at: datetime


class TemplateVersionsResponse(BaseModel):
    template_id: str
    versions: list[TemplateVersionRow]
