"""DraftRepo — CRUD for Draft, Section, Citation aggregates."""
from __future__ import annotations
import json
from datetime import datetime, timezone

import structlog
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.db.models.draft import Draft, Section, Citation

log = structlog.get_logger(__name__)


class DraftRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_queued(
        self, template_id: str, document_ids: list[str]
    ) -> str:
        from app.core.ids import new_uuid7
        draft_id = new_uuid7()
        draft = Draft(
            id=draft_id,
            template_id=template_id,
            template_version=0,
            prompt_fingerprint="",
            document_ids=document_ids,
            status="queued",
        )
        self._session.add(draft)
        await self._session.flush()
        return draft_id

    async def mark_generating(
        self, draft_id: str, template_version: int, prompt_fingerprint: str
    ) -> None:
        result = await self._session.execute(
            text(
                "UPDATE app.drafts SET status='generating', template_version=:version, "
                "prompt_fingerprint=:fp WHERE id=:id AND status='queued'"
            ),
            {"id": draft_id, "version": template_version, "fp": prompt_fingerprint},
        )
        if result.rowcount == 0:
            raise ConflictError(
                f"Draft {draft_id} is not in 'queued' state", code="DRAFT_NOT_QUEUED"
            )

    async def finalize(
        self,
        draft_id: str,
        fields: dict,
        sections: list,
        citations: list,
        validators: list,
        model_used: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        section_target_lengths: dict[str, tuple[int, int]] | None = None,
        groundedness_score: float | None = None,
        section_groundedness: dict[str, float | None] | None = None,
    ) -> None:
        # Insert Section rows, build name->id map
        section_id_map: dict[str, str] = {}
        for sec in sections:
            bounds = (section_target_lengths or {}).get(sec.section_name, (None, None))
            sec_obj = Section(
                draft_id=draft_id,
                name=sec.section_name,
                ai_text=sec.text,
                target_length_min=bounds[0],
                target_length_max=bounds[1],
            )
            self._session.add(sec_obj)
            await self._session.flush()
            section_id_map[sec.section_name] = sec_obj.id

        # Insert Citation rows
        for cit in citations:
            section_id = section_id_map.get(cit.section_name)
            if section_id is None:
                continue
            cit_obj = Citation(
                section_id=section_id,
                chunk_id=cit.chunk_id,
                claim_span_start=cit.claim_span_start,
                claim_span_end=cit.claim_span_end,
                validation_status=getattr(cit, "validation_status", "unchecked"),
                validation_reason=getattr(cit, "validation_reason", None),
            )
            self._session.add(cit_obj)

        await self._session.flush()

        # Prepare ai_output
        fields_serialized = {
            name: {
                "value": ext.value,
                "supporting_chunk_ids": ext.supporting_chunk_ids,
                "confidence": ext.confidence,
                "error_code": ext.error_code,
            }
            for name, ext in fields.items()
        }
        sections_text = {sec.section_name: sec.text for sec in sections}
        retrieval_meta = {
            "dangling_citations": sum(len(sec.dangling_citations) for sec in sections)
        }
        validators_data = [
            {"field_or_section": v.field_or_section, "validator_id": v.validator_id,
             "status": v.status, "message": v.message}
            for v in validators
        ]
        ai_output = {
            "fields": fields_serialized,
            "sections_text": sections_text,
            "validators": validators_data,
            "retrieval_meta": retrieval_meta,
            "sections_groundedness": section_groundedness or {},
        }

        await self._session.execute(
            text(
                "UPDATE app.drafts SET status='ready', generated_at=:now, "
                "ai_output=CAST(:ai_output AS jsonb), model_used=:model, "
                "tokens_in=:ti, tokens_out=:to_, cost_usd=:cost, "
                "groundedness_score=:gnd "
                "WHERE id=:id"
            ),
            {
                "id": draft_id,
                "now": datetime.now(timezone.utc),
                "ai_output": json.dumps(ai_output),
                "model": model_used,
                "ti": tokens_in,
                "to_": tokens_out,
                "cost": cost_usd,
                "gnd": groundedness_score,
            },
        )

    async def fail(self, draft_id: str, error_code: str, message: str) -> None:
        ai_output = json.dumps({"error_code": error_code, "error_message": message})
        await self._session.execute(
            text(
                "UPDATE app.drafts SET status='failed', "
                "ai_output=CAST(:ai_output AS jsonb) WHERE id=:id"
            ),
            {"id": draft_id, "ai_output": ai_output},
        )

    async def get(self, draft_id: str) -> Draft:
        result = await self._session.execute(
            select(Draft).where(Draft.id == draft_id)
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            raise NotFoundError(f"Draft {draft_id} not found", code="DRAFT_NOT_FOUND")
        return draft

    async def replace_section(
        self,
        draft_id: str,
        section_name: str,
        new_text: str,
        new_citations: list,
        section_groundedness: float | None = None,
    ) -> None:
        # Find existing section
        result = await self._session.execute(
            select(Section).where(
                Section.draft_id == draft_id,
                Section.name == section_name,
            )
        )
        section = result.scalar_one_or_none()
        if section is None:
            raise NotFoundError(
                f"Section '{section_name}' not found in draft {draft_id}",
                code="SECTION_NOT_FOUND",
            )

        # Delete old citations
        await self._session.execute(
            text("DELETE FROM app.citations WHERE section_id=:sid"),
            {"sid": section.id},
        )

        # Update section text
        section.ai_text = new_text
        await self._session.flush()

        # Insert new citations
        for cit in new_citations:
            cit_obj = Citation(
                section_id=section.id,
                chunk_id=cit.chunk_id,
                claim_span_start=cit.claim_span_start,
                claim_span_end=cit.claim_span_end,
                validation_status=getattr(cit, "validation_status", "unchecked"),
                validation_reason=getattr(cit, "validation_reason", None),
            )
            self._session.add(cit_obj)

        await self._session.flush()

        # Update sections_groundedness and recompute draft groundedness_score
        if section_groundedness is not None:
            await self._session.execute(
                text(
                    """
                    UPDATE app.drafts
                    SET ai_output = jsonb_set(
                            COALESCE(ai_output, '{}'::jsonb),
                            '{sections_groundedness}',
                            COALESCE(ai_output->'sections_groundedness', '{}'::jsonb)
                            || jsonb_build_object(:section_name::text, :gnd::numeric)
                        ),
                        groundedness_score = (
                            SELECT AVG((val #>> '{}')::numeric)
                            FROM jsonb_each(
                                jsonb_set(
                                    COALESCE(ai_output, '{}'::jsonb)->'sections_groundedness',
                                    ARRAY[:section_name::text],
                                    to_jsonb(:gnd::numeric)
                                )
                            ) AS kv(key, val)
                            WHERE jsonb_typeof(val) = 'number'
                        )
                    WHERE id = :draft_id
                    """
                ),
                {
                    "draft_id": draft_id,
                    "section_name": section_name,
                    "gnd": section_groundedness,
                },
            )
