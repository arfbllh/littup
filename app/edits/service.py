"""EditService — save operator edits and compute per-field metrics."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.edits import (
    EditMetricFieldRow,
    EditMetricSectionRow,
    EditMetricsResponse,
)
from app.core.errors import EditError, NotFoundError
from app.core.ids import new_uuid7
from app.db.models.draft import Citation, Draft, Section
from app.db.models.edit import Edit
from app.edits.diff import compute_diff
from app.jobs.kinds import JobKind
from app.settings import settings

logger = structlog.get_logger(__name__)


@dataclass
class SavedEditResult:
    edit_ids: list[str]
    skipped_reason: str | None = None


class EditService:
    def __init__(self, session: AsyncSession, queue, registry) -> None:
        self._session = session
        self._queue = queue
        self._registry = registry

    async def save_edit(
        self,
        draft_id: str,
        user_output: dict,
        *,
        trace_id: str | None,
    ) -> SavedEditResult:
        """Apply an operator edit to a draft.

        Writes Edit rows, updates draft.final_output/status/edited_at, and enqueues
        FEW_SHOT_INDEX jobs — all within the caller's transaction. Caller commits.
        """
        # SELECT FOR UPDATE serializes concurrent edits on the same draft
        result = await self._session.execute(
            select(Draft).where(Draft.id == draft_id).with_for_update()
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            raise NotFoundError(f"Draft {draft_id} not found", code="DRAFT_NOT_FOUND")
        if draft.status not in ("ready", "edited"):
            raise EditError(
                f"Draft {draft_id} is not ready for editing (status={draft.status})",
                code="EDIT_DRAFT_NOT_READY",
                status_code=409,
                retryable=False,
            )

        template = await self._registry.get_by_version(
            draft.template_id, draft.template_version, self._session
        )

        # Validate user_output keys against template schema
        valid_field_names = {f.name for f in template.extraction_schema}
        valid_section_names = {s.name for s in template.sections}
        for key in (user_output.get("fields") or {}):
            if key not in valid_field_names:
                raise EditError(
                    f"Unknown field '{key}' for template '{template.id}'",
                    code="EDIT_PAYLOAD_INVALID",
                    status_code=400,
                    retryable=False,
                )
        for sec in (user_output.get("sections") or []):
            sec_name = sec.get("name") if isinstance(sec, dict) else None
            if sec_name not in valid_section_names:
                raise EditError(
                    f"Unknown section '{sec_name}' for template '{template.id}'",
                    code="EDIT_PAYLOAD_INVALID",
                    status_code=400,
                    retryable=False,
                )

        ai_output = draft.ai_output or {}
        merged_final_output = _build_merged_output(template, ai_output, user_output)

        diff = compute_diff(template, ai_output, user_output)

        edit_ids: list[str] = []

        if not diff.is_empty():
            ai_fields = ai_output.get("fields") or {}
            base_context = {
                "retrieved_at": draft.generated_at.isoformat() if draft.generated_at else None,
                "model_used": draft.model_used,
            }

            for field_name, field_diff in diff.fields.items():
                field_entry = ai_fields.get(field_name) or {}
                ai_value = field_entry.get("value")
                user_value = (user_output.get("fields") or {}).get(field_name)
                chunk_ids = field_entry.get("supporting_chunk_ids") or []
                context = {**base_context, "source_chunks": chunk_ids, "context_tags": [draft.template_id, "field"]}

                edit_id = new_uuid7()
                edit = Edit(
                    id=edit_id,
                    draft_id=draft_id,
                    template_id=draft.template_id,
                    template_version=draft.template_version,
                    prompt_fingerprint=draft.prompt_fingerprint,
                    field_or_section_name=field_name,
                    field_type="field",
                    ai_value={"value": ai_value},
                    user_value={"value": user_value},
                    diff=field_diff,
                    context=context,
                )
                self._session.add(edit)
                edit_ids.append(edit_id)

            for section_name, section_diff in diff.sections.items():
                ai_sections_text = ai_output.get("sections_text") or {}
                ai_text = ai_sections_text.get(section_name) or ""
                user_sections = {
                    s["name"]: s["text"]
                    for s in (user_output.get("sections") or [])
                    if isinstance(s, dict)
                }
                user_text = user_sections.get(section_name, "")

                section_chunk_ids = await _get_section_chunk_ids(
                    self._session, draft_id, section_name
                )
                context = {**base_context, "source_chunks": section_chunk_ids, "context_tags": [draft.template_id, "section"]}

                edit_id = new_uuid7()
                edit = Edit(
                    id=edit_id,
                    draft_id=draft_id,
                    template_id=draft.template_id,
                    template_version=draft.template_version,
                    prompt_fingerprint=draft.prompt_fingerprint,
                    field_or_section_name=section_name,
                    field_type="section",
                    ai_value={"text": ai_text},
                    user_value={"text": user_text},
                    diff=section_diff,
                    context=context,
                )
                self._session.add(edit)
                edit_ids.append(edit_id)

        await self._session.flush()

        # Update draft: merge final_output, status, edited_at
        await self._session.execute(
            text(
                "UPDATE app.drafts "
                "SET final_output = CAST(:fo AS jsonb), "
                "    edited_at = NOW(), "
                "    status = 'edited' "
                "WHERE id = :id"
            ),
            {"fo": json.dumps(merged_final_output), "id": draft_id},
        )

        # Enqueue FEW_SHOT_INDEX jobs — same transaction as Edit inserts
        for edit_id in edit_ids:
            await self._queue.enqueue(
                kind=JobKind.FEW_SHOT_INDEX,
                payload={"edit_id": edit_id},
                dedup_key=f"few_shot_index:{edit_id}",
                max_attempts=settings.FEW_SHOT_INDEX_MAX_ATTEMPTS,
            )

        logger.info(
            "edit.saved",
            draft_id=draft_id,
            edit_count=len(edit_ids),
            trace_id=trace_id,
        )
        if not edit_ids:
            return SavedEditResult(edit_ids=[], skipped_reason="no_changes")
        return SavedEditResult(edit_ids=edit_ids)

    async def metrics(
        self,
        template_id: str,
        *,
        days: int = 30,
    ) -> EditMetricsResponse:
        """Return per-field and per-section edit rates for a template."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        template = await self._registry.get_latest(template_id, self._session)

        row = (
            await self._session.execute(
                text(
                    "SELECT COUNT(DISTINCT id) FROM app.drafts "
                    "WHERE template_id = :tid AND created_at > :cutoff"
                ),
                {"tid": template_id, "cutoff": cutoff},
            )
        ).fetchone()
        drafts_count = int(row[0]) if row else 0

        rows = (
            await self._session.execute(
                text(
                    """
                    SELECT e.field_or_section_name, e.field_type,
                           COUNT(DISTINCT e.draft_id) AS edited_count
                    FROM app.edits e
                    JOIN app.drafts d ON d.id = e.draft_id
                    WHERE d.template_id = :tid
                      AND d.created_at > :cutoff
                    GROUP BY e.field_or_section_name, e.field_type
                    """
                ),
                {"tid": template_id, "cutoff": cutoff},
            )
        ).fetchall()
        edit_counts: dict[tuple[str, str], int] = {
            (r[0], r[1]): int(r[2]) for r in rows
        }

        def rate(count: int) -> float | None:
            if drafts_count == 0:
                return None
            return count / drafts_count

        fields = [
            EditMetricFieldRow(
                name=f.name,
                edited_count=edit_counts.get((f.name, "field"), 0),
                edit_rate=rate(edit_counts.get((f.name, "field"), 0)),
            )
            for f in template.extraction_schema
        ]
        sections = [
            EditMetricSectionRow(
                name=s.name,
                edited_count=edit_counts.get((s.name, "section"), 0),
                edit_rate=rate(edit_counts.get((s.name, "section"), 0)),
            )
            for s in template.sections
        ]

        return EditMetricsResponse(
            template_id=template_id,
            window_days=days,
            drafts_count=drafts_count,
            fields=fields,
            sections=sections,
        )


