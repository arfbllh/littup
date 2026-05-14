"""Eval: citation validity across all templates × ready documents.

Measures:
  pct_supported_claims       — fraction of [chunk:X] citations whose validation_status
                               is 'supported' across all generated drafts.
  pct_sections_high_groundedness — fraction of sections where groundedness >= 0.8
                               (read from draft.ai_output['sections_groundedness']).
  fabricated_chunk_ids       — count of [chunk:X] references in section text where
                               chunk ID X does not exist in app.chunks.

Usage:
    python eval/run_citation_validity.py
    make eval
"""
from __future__ import annotations

import asyncio
import re
import textwrap
from typing import Any

import structlog
from sqlalchemy import text

from eval.common import EvalContext, build_context, write_report

log = structlog.get_logger(__name__)

# Regex to parse [chunk:CHUNK_ID] citations in section text
_CHUNK_REF_RE = re.compile(r"\[chunk:([^\]]+)\]")

# Maximum ready documents to evaluate per template (keeps eval fast)
_MAX_DOCS_PER_TEMPLATE = 3


async def run(
    db_url: str | None = None,
    mock_llm: bool = False,
    _ctx: EvalContext | None = None,
) -> dict:
    """Run citation validity eval.

    Parameters
    ----------
    db_url:   Override DATABASE_URL (integration tests pass TEST_DATABASE_URL).
    mock_llm: Use stub models instead of real inference.
    _ctx:     Inject a pre-built EvalContext (used by integration tests to bypass
              the real LLM / embedder wiring).

    Returns
    -------
    dict with keys: pct_supported_claims, pct_sections_high_groundedness,
                    fabricated_chunk_ids, drafts_evaluated.
    """
    ctx = _ctx or await build_context(db_url=db_url, mock_llm=mock_llm)

    # ── 1. Find ready documents ───────────────────────────────────────────────
    async with ctx.session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT id FROM app.documents WHERE status = 'ready' ORDER BY created_at")
            )
        ).fetchall()

    ready_doc_ids: list[str] = [r[0] for r in rows]

    if not ready_doc_ids:
        log.warning(
            "eval.citation_validity.no_ready_documents",
            hint="run `make seed` first",
        )
        empty: dict[str, Any] = {
            "pct_supported_claims": None,
            "pct_sections_high_groundedness": None,
            "fabricated_chunk_ids": 0,
            "drafts_evaluated": 0,
        }
        write_report(
            "citation_validity",
            empty,
            _build_markdown(empty),
        )
        return empty

    # ── 2. Enumerate templates from registry ─────────────────────────────────
    template_ids: list[str] = list(ctx.registry._templates.keys())

    if not template_ids:
        log.warning("eval.citation_validity.no_templates_loaded")
        empty = {
            "pct_supported_claims": None,
            "pct_sections_high_groundedness": None,
            "fabricated_chunk_ids": 0,
            "drafts_evaluated": 0,
        }
        write_report("citation_validity", empty, _build_markdown(empty))
        return empty

    # ── 3. Generate drafts for each (template, document) pair ────────────────
    from app.draft.draft_repo import DraftRepo

    draft_ids: list[str] = []

    docs_sample = ready_doc_ids[:_MAX_DOCS_PER_TEMPLATE]

    for template_id in template_ids:
        for doc_id in docs_sample:
            async with ctx.session_factory() as session:
                repo = DraftRepo(session)
                draft_id = await repo.create_queued(template_id, [doc_id])
                await session.commit()

            log.info(
                "eval.citation_validity.generating",
                draft_id=draft_id,
                template_id=template_id,
                doc_id=doc_id,
            )
            try:
                await ctx.draft_engine.generate(
                    draft_id=draft_id,
                    template_id=template_id,
                    document_ids=[doc_id],
                    trace_id=None,
                )
                draft_ids.append(draft_id)
            except Exception as exc:
                log.error(
                    "eval.citation_validity.generation_failed",
                    draft_id=draft_id,
                    error=str(exc),
                )
                # Skip this draft but continue with others

    # ── 4. Load sections + citations and compute metrics ─────────────────────
    total_citations = 0
    total_supported = 0
    total_sections = 0
    sections_high_gnd = 0
    total_fabricated = 0

    # Collect all chunk refs across all section texts for a single bulk lookup
    all_chunk_refs: set[str] = set()
    section_texts_for_ref_check: list[str] = []

    async with ctx.session_factory() as session:
        for draft_id in draft_ids:
            # Load draft.ai_output for sections_groundedness
            draft_row = (
                await session.execute(
                    text(
                        "SELECT ai_output FROM app.drafts WHERE id = :id AND status = 'ready'"
                    ),
                    {"id": draft_id},
                )
            ).fetchone()

            if draft_row is None:
                # Draft failed or never finalized
                continue

            ai_output: dict = draft_row[0] or {}
            sections_groundedness: dict = ai_output.get("sections_groundedness", {})

            # Load sections
            sec_rows = (
                await session.execute(
                    text(
                        "SELECT id, name, ai_text FROM app.sections WHERE draft_id = :did"
                    ),
                    {"did": draft_id},
                )
            ).fetchall()

            for sec_id, sec_name, sec_text in sec_rows:
                total_sections += 1

                # Check groundedness from ai_output
                gnd = sections_groundedness.get(sec_name)
                if gnd is not None and float(gnd) >= 0.8:
                    sections_high_gnd += 1

                # Collect chunk refs from text for fabrication check
                if sec_text:
                    refs = _CHUNK_REF_RE.findall(sec_text)
                    all_chunk_refs.update(refs)
                    section_texts_for_ref_check.append(sec_text)

                # Load citations for this section
                cit_rows = (
                    await session.execute(
                        text(
                            "SELECT validation_status FROM app.citations WHERE section_id = :sid"
                        ),
                        {"sid": sec_id},
                    )
                ).fetchall()

                for (status,) in cit_rows:
                    total_citations += 1
                    if status == "supported":
                        total_supported += 1

        # ── 5. Fabricated chunk_id check ──────────────────────────────────────
        if all_chunk_refs:
            ref_list = list(all_chunk_refs)
            existing_rows = (
                await session.execute(
                    text("SELECT id FROM app.chunks WHERE id = ANY(:ids)"),
                    {"ids": ref_list},
                )
            ).fetchall()
            existing_ids = {r[0] for r in existing_rows}
            total_fabricated = sum(
                1 for ref in all_chunk_refs if ref not in existing_ids
            )

    # ── 6. Aggregate metrics ──────────────────────────────────────────────────
    pct_supported = (total_supported / total_citations) if total_citations > 0 else None
    pct_high_gnd = (sections_high_gnd / total_sections) if total_sections > 0 else None

    metrics: dict[str, Any] = {
        "pct_supported_claims": pct_supported,
        "pct_sections_high_groundedness": pct_high_gnd,
        "fabricated_chunk_ids": total_fabricated,
        "drafts_evaluated": len(draft_ids),
        "total_citations": total_citations,
        "total_supported": total_supported,
        "total_sections": total_sections,
        "sections_high_groundedness": sections_high_gnd,
    }

    write_report("citation_validity", metrics, _build_markdown(metrics))
    log.info("eval.citation_validity.done", **metrics)
    return metrics


