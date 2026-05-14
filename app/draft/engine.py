"""DraftEngine — orchestrates template snapshot, retrieval, extraction, generation."""
from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DraftError
from app.draft.draft_repo import DraftRepo
from app.draft.extractor import FieldExtractor
from app.draft.generator import SectionGenerator
from app.draft.templates.validators import run as run_validators
from app.draft.validator import CitationValidator, ValidationReport, _apply_report_to_citations

log = structlog.get_logger(__name__)


def _section_requires_citations(template, section_name: str) -> bool:
    sec = next((s for s in template.sections if s.name == section_name), None)
    if sec and "cites_at_least_one" in sec.validators:
        return True
    return any(
        v.id == "cites_at_least_one" and v.args.get("section") == section_name
        for v in template.validators
    )


def _gnd(r: ValidationReport) -> float:
    return (r.supported_count / r.total_claims) if r.total_claims else 0.0


class DraftEngine:
    def __init__(
        self,
        retriever,
        llm_router,
        registry,
        session_factory,
        embedder=None,
    ) -> None:
        self._retriever = retriever
        self._router = llm_router
        self._registry = registry
        self._session_factory = session_factory
        self._embedder = embedder

        # Build FewShotStore once at construction — dim check happens here, not per-request.
        self._few_shot_store = None
        if embedder is not None:
            from app.edits.few_shot_store import FewShotStore
            from app.core.errors import EditError
            try:
                self._few_shot_store = FewShotStore(embedder=embedder)
            except EditError:
                log.warning("draft_engine.few_shot_disabled_dim_mismatch", embedder_dim=embedder.dim)

    async def generate(
        self,
        draft_id: str,
        template_id: str,
        document_ids: list[str],
        trace_id: str | None,
    ) -> None:
        async with self._session_factory() as session:
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
            all_queries = {**template.retrieval_queries}
            retrieved = await self._retriever.multi_retrieve(
                all_queries,
                document_ids,
                top_k_per_query=5,
            )

            # Open a dedicated session for the generation phase so extractor/generator
            # can do few-shot retrieval without opening additional sessions inside the LLM calls.
            async with self._session_factory() as gen_session:
                extractor = FieldExtractor(
                    self._router,
                    few_shot_store=self._few_shot_store,
                    current_template_id=template_id,
                    session=gen_session,
                )
                fields = await extractor.extract_all(
                    template, retrieved, fingerprint, trace_id
                )

                generator = SectionGenerator(
                    self._router,
                    few_shot_store=self._few_shot_store,
                    current_template_id=template_id,
                    session=gen_session,
                )
                sections, citations = await generator.generate_all(
                    template, fields, retrieved, fingerprint, trace_id=trace_id
                )

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

            chunks_by_id = {
                chunk.id: chunk
                for chunks in retrieved.values()
                for chunk in chunks
            }
            validator = CitationValidator(self._router)
            section_groundedness: dict[str, float | None] = {}
            total_supported = 0
            total_claims = 0

            for section in sections:
                section_cits = [c for c in citations if c.section_name == section.section_name]
                report = await validator.validate_section(
                    section.text, section_cits, chunks_by_id, fingerprint, trace_id
                )
                needs_retry = (
                    (report.unsupported_count + report.contradicted_count) > 0
                    and _section_requires_citations(template, section.section_name)
                )
                if needs_retry:
                    section_spec = next(
                        s for s in template.sections if s.name == section.section_name
                    )
                    suffix = (
                        f"\n\nYour previous output had "
                        f"{report.unsupported_count + report.contradicted_count} "
                        f"unsupported or contradicted claims. Use only the evidence provided. "
                        f"Cite or remove unsupported statements."
                    )
                    # Retry uses the same gen_session, but it's closed. Open a new one.
                    async with self._session_factory() as retry_session:
                        retry_gen = SectionGenerator(
                            self._router,
                            few_shot_store=self._few_shot_store,
                            current_template_id=template_id,
                            session=retry_session,
                        )
                        retry_section, retry_cits = await retry_gen.generate_section(
                            section_spec, template, fields, retrieved,
                            set(chunks_by_id.keys()), trace_id,
                            extra_instructions=suffix,
                        )
                    retry_report = await validator.validate_section(
                        retry_section.text, retry_cits, chunks_by_id, fingerprint, trace_id
                    )
                    if _gnd(retry_report) >= _gnd(report):
                        idx = sections.index(section)
                        sections[idx] = retry_section
                        citations = [
                            c for c in citations if c.section_name != section.section_name
                        ] + retry_cits
                        section_cits, report = retry_cits, retry_report

                _apply_report_to_citations(report, section_cits)
                section_groundedness[section.section_name] = (
                    report.supported_count / report.total_claims
                    if report.total_claims else None
                )
                total_supported += report.supported_count
                total_claims += report.total_claims

            groundedness_score = (total_supported / total_claims) if total_claims else None

            tokens_in = extractor.tokens_in + generator.tokens_in + validator.tokens_in
            tokens_out = extractor.tokens_out + generator.tokens_out + validator.tokens_out
            cost_usd = extractor.cost_usd + generator.cost_usd + validator.cost_usd
            model_used = generator.model_used if generator.model_used != "unknown" else extractor.model_used

        except Exception as exc:
            async with self._session_factory() as session:
                repo = DraftRepo(session)
                await repo.fail(draft_id, "DRAFT_GENERATION_ERROR", str(exc))
                await session.commit()
            log.error("draft.generation_failed", draft_id=draft_id, error=str(exc))
            raise DraftError(str(exc), code="DRAFT_GENERATION_ERROR") from exc

        section_target_lengths = {
            s.name: (s.target_length_min, s.target_length_max)
            for s in template.sections
        }

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
                groundedness_score=groundedness_score,
                section_groundedness=section_groundedness,
            )
            await session.commit()

        log.info("draft.ready", draft_id=draft_id)

    async def regenerate_section(
        self,
        draft_id: str,
        section_name: str,
        trace_id: str | None,
        *,
        session: AsyncSession | None = None,
    ) -> None:
        async with self._session_factory() as _s:
            repo = DraftRepo(_s)
            draft = await repo.get(draft_id)

        async with self._session_factory() as _s:
            template = await self._registry.get_by_version(
                draft.template_id, draft.template_version, _s
            )

        fingerprint = template.compute_fingerprint()

        section_spec = next(
            (s for s in template.sections if s.name == section_name), None
        )
        if section_spec is None:
            from app.core.errors import NotFoundError
            raise NotFoundError(
                f"Section '{section_name}' not in template '{draft.template_id}'",
                code="SECTION_NOT_FOUND",
            )

        retrieval_query = template.retrieval_queries.get(section_spec.retrieval_key)
        if retrieval_query is None:
            from app.core.errors import NotFoundError
            raise NotFoundError(
                f"Retrieval key '{section_spec.retrieval_key}' not in template '{draft.template_id}'",
                code="RETRIEVAL_KEY_NOT_FOUND",
            )

        retrieved = await self._retriever.multi_retrieve(
            {section_spec.retrieval_key: retrieval_query},
            list(draft.document_ids or []),
            top_k_per_query=5,
        )

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

        # Use passed-in session for few-shot retrieval if available, else open dedicated one
        regen_session_ctx = (
            _nullctx(session) if session is not None
            else self._session_factory()
        )
        async with regen_session_ctx as regen_session:
            generator = SectionGenerator(
                self._router,
                few_shot_store=self._few_shot_store,
                current_template_id=draft.template_id,
                session=regen_session,
            )
            section_draft, new_citations = await generator.generate_section(
                section_spec, template, fields, retrieved, all_chunk_ids, trace_id
            )

        chunks_by_id = {chunk.id: chunk for chunks in retrieved.values() for chunk in chunks}
        validator = CitationValidator(self._router)
        report = await validator.validate_section(
            section_draft.text, new_citations, chunks_by_id, fingerprint, trace_id
        )
        _apply_report_to_citations(report, new_citations)
        section_groundedness_value = (
            report.supported_count / report.total_claims if report.total_claims else None
        )

        async with self._session_factory() as _s:
            repo = DraftRepo(_s)
            await repo.replace_section(
                draft_id, section_name, section_draft.text, new_citations,
                section_groundedness=section_groundedness_value,
            )
            await _s.commit()

        log.info("draft.section_regenerated", draft_id=draft_id, section=section_name)


class _nullctx:
    """Trivial async context manager that yields an already-open session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_) -> None:
        pass
