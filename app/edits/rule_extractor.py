"""Stage-2 edit loop: offline rule extractor (M10).

Groups recent edits per template, asks the analysis-tier LLM for a generalizable
rule, dedups against existing appended_rules, and (on novel rule) inserts a new
TemplateVersion. Advisory lock via direct connection prevents concurrent runs.
"""
from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import settings

logger = structlog.get_logger(__name__)

_MAX_CHARS = 400

RULE_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "rule": {"type": "string"},
        "evidence_count": {"type": "integer"},
        "rationale": {"type": "string"},
    },
    "required": ["rule", "evidence_count", "rationale"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """\
You are a careful technical editor reviewing how an experienced legal operator
corrects an AI draft. Your task is to extract ONE generalizable, instruction-shaped
rule that explains a consistent edit pattern across multiple cases, or to declare
that no consistent pattern exists.

Output JSON with exactly this shape:
{"rule": "<one sentence imperative, OR the literal string NO_RULE>",
 "evidence_count": <integer: cases the rule applies to>,
 "rationale": "<one sentence>"}

Output the JSON object and nothing else. No prose, no markdown fences."""


@dataclass(frozen=True)
class ExtractionResult:
    template_id: str
    skipped_reason: Literal["locked", "no_template", "no_edits", None]
    edits_processed: int
    groups_evaluated: int
    groups_skipped_min_edits: int
    new_rules: list[str]
    new_version: int | None
    prompt_fingerprint: str
    trace_id: str | None


class RuleExtractor:
    def __init__(
        self,
        session: AsyncSession,
        lock_session: AsyncSession,
        registry: Any,
        llm_router: Any,
        embedder: Any,
        settings_obj: Any = None,
    ) -> None:
        self._session = session
        self._lock_session = lock_session
        self._registry = registry
        self._router = llm_router
        self._embedder = embedder
        self._settings = settings_obj or settings

    async def run(
        self,
        template_id: str,
        *,
        since: datetime | None = None,
        trace_id: str | None = None,
    ) -> ExtractionResult:
        s = self._settings
        locked = await _try_acquire(self._lock_session, s.RULE_EXTRACTOR_LOCK_ID)
        if not locked:
            logger.info("rule_extractor.lock_busy", template_id=template_id, trace_id=trace_id)
            return ExtractionResult(
                template_id=template_id,
                skipped_reason="locked",
                edits_processed=0,
                groups_evaluated=0,
                groups_skipped_min_edits=0,
                new_rules=[],
                new_version=None,
                prompt_fingerprint="",
                trace_id=trace_id,
            )

        try:
            return await self._run_locked(template_id, since=since, trace_id=trace_id)
        finally:
            await _release(self._lock_session, s.RULE_EXTRACTOR_LOCK_ID)

    async def _run_locked(
        self,
        template_id: str,
        *,
        since: datetime | None,
        trace_id: str | None,
    ) -> ExtractionResult:
        s = self._settings
        session = self._session

        # Load extractor state (None on first run).
        state_row = (
            await session.execute(
                text(
                    "SELECT last_run_at, last_edit_id, edits_processed, rules_added "
                    "FROM app.template_extractor_state WHERE template_id = :tid"
                ),
                {"tid": template_id},
            )
        ).fetchone()

        effective_since: datetime
        if since is not None:
            effective_since = since
        elif state_row and state_row[0]:
            effective_since = state_row[0]
        else:
            effective_since = datetime.now(timezone.utc) - timedelta(
                days=s.RULE_EXTRACTOR_FIRST_RUN_LOOKBACK_DAYS
            )

        # Capture the run timestamp before querying edits so any edit committed
        # between the query and _upsert_state is counted in the next run's window.
        run_started_at = datetime.now(timezone.utc)

        # Snapshot the template.
        try:
            template = await self._registry.get_latest(template_id, session=session)
        except Exception:
            logger.info("rule_extractor.no_template", template_id=template_id, trace_id=trace_id)
            return ExtractionResult(
                template_id=template_id,
                skipped_reason="no_template",
                edits_processed=0,
                groups_evaluated=0,
                groups_skipped_min_edits=0,
                new_rules=[],
                new_version=None,
                prompt_fingerprint="",
                trace_id=trace_id,
            )

        pre_run_fp = template.compute_fingerprint()
        v_floor = max(1, template.version - s.RULE_EXTRACTOR_VERSION_LOOKBACK)

        # Pull recent edits.
        edits_rows = (
            await session.execute(
                text(
                    "SELECT id, field_or_section_name, field_type, ai_value, user_value, "
                    "template_version, created_at "
                    "FROM app.edits "
                    "WHERE template_id = :tid "
                    "  AND created_at > :since "
                    "  AND template_version >= :v_floor "
                    "ORDER BY created_at DESC"
                ),
                {"tid": template_id, "since": effective_since, "v_floor": v_floor},
            )
        ).fetchall()

        if not edits_rows:
            logger.info("rule_extractor.no_edits", template_id=template_id, trace_id=trace_id)
            await _upsert_state(session, template_id, None, state_row, 0, 0, run_started_at=run_started_at)
            return ExtractionResult(
                template_id=template_id,
                skipped_reason="no_edits",
                edits_processed=0,
                groups_evaluated=0,
                groups_skipped_min_edits=0,
                new_rules=[],
                new_version=None,
                prompt_fingerprint=pre_run_fp,
                trace_id=trace_id,
            )

        # Group by (field_or_section_name, field_type).
        groups: dict[tuple[str, str], list] = {}
        for row in edits_rows:
            key = (row.field_or_section_name, row.field_type)
            groups.setdefault(key, []).append(row)

        groups_evaluated = 0
        groups_skipped = 0
        new_rules: list[str] = []
        min_edits = s.RULE_EXTRACTOR_MIN_EDITS
        max_per_group = s.RULE_EXTRACTOR_MAX_EDITS_PER_GROUP

        for (name, field_type), group_rows in groups.items():
            if len(group_rows) < min_edits:
                groups_skipped += 1
                continue

            groups_evaluated += 1
            cases = group_rows[:max_per_group]
            n = len(cases)
            min_evidence = max(2, math.ceil(n * 0.6))

            messages = _build_analysis_prompt(name, field_type, cases, min_evidence)
            try:
                response = await self._router.generate(
                    messages,
                    task="analysis",
                    schema=RULE_EXTRACTION_SCHEMA,
                    trace_id=trace_id,
                )
            except Exception as exc:
                logger.warning(
                    "rule_extractor.llm_failed",
                    template_id=template_id,
                    field=name,
                    error=str(exc),
                    trace_id=trace_id,
                )
                continue

            rule = _parse_rule(response, min_evidence=min_evidence, trace_id=trace_id)
            if rule is None:
                continue

            existing = list(template.appended_rules) + new_rules
            similar = await _is_similar(
                rule,
                existing,
                embedder=self._embedder,
                threshold=s.RULE_EXTRACTOR_SIMILARITY_THRESHOLD,
            )
            if similar:
                logger.info(
                    "rule_extractor.rule_duplicate",
                    template_id=template_id,
                    field=name,
                    trace_id=trace_id,
                )
                continue

            new_rules.append(rule)
            logger.info(
                "rule_extractor.rule_accepted",
                template_id=template_id,
                field=name,
                rule=rule[:100],
                trace_id=trace_id,
            )

        new_version: int | None = None
        post_run_fp = pre_run_fp

        if new_rules:
            new_template = await self._registry.append_rules(
                template_id, new_rules, session=session
            )
            new_version = new_template.version
            post_run_fp = new_template.compute_fingerprint()
            logger.info(
                "rule_extractor.version_bumped",
                template_id=template_id,
                new_version=new_version,
                rules_added=len(new_rules),
                trace_id=trace_id,
            )

        last_edit_id = str(edits_rows[0].id) if edits_rows else (
            state_row[1] if state_row else None
        )
        await _upsert_state(
            session, template_id, last_edit_id, state_row, len(edits_rows), len(new_rules),
            run_started_at=run_started_at,
        )

        logger.info(
            "rule_extractor.run_complete",
            template_id=template_id,
            edits_processed=len(edits_rows),
            groups_evaluated=groups_evaluated,
            groups_skipped_min_edits=groups_skipped,
            new_rules=len(new_rules),
            new_version=new_version,
            trace_id=trace_id,
        )

        return ExtractionResult(
            template_id=template_id,
            skipped_reason=None,
            edits_processed=len(edits_rows),
            groups_evaluated=groups_evaluated,
            groups_skipped_min_edits=groups_skipped,
            new_rules=new_rules,
            new_version=new_version,
            prompt_fingerprint=post_run_fp,
            trace_id=trace_id,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _render_case(edit_row: Any, max_chars: int = _MAX_CHARS) -> tuple[str, str]:
    """Return (ai_repr, user_repr) for a single edit row."""
    ai_raw = edit_row.ai_value or {}
    user_raw = edit_row.user_value or {}

    field_type = edit_row.field_type
    is_section = field_type == "section"

    if is_section:
        ai_val = ai_raw.get("text", "")
        user_val = user_raw.get("text", "")
    else:
        ai_val = ai_raw.get("value", "")
        user_val = user_raw.get("value", "")

    def _fmt(v: Any) -> str:
        if v is None:
            return ""
        s = unicodedata.normalize("NFC", str(v)).strip()
        return s[:max_chars]

    return _fmt(ai_val), _fmt(user_val)


def _build_analysis_prompt(
    name: str,
    field_type: str,
    cases: list,
    min_evidence: int,
) -> list[dict]:
    user_lines = [
        f"Field or section: {name}",
        f"Type: {field_type}",
        "",
        f"Cases ({len(cases)}):",
    ]
    for i, row in enumerate(cases, 1):
        ai_repr, user_repr = _render_case(row)
        user_lines.append(f"{i}. AI produced: {ai_repr}")
        user_lines.append(f"   Operator changed to: {user_repr}")

    user_lines += [
        "",
        "Look for a CONSISTENT pattern. The rule must be imperative (e.g., \"When listing",
        "parties, always include their role in parentheses.\"). If fewer than "
        f"{min_evidence} of the cases support a single rule, return NO_RULE.",
    ]

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def _parse_rule(response: Any, *, min_evidence: int, trace_id: str | None) -> str | None:
    text_body = getattr(response, "text", None) or ""
    try:
        obj = json.loads(text_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("rule_extractor.parse_failed", reason="json_decode", trace_id=trace_id)
        return None

    if not isinstance(obj, dict):
        logger.warning("rule_extractor.parse_failed", reason="not_object", trace_id=trace_id)
        return None

    rule = obj.get("rule", "")
    if not rule or not isinstance(rule, str):
        logger.warning("rule_extractor.parse_failed", reason="missing_rule", trace_id=trace_id)
        return None

    rule = rule.strip()
    if rule == "NO_RULE":
        logger.info("rule_extractor.no_rule", trace_id=trace_id)
        return None

    if not rule:
        logger.warning("rule_extractor.parse_failed", reason="empty_rule", trace_id=trace_id)
        return None

    evidence = obj.get("evidence_count", 0)
    if not isinstance(evidence, int) or evidence < min_evidence:
        logger.warning(
            "rule_extractor.evidence_insufficient",
            evidence_count=evidence,
            min_evidence=min_evidence,
            trace_id=trace_id,
        )
        return None

    return rule


async def _is_similar(
    rule: str,
    existing: list[str],
    *,
    embedder: Any,
    threshold: float,
) -> bool:
    if not existing:
        return False

    rule_cf = rule.casefold().strip()
    for ex in existing:
        if rule_cf == ex.casefold().strip():
            return True

    texts = [rule] + existing
    vecs = await embedder.embed(texts)

    rule_vec = vecs[0]
    rule_norm = _norm(rule_vec)
    if rule_norm == 0:
        return False

    for ex_vec in vecs[1:]:
        ex_norm = _norm(ex_vec)
        if ex_norm == 0:
            continue
        cos = sum(a * b for a, b in zip(rule_vec, ex_vec)) / (rule_norm * ex_norm)
        if cos >= threshold:
            return True

    return False


def _norm(vec: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vec))


async def _try_acquire(lock_session: AsyncSession, lock_id: int) -> bool:
    result = await lock_session.execute(
        text("SELECT pg_try_advisory_lock(:k)"),
        {"k": lock_id},
    )
    return bool(result.scalar())


async def _release(lock_session: AsyncSession, lock_id: int) -> None:
    await lock_session.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": lock_id},
    )


async def _upsert_state(
    session: AsyncSession,
    template_id: str,
    last_edit_id: str | None,
    existing_state: Any,
    edits_processed: int,
    rules_added: int,
    *,
    run_started_at: datetime,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO app.template_extractor_state
                (template_id, last_run_at, last_edit_id, edits_processed, rules_added, updated_at)
            VALUES
                (:tid, :run_started_at, :last_edit_id, :edits_processed, :rules_added, :run_started_at)
            ON CONFLICT (template_id) DO UPDATE
              SET last_run_at    = EXCLUDED.last_run_at,
                  last_edit_id   = COALESCE(EXCLUDED.last_edit_id, template_extractor_state.last_edit_id),
                  edits_processed = template_extractor_state.edits_processed + EXCLUDED.edits_processed,
                  rules_added    = template_extractor_state.rules_added + EXCLUDED.rules_added,
                  updated_at     = EXCLUDED.updated_at
            """
        ),
        {
            "tid": template_id,
            "run_started_at": run_started_at,
            "last_edit_id": last_edit_id,
            "edits_processed": edits_processed,
            "rules_added": rules_added,
        },
    )
