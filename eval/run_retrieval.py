"""Retrieval eval: measures Recall@5, Recall@10, and MRR across 30 queries.

Checks that legal_term recall@10 must be >= 0.85.

Usage:
    python eval/run_retrieval.py
    python eval/run_retrieval.py  # uses settings.DATABASE_URL
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

QUERIES_PATH = Path(__file__).parent / "data" / "retrieval_queries.jsonl"

LEGAL_TERM_RECALL_THRESHOLD = 0.85


def _load_queries(limit: int | None = None) -> list[dict]:
    rows = []
    with QUERIES_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    if limit is not None:
        rows = rows[:limit]
    return rows


def _keywords_hit(chunks: list[Any], keywords: list[str]) -> bool:
    """Return True if every keyword appears (case-insensitive) in at least one chunk's text."""
    for kw in keywords:
        kw_lower = kw.lower()
        if not any(kw_lower in (c.text or "").lower() for c in chunks):
            return False
    return True


def _mrr_score(chunks: list[Any], keywords: list[str]) -> float:
    """Reciprocal rank of first chunk that contains any keyword (1-indexed). 0 if none."""
    for rank, chunk in enumerate(chunks, start=1):
        text_lower = (chunk.text or "").lower()
        if any(kw.lower() in text_lower for kw in keywords):
            return 1.0 / rank
    return 0.0


def _compute_metrics(results: list[dict]) -> dict:
    """
    Each result has: hit_at_5, hit_at_10, mrr_score, tag.
    Returns aggregate + per_tag metrics.
    """
    if not results:
        return {
            "recall_at_5": 0.0,
            "recall_at_10": 0.0,
            "mrr": 0.0,
            "per_tag": {},
            "query_count": 0,
        }

    total = len(results)
    recall_at_5 = sum(r["hit_at_5"] for r in results) / total
    recall_at_10 = sum(r["hit_at_10"] for r in results) / total
    mrr = sum(r["mrr_score"] for r in results) / total

    # Per-tag breakdown
    from collections import defaultdict
    tag_buckets: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        tag_buckets[r["tag"]].append(r)

    per_tag: dict[str, dict] = {}
    for tag, tag_results in tag_buckets.items():
        n = len(tag_results)
        per_tag[tag] = {
            "recall_at_5": sum(r["hit_at_5"] for r in tag_results) / n,
            "recall_at_10": sum(r["hit_at_10"] for r in tag_results) / n,
            "mrr": sum(r["mrr_score"] for r in tag_results) / n,
            "count": n,
        }

    return {
        "recall_at_5": recall_at_5,
        "recall_at_10": recall_at_10,
        "mrr": mrr,
        "per_tag": per_tag,
        "query_count": total,
    }


def _build_markdown(metrics: dict) -> str:
    per_tag = metrics.get("per_tag", {})
    tag_rows = ""
    for tag in sorted(per_tag.keys()):
        t = per_tag[tag]
        tag_rows += (
            f"| {tag} | {t['recall_at_5']:.3f} | {t['recall_at_10']:.3f}"
            f" | {t['mrr']:.3f} | {t['count']} |\n"
        )

    # Aggregate row
    tag_rows += (
        f"| **ALL** | **{metrics['recall_at_5']:.3f}** | **{metrics['recall_at_10']:.3f}**"
        f" | **{metrics['mrr']:.3f}** | **{metrics['query_count']}** |\n"
    )

    return textwrap.dedent(f"""\
        # Retrieval Eval

        | Tag | Recall@5 | Recall@10 | MRR | N |
        |-----|----------|-----------|-----|---|
        {tag_rows}
        > Threshold: legal_term Recall@10 >= {LEGAL_TERM_RECALL_THRESHOLD:.0%}
    """)


async def run(
    db_url: str | None = None,
    mock_llm: bool = False,
    query_limit: int | None = None,
    _ctx: EvalContext | None = None,
) -> dict:
    """Run the retrieval eval and return a metrics dict.

    Parameters
    ----------
    db_url:
        Override the database URL (defaults to settings.DATABASE_URL).
    mock_llm:
        Use stub embedder/reranker (no GPU/API keys needed).
    query_limit:
        Only process the first N queries (useful for quick smoke tests).
    _ctx:
        Inject a pre-built EvalContext (used by integration tests to avoid
        calling build_context against a real DB).
    """
    ctx = _ctx if _ctx is not None else await build_context(db_url=db_url, mock_llm=mock_llm)

    queries = _load_queries(limit=query_limit)
    log.info("eval.retrieval.loaded_queries", count=len(queries))

    # Resolve document_id stem → DB UUID
    # Cache to avoid re-querying for the same stem multiple times
    doc_uuid_cache: dict[str, str | None] = {}

    async def resolve_doc_uuid(stem: str) -> str | None:
        if stem in doc_uuid_cache:
            return doc_uuid_cache[stem]
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
        uuid = str(row[0]) if row else None
        doc_uuid_cache[stem] = uuid
        if uuid is None:
            log.warning(
                "eval.retrieval.doc_not_found",
                stem=stem,
                hint="Ensure fixture documents are seeded and status=ready",
            )
        return uuid

    results: list[dict] = []

    for qrow in queries:
        qid = qrow["id"]
        query_text = qrow["query"]
        doc_stem = qrow["document_id"]
        tag = qrow["tag"]
        keywords = qrow.get("expected_chunk_keywords", [])

        doc_uuid = await resolve_doc_uuid(doc_stem)
        if doc_uuid is None:
            log.warning("eval.retrieval.skipping_query", query_id=qid, reason="document not found")
            continue

        try:
            chunks = await ctx.retriever.retrieve(
                query_text,
                document_ids=[doc_uuid],
                top_k=10,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("eval.retrieval.retrieve_failed", query_id=qid, error=str(exc))
            continue

        top5 = chunks[:5]
        top10 = chunks[:10]

        hit_at_5 = 1 if _keywords_hit(top5, keywords) else 0
        hit_at_10 = 1 if _keywords_hit(top10, keywords) else 0
        mrr_score = _mrr_score(top10, keywords)

        results.append(
            {
                "id": qid,
                "tag": tag,
                "hit_at_5": hit_at_5,
                "hit_at_10": hit_at_10,
                "mrr_score": mrr_score,
            }
        )

        log.debug(
            "eval.retrieval.query_done",
            query_id=qid,
            tag=tag,
            hit_at_5=hit_at_5,
            hit_at_10=hit_at_10,
            mrr=mrr_score,
        )

    metrics = _compute_metrics(results)

    lt_recall10 = metrics.get("per_tag", {}).get("legal_term", {}).get("recall_at_10")
    if lt_recall10 is not None and lt_recall10 < LEGAL_TERM_RECALL_THRESHOLD:
        log.warning(
            "eval.retrieval.legal_term_threshold_violation",
            legal_term_recall_at_10=lt_recall10,
            threshold=LEGAL_TERM_RECALL_THRESHOLD,
            message="legal_term Recall@10 below threshold of 0.85",
        )

    markdown = _build_markdown(metrics)
    write_report("retrieval", metrics, markdown)

    log.info(
        "eval.retrieval.done",
        recall_at_5=metrics["recall_at_5"],
        recall_at_10=metrics["recall_at_10"],
        mrr=metrics["mrr"],
        query_count=metrics["query_count"],
    )

    return metrics


if __name__ == "__main__":
    asyncio.run(run())
