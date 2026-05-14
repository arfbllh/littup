"""Edit-improvement eval: measures whether the few-shot store + rule extractor
reduce the Levenshtein distance between AI output and operator gold values after
being seeded with operator edit examples.

Usage:
    python eval/run_edit_improvement.py
    # Or called from eval/run_all.py
"""
from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import Any

import structlog

from eval.common import EvalContext, build_context, write_report

log = structlog.get_logger(__name__)

GOLD_PATH = Path(__file__).parent / "data" / "extraction_gold.jsonl"
EDIT_PAIRS_PATH = Path(__file__).parent / "data" / "edit_pairs.jsonl"


# ── Levenshtein distance ──────────────────────────────────────────────────────

def _levenshtein(a: str, b: str) -> int:
    """Simple Wagner-Fischer edit distance. Used when the Levenshtein package
    is not installed."""
    m, n = len(a), len(b)
    if m < n:
        a, b, m, n = b, a, n, m
    # Keep only two rows
    prev = list(range(n + 1))
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        curr[0] = i
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1]
            else:
                curr[j] = 1 + min(prev[j], curr[j - 1], prev[j - 1])
        prev, curr = curr, prev
    return prev[n]


try:
    import Levenshtein as _lev_pkg

    def _edit_distance(a: str, b: str) -> int:
        return _lev_pkg.distance(a, b)

except ImportError:
    log.debug("eval.edit_improvement.levenshtein_fallback", reason="package not installed")
    _edit_distance = _levenshtein


# ── Data loading ─────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


# ── Document lookup ───────────────────────────────────────────────────────────

async def _find_ready_doc(ctx: EvalContext, stem: str) -> str | None:
    """Return the UUID of the first ready document whose filename contains *stem*."""
    from sqlalchemy import text as sqlt

    async with ctx.session_factory() as s:
        row = (
            await s.execute(
                sqlt(
                    "SELECT id FROM app.documents"
                    " WHERE filename LIKE :pattern AND status = 'ready'"
                    " LIMIT 1"
                ),
                {"pattern": f"%{stem}%"},
            )
        ).fetchone()
    if row is None:
        log.warning(
            "eval.edit_improvement.doc_not_found",
            stem=stem,
            hint="Ensure fixture documents are seeded with status=ready",
        )
        return None
    return str(row[0])


# ── Draft helpers ─────────────────────────────────────────────────────────────

async def _insert_queued_draft(
    ctx: EvalContext,
    template_id: str,
    document_ids: list[str],
) -> str:
    """Insert a draft row in 'queued' state and return its ID."""
    from sqlalchemy import text as sqlt

    from app.core.ids import new_uuid7

    draft_id = new_uuid7()
    async with ctx.session_factory() as s:
        await s.execute(
            sqlt(
                "INSERT INTO app.drafts "
                "(id, template_id, template_version, prompt_fingerprint, document_ids, status) "
                "VALUES (:id, :tid, 0, '', CAST(:doc_ids AS uuid[]), 'queued')"
            ),
            {"id": draft_id, "tid": template_id, "doc_ids": "{" + ",".join(document_ids) + "}"},
        )
        await s.commit()
    return draft_id


async def _reload_draft_fields(ctx: EvalContext, draft_id: str) -> dict:
    """Return the ai_output['fields'] dict for a draft, or {} if not found."""
    from sqlalchemy import text as sqlt

    async with ctx.session_factory() as s:
        row = (
            await s.execute(
                sqlt("SELECT ai_output FROM app.drafts WHERE id = :id"),
                {"id": draft_id},
            )
        ).fetchone()
    if row is None or row[0] is None:
        return {}
    ai_output = row[0]
    return ai_output.get("fields") or {}


# ── Levenshtein scoring ───────────────────────────────────────────────────────

def _score_fields(
    ai_fields: dict,
    gold_fields: dict,
) -> dict[str, int]:
    """Return {field_name: levenshtein_distance} for each non-None gold field."""
    scores: dict[str, int] = {}
    for field_name, gold_val in gold_fields.items():
        if gold_val is None:
            continue
        field_entry = ai_fields.get(field_name)
        if isinstance(field_entry, dict):
            ai_val = field_entry.get("value")
        else:
            ai_val = field_entry
        ai_str = json.dumps(ai_val, default=str, sort_keys=True)
        gold_str = json.dumps(gold_val, default=str, sort_keys=True)
        scores[field_name] = _edit_distance(ai_str, gold_str)
    return scores


