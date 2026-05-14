"""DraftEngine — orchestrates template snapshot, retrieval, extraction, generation."""
from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DraftError
from app.draft.draft_repo import DraftRepo
from app.draft.extractor import FieldExtractor
from app.draft.generator import SectionGenerator
from app.draft.templates.validators import run as run_validators

log = structlog.get_logger(__name__)


class DraftEngine:
    def __init__(self, retriever, llm_router, registry, session_factory) -> None:
        self._retriever = retriever
        self._router = llm_router
        self._registry = registry
        self._session_factory = session_factory

    async def generate(
        self,
        draft_id: str,
        template_id: str,
        document_ids: list[str],
        trace_id: str | None,
    ) -> None:
        async with self._session_factory() as session:
            # NN-5: snapshot template exactly once at generate() entry
            template = await self._registry.get_latest(template_id, session)

        fingerprint = template.compute_fingerprint()

        log.bind(
            draft_id=draft_id,
            template_id=template_id,
            prompt_fingerprint=fingerprint,
            trace_id=trace_id,
        )

        async with self._session_factory() as session:
            repo = DraftRepo(session)
            await repo.mark_generating(
                draft_id,
                template_version=template.version,
                prompt_fingerprint=fingerprint,
            )
            await session.commit()

        try:
            # Step 4: retrieve chunks for all query keys
            all_queries = {**template.retrieval_queries}
            retrieved = await self._retriever.multi_retrieve(
                all_queries,
                document_ids,
                top_k_per_query=5,
            )

            # Step 5: field extraction (Pass 1)
            extractor = FieldExtractor(self._router)
            fields = await extractor.extract_all(
                template, retrieved, fingerprint, trace_id
            )

            # Step 6: section generation (Pass 2)
            generator = SectionGenerator(self._router)
            sections, citations = await generator.generate_all(
                template, fields, retrieved, fingerprint, few_shot=[], trace_id=trace_id
            )

            # Step 7: run validators
            fields_plain = {
                name: (ext.value if hasattr(ext, "value") else ext)
                for name, ext in fields.items()
            }
            field_chunk_ids = {
                name: (ext.supporting_chunk_ids if hasattr(ext, "supporting_chunk_ids") else [])
                for name, ext in fields.items()
            }
            sections_plain = [
                {"name": s.section_name, "text": s.text} for s in sections
            ]
            citations_plain = [
                {"section_name": c.section_name, "chunk_id": c.chunk_id}
                for c in citations
            ]
            validators = run_validators(
                template, fields_plain, sections_plain, citations_plain,
                field_chunk_ids=field_chunk_ids,
            )

            # Aggregate model/token stats from all LLM calls
            tokens_in = extractor.tokens_in + generator.tokens_in
            tokens_out = extractor.tokens_out + generator.tokens_out
            cost_usd = extractor.cost_usd + generator.cost_usd
            model_used = generator.model_used if generator.model_used != "unknown" else extractor.model_used

        except Exception as exc:
            async with self._session_factory() as session:
                repo = DraftRepo(session)
                await repo.fail(draft_id, "DRAFT_GENERATION_ERROR", str(exc))
                await session.commit()
            log.error("draft.generation_failed", draft_id=draft_id, error=str(exc))
            raise DraftError(str(exc), code="DRAFT_GENERATION_ERROR") from exc

        # Build section target lengths from template spec
        section_target_lengths = {
            s.name: (s.target_length_min, s.target_length_max)
            for s in template.sections
        }

        # Step 8: persist
        async with self._session_factory() as session:
            repo = DraftRepo(session)
            await repo.finalize(
                draft_id,
                fields=fields,
                sections=sections,
                citations=citations,
                validators=validators,
                model_used=model_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                section_target_lengths=section_target_lengths,
            )
            await session.commit()

        log.info("draft.ready", draft_id=draft_id)

    async def regenerate_section(
        self,
        draft_id: str,
        section_name: str,
        trace_id: str | None,
    ) -> None:
        async with self._session_factory() as session:
            repo = DraftRepo(session)
            draft = await repo.get(draft_id)

        # Pin to original template version (NN-5 across regeneration)
        async with self._session_factory() as session:
            template = await self._registry.get_by_version(
                draft.template_id, draft.template_version, session
            )

        fingerprint = template.compute_fingerprint()

        # Find the section spec
        section_spec = next(
            (s for s in template.sections if s.name == section_name), None
        )
        if section_spec is None:
            from app.core.errors import NotFoundError
            raise NotFoundError(
                f"Section '{section_name}' not in template '{draft.template_id}'",
                code="SECTION_NOT_FOUND",
            )

        retrieved = await self._retriever.multi_retrieve(
            {section_spec.retrieval_key: template.retrieval_queries[section_spec.retrieval_key]},
            list(draft.document_ids or []),
            top_k_per_query=5,
        )

        # Rebuild fields summary from stored ai_output
        fields: dict = {}
        if draft.ai_output and "fields" in draft.ai_output:
            from app.draft.extractor import FieldExtraction
            for name, data in draft.ai_output["fields"].items():
                fields[name] = FieldExtraction(
                    value=data.get("value"),
                    supporting_chunk_ids=data.get("supporting_chunk_ids", []),
                    confidence=data.get("confidence", 0.0),
                    error_code=data.get("error_code"),
                )

        all_chunk_ids: set[str] = set()
        for chunks in retrieved.values():
            for chunk in chunks:
                all_chunk_ids.add(chunk.id)

        generator = SectionGenerator(self._router)
        section_draft, new_citations = await generator._generate_section(
            section_spec, template, fields, retrieved, all_chunk_ids, trace_id
        )

        async with self._session_factory() as session:
            repo = DraftRepo(session)
            await repo.replace_section(
                draft_id, section_name, section_draft.text, new_citations
            )
            await session.commit()

        log.info("draft.section_regenerated", draft_id=draft_id, section=section_name)