def _build_markdown(m: dict) -> str:
    pct_sup = m.get("pct_supported_claims")
    pct_gnd = m.get("pct_sections_high_groundedness")
    fab = m.get("fabricated_chunk_ids", 0)
    n_drafts = m.get("drafts_evaluated", 0)
    total_cit = m.get("total_citations", 0)
    total_sup = m.get("total_supported", 0)
    total_sec = m.get("total_sections", 0)
    sec_high = m.get("sections_high_groundedness", 0)

    pct_sup_str = f"{pct_sup:.1%}" if pct_sup is not None else "n/a (no drafts)"
    pct_gnd_str = f"{pct_gnd:.1%}" if pct_gnd is not None else "n/a (no drafts)"

    return textwrap.dedent(f"""\
        # Citation Validity Eval

        | Metric | Value |
        |--------|-------|
        | % Supported claims | {pct_sup_str} |
        | % Sections ≥ 0.8 groundedness | {pct_gnd_str} |
        | Fabricated chunk_ids | {fab} |
        | Drafts evaluated | {n_drafts} |
        | Total citations | {total_cit} |
        | Supported citations | {total_sup} |
        | Total sections | {total_sec} |
        | Sections with high groundedness | {sec_high} |

        ## Notes

        - "Supported" means `citations.validation_status = 'supported'`.
        - "High groundedness" means section groundedness score ≥ 0.8
          (from `draft.ai_output['sections_groundedness']`).
        - "Fabricated chunk_ids" counts `[chunk:X]` references in section text
          where chunk X does not exist in `app.chunks`.
        - Eval generates at most {_MAX_DOCS_PER_TEMPLATE} draft(s) per template.
    """)


if __name__ == "__main__":
    asyncio.run(run())