# ── Improvement ratio ─────────────────────────────────────────────────────────

def _compute_improvement_ratio(
    baseline_scores: dict[str, dict[str, int]],
    post_scores: dict[str, dict[str, int]],
) -> float:
    """Mean (1 - post/baseline) over all (template, field) pairs where baseline > 0."""
    ratios: list[float] = []
    for template_id, b_fields in baseline_scores.items():
        p_fields = post_scores.get(template_id, {})
        for field_name, b_dist in b_fields.items():
            if b_dist == 0:
                continue
            p_dist = p_fields.get(field_name, b_dist)
            ratios.append(1.0 - p_dist / b_dist)
    if not ratios:
        return 0.0
    return sum(ratios) / len(ratios)


# ── Markdown report ───────────────────────────────────────────────────────────

def _build_markdown(data: dict) -> str:
    improvement = data["improvement_ratio"]
    rules = data["rules_extracted"]
    baseline_scores = data["baseline_scores"]
    post_scores = data["post_scores"]

    rows = ""
    for template_id, fields in baseline_scores.items():
        p_fields = post_scores.get(template_id, {})
        for field_name, b_dist in sorted(fields.items()):
            p_dist = p_fields.get(field_name, b_dist)
            delta = b_dist - p_dist
            rows += f"| {template_id} | {field_name} | {b_dist} | {p_dist} | {delta:+d} |\n"

    return textwrap.dedent(f"""\
        # Edit Improvement Eval

        **Improvement ratio**: {improvement:.3f} (mean 1 - post/baseline over all scored fields)
        **Rules extracted**: {rules}

        | Template | Field | Baseline dist | Post dist | Delta |
        |----------|-------|---------------|-----------|-------|
        {rows}
        > Positive delta = AI output moved closer to gold after few-shot + rule seeding.
    """)


# ── Main entry point ──────────────────────────────────────────────────────────

