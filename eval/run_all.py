"""Orchestrator: run all eval scripts and write eval/reports/report.md.

Usage:
    python eval/run_all.py
    make eval
"""
from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import structlog

from eval.common import REPORTS_DIR, write_report

log = structlog.get_logger(__name__)


async def main() -> None:
    import eval.run_retrieval as _retrieval
    import eval.run_citation_validity as _citation
    import eval.run_edit_improvement as _edit

    log.info("eval.start")

    r = await _retrieval.run()
    log.info("eval.retrieval_done", recall_at_5=r.get("recall_at_5"), recall_at_10=r.get("recall_at_10"))

    c = await _citation.run()
    log.info("eval.citation_done", pct_supported=c.get("pct_supported_claims"))

    e = await _edit.run()
    log.info("eval.edit_done", improvement_ratio=e.get("improvement_ratio"))

    _write_summary_report(r, c, e)
    log.info("eval.done", report=str(REPORTS_DIR / "report.md"))


def _write_summary_report(r: dict, c: dict, e: dict) -> None:
    # Build summary markdown
    recall5 = r.get("recall_at_5", "n/a")
    recall10 = r.get("recall_at_10", "n/a")
    lt_recall10 = r.get("per_tag", {}).get("legal_term", {}).get("recall_at_10", "n/a")
    pct_supported = c.get("pct_supported_claims", "n/a")
    fabricated = c.get("fabricated_chunk_ids", "n/a")
    improvement = e.get("improvement_ratio", "n/a")

    summary = textwrap.dedent(f"""\
        # Eval Report

        | Metric | Value |
        |--------|-------|
        | Recall@5 (all tags) | {recall5} |
        | Recall@10 (all tags) | {recall10} |
        | legal_term Recall@10 | {lt_recall10} |
        | % Supported Claims | {pct_supported} |
        | Fabricated chunk_ids | {fabricated} |
        | Edit Improvement Ratio | {improvement} |

        ## Details

    """)

    # Append individual reports if they exist
    for name in ("retrieval", "citation_validity", "edit_improvement"):
        md_path = REPORTS_DIR / f"{name}.md"
        if md_path.exists():
            summary += f"\n---\n\n{md_path.read_text()}\n"

    data = {
        "recall_at_5": recall5,
        "recall_at_10": recall10,
        "legal_term_recall_at_10": lt_recall10,
        "pct_supported_claims": pct_supported,
        "fabricated_chunk_ids": fabricated,
        "improvement_ratio": improvement,
    }
    write_report("report", data, summary)


if __name__ == "__main__":
    asyncio.run(main())
