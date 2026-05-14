from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class EditCreateRequest(BaseModel):
    final_output: dict[str, Any]  # validated against template at service layer


class EditMetricFieldRow(BaseModel):
    name: str
    edited_count: int
    edit_rate: float | None


class EditMetricSectionRow(BaseModel):
    name: str
    edited_count: int
    edit_rate: float | None


class EditMetricsResponse(BaseModel):
    template_id: str
    window_days: int
    drafts_count: int
    fields: list[EditMetricFieldRow]
    sections: list[EditMetricSectionRow]