async def _get_section_chunk_ids(
    session: AsyncSession, draft_id: str, section_name: str
) -> list[str]:
    result = await session.execute(
        select(Citation.chunk_id)
        .join(Section, Section.id == Citation.section_id)
        .where(Section.draft_id == draft_id, Section.name == section_name)
    )
    return [str(r.chunk_id) for r in result.fetchall()]


def _build_merged_output(template, ai_output: dict, user_output: dict) -> dict:
    """Merge user_output over ai_output to produce a complete final_output."""
    ai_fields = ai_output.get("fields") or {}
    user_fields = user_output.get("fields") or {}

    merged_fields: dict = {}
    for field_spec in template.extraction_schema:
        if field_spec.name in user_fields:
            merged_fields[field_spec.name] = user_fields[field_spec.name]
        else:
            entry = ai_fields.get(field_spec.name) or {}
            merged_fields[field_spec.name] = entry.get("value")

    ai_sections_text = ai_output.get("sections_text") or {}
    user_sections_by_name = {
        s["name"]: s["text"]
        for s in (user_output.get("sections") or [])
        if isinstance(s, dict) and "name" in s
    }

    merged_sections = []
    for section_spec in template.sections:
        if section_spec.name in user_sections_by_name:
            text_val = user_sections_by_name[section_spec.name]
        else:
            text_val = ai_sections_text.get(section_spec.name)
        merged_sections.append({"name": section_spec.name, "text": text_val})

    return {"fields": merged_fields, "sections": merged_sections}