async def run(
    db_url: str | None = None,
    mock_llm: bool = False,
    _ctx: EvalContext | None = None,
) -> dict:
    """Run the edit-improvement eval and return a metrics dict.

    Parameters
    ----------
    db_url:
        Override the database URL (defaults to settings.DATABASE_URL).
    mock_llm:
        Use stub LLM/embedder (no GPU/API keys needed).
    _ctx:
        Inject a pre-built EvalContext (used by integration tests).
    """
    ctx = _ctx if _ctx is not None else await build_context(db_url=db_url, mock_llm=mock_llm)

    gold_rows = _load_jsonl(GOLD_PATH)
    edit_pairs = _load_jsonl(EDIT_PAIRS_PATH)

    log.info(
        "eval.edit_improvement.loaded",
        gold_rows=len(gold_rows),
        edit_pairs=len(edit_pairs),
    )

    # ── Resolve document stems → UUIDs (only ready docs) ──────────────────
    # For each gold row, resolve the doc UUID once.
    doc_uuid_by_stem: dict[str, str | None] = {}
    for row in gold_rows:
        stem = row["document_id"]
        if stem not in doc_uuid_by_stem:
            doc_uuid_by_stem[stem] = await _find_ready_doc(ctx, stem)

    # Guard: if no ready documents found at all, return zeros.
    if all(v is None for v in doc_uuid_by_stem.values()):
        log.warning(
            "eval.edit_improvement.no_ready_docs",
            hint="Ensure fixture documents are seeded with status=ready; returning zeros.",
        )
        empty_metrics = {
            "improvement_ratio": 0.0,
            "baseline_scores": {},
            "post_scores": {},
            "rules_extracted": 0,
        }
        write_report("edit_improvement", empty_metrics, _build_markdown(empty_metrics))
        return empty_metrics

    # ── Baseline pass ──────────────────────────────────────────────────────
    baseline_scores: dict[str, dict[str, int]] = {}
    # Also keep the (template_id, doc_id) pairs for the post-loop pass.
    evaluated_pairs: list[tuple[str, str, list[str]]] = []  # (template_id, draft_id, [doc_id])

    for gold_row in gold_rows:
        template_id = gold_row["template_id"]
        stem = gold_row["document_id"]
        gold_fields = gold_row.get("fields") or {}

        doc_id = doc_uuid_by_stem.get(stem)
        if doc_id is None:
            log.warning(
                "eval.edit_improvement.baseline_skip",
                template_id=template_id,
                document_id=stem,
                reason="document not found",
            )
            continue

        # Insert a fresh queued draft.
        draft_id = await _insert_queued_draft(ctx, template_id, [doc_id])

        try:
            await ctx.draft_engine.generate(
                draft_id, template_id, [doc_id], trace_id=None
            )
        except Exception as exc:
            log.warning(
                "eval.edit_improvement.baseline_generate_failed",
                template_id=template_id,
                draft_id=draft_id,
                error=str(exc),
            )
            continue

        ai_fields = await _reload_draft_fields(ctx, draft_id)
        field_scores = _score_fields(ai_fields, gold_fields)

        # Merge into template-level baseline dict (last writer wins per field).
        baseline_scores.setdefault(template_id, {}).update(field_scores)
        evaluated_pairs.append((template_id, draft_id, [doc_id]))

        log.debug(
            "eval.edit_improvement.baseline_done",
            template_id=template_id,
            document_id=stem,
            field_scores=field_scores,
        )

    # ── Seed few-shot store ───────────────────────────────────────────────
    # Build a per-template mapping to a ready doc_id for synthetic drafts.
    template_doc_map: dict[str, str] = {}
    for template_id, _draft_id, doc_ids in evaluated_pairs:
        if template_id not in template_doc_map and doc_ids:
            template_doc_map[template_id] = doc_ids[0]

    for pair in edit_pairs:
        template_id = pair["template_id"]
        field_or_section = pair["field_or_section"]
        field_type = pair.get("field_type", "field")
        ai_val = pair["ai"]
        user_val = pair["user"]

        doc_id = template_doc_map.get(template_id)
        if doc_id is None:
            # Fall back to first ready doc in the DB.
            doc_id = await _find_ready_doc(ctx, "")
        if doc_id is None:
            log.warning(
                "eval.edit_improvement.seed_skip",
                template_id=template_id,
                reason="no ready document available",
            )
            continue

        from app.core.ids import new_uuid7

        # Resolve the template version/fingerprint from the registry.
        try:
            async with ctx.session_factory() as s:
                template = await ctx.registry.get_latest(template_id, s)
            t_version = template.version
            t_fingerprint = template.compute_fingerprint()
        except Exception as exc:
            log.warning(
                "eval.edit_improvement.seed_template_missing",
                template_id=template_id,
                error=str(exc),
            )
            continue

        # Determine whether this is a section or a field.
        is_section = field_type == "section"

        if is_section:
            ai_output = {"sections_text": {field_or_section: ai_val}}
        else:
            ai_output = {"fields": {field_or_section: {"value": ai_val}}}

        synth_draft_id = new_uuid7()
        from sqlalchemy import text as sqlt

        try:
            async with ctx.session_factory() as s:
                await s.execute(
                    sqlt(
                        "INSERT INTO app.drafts "
                        "(id, template_id, template_version, prompt_fingerprint, "
                        "document_ids, status, ai_output) "
                        "VALUES (:id, :tid, :ver, :fp, CAST(:doc_ids AS uuid[]), 'ready', "
                        "CAST(:ai_output AS jsonb))"
                    ),
                    {
                        "id": synth_draft_id,
                        "tid": template_id,
                        "ver": t_version,
                        "fp": t_fingerprint,
                        "doc_ids": "{" + doc_id + "}",
                        "ai_output": json.dumps(ai_output),
                    },
                )
                await s.commit()
        except Exception as exc:
            log.warning(
                "eval.edit_improvement.seed_draft_insert_failed",
                template_id=template_id,
                error=str(exc),
            )
            continue

        # Build the user_output payload (fields dict or sections list).
        if is_section:
            user_output = {"sections": [{"name": field_or_section, "text": user_val}]}
        else:
            user_output = {"fields": {field_or_section: user_val}}

        try:
            async with ctx.session_factory() as s:
                svc = ctx.edit_service_factory(s)
                save_result = await svc.save_edit(
                    synth_draft_id, user_output, trace_id=None
                )
                await s.commit()
        except Exception as exc:
            log.warning(
                "eval.edit_improvement.seed_edit_failed",
                template_id=template_id,
                draft_id=synth_draft_id,
                error=str(exc),
            )
            continue

        # Run few-shot indexing inline for each new edit.
        try:
            from app.edits.few_shot_store import FewShotStore

            fs_store = FewShotStore(embedder=ctx.embedder)
            for edit_id in (save_result.edit_ids or []):
                async with ctx.session_factory() as s:
                    await fs_store.index(edit_id, session=s)
                    await s.commit()
        except Exception as exc:
            log.warning(
                "eval.edit_improvement.few_shot_index_failed",
                error=str(exc),
                hint="Skipping few-shot indexing; test still continues",
            )

    log.info("eval.edit_improvement.seed_done", pairs=len(edit_pairs))

    # ── Rule extraction ───────────────────────────────────────────────────
    unique_template_ids = {p["template_id"] for p in edit_pairs}
    rules_extracted_total = 0

    for template_id in sorted(unique_template_ids):
        try:
            async with ctx.session_factory() as data_session:
                async with ctx.session_factory() as lock_session:
                    extractor = ctx.rule_extractor_factory(data_session, lock_session)
                    result = await extractor.run(template_id, trace_id=None)
                    await data_session.commit()

            if result.skipped_reason is not None:
                log.info(
                    "eval.edit_improvement.rule_extraction_skipped",
                    template_id=template_id,
                    reason=result.skipped_reason,
                )
            else:
                count = len(result.new_rules)
                rules_extracted_total += count
                log.info(
                    "eval.edit_improvement.rule_extraction_done",
                    template_id=template_id,
                    new_rules=count,
                    new_version=result.new_version,
                )
        except Exception as exc:
            log.warning(
                "eval.edit_improvement.rule_extraction_failed",
                template_id=template_id,
                error=str(exc),
            )

    # ── Post-loop pass ────────────────────────────────────────────────────
    post_scores: dict[str, dict[str, int]] = {}

    for gold_row in gold_rows:
        template_id = gold_row["template_id"]
        stem = gold_row["document_id"]
        gold_fields = gold_row.get("fields") or {}

        doc_id = doc_uuid_by_stem.get(stem)
        if doc_id is None:
            continue

        draft_id = await _insert_queued_draft(ctx, template_id, [doc_id])

        try:
            await ctx.draft_engine.generate(
                draft_id, template_id, [doc_id], trace_id=None
            )
        except Exception as exc:
            log.warning(
                "eval.edit_improvement.post_generate_failed",
                template_id=template_id,
                draft_id=draft_id,
                error=str(exc),
            )
            continue

        ai_fields = await _reload_draft_fields(ctx, draft_id)
        field_scores = _score_fields(ai_fields, gold_fields)
        post_scores.setdefault(template_id, {}).update(field_scores)

        log.debug(
            "eval.edit_improvement.post_done",
            template_id=template_id,
            document_id=stem,
            field_scores=field_scores,
        )

    # ── Aggregate ─────────────────────────────────────────────────────────
    improvement_ratio = _compute_improvement_ratio(baseline_scores, post_scores)

    metrics = {
        "improvement_ratio": improvement_ratio,
        "baseline_scores": baseline_scores,
        "post_scores": post_scores,
        "rules_extracted": rules_extracted_total,
    }

    markdown = _build_markdown(metrics)
    write_report("edit_improvement", metrics, markdown)

    log.info(
        "eval.edit_improvement.done",
        improvement_ratio=improvement_ratio,
        rules_extracted=rules_extracted_total,
        templates_evaluated=len(baseline_scores),
    )

    return metrics


if __name__ == "__main__":
    asyncio.run(run())
