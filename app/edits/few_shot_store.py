"""Few-shot store — index and retrieve past operator edits for in-context learning."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EditError
from app.settings import settings

logger = structlog.get_logger(__name__)

_WORK_MEM = "64MB"
_STMT_TIMEOUT = "5s"

# Max chars per search-repr line; total block limit
_LINE_MAX = 1500
_BLOCK_MAX = 3500


@dataclass(frozen=True)
class FewShotResult:
    edit_id: str
    ai_value: Any    # JSONB from the Edit row ({"value": ...} or {"text": ...})
    user_value: Any
    context_tags: list[str]
    distance: float
    prompt_fingerprint: str = ""


class FewShotStore:
    def __init__(self, embedder) -> None:
        if embedder.dim != 1024:
            raise EditError(
                f"Embedder dim {embedder.dim} != 1024; few-shot store requires 1024-dim embeddings",
                code="EMBEDDER_DIM_MISMATCH",
                status_code=500,
                retryable=False,
            )
        self._embedder = embedder

    async def index(self, edit_id: str, *, session: AsyncSession) -> None:
        """Embed an Edit row and stamp few_shot_indexed_at. Idempotent."""
        from sqlalchemy import select
        from app.db.models.edit import Edit

        result = await session.execute(select(Edit).where(Edit.id == edit_id))
        edit = result.scalar_one_or_none()
        if edit is None:
            logger.warning("few_shot.edit_not_found", edit_id=edit_id)
            return

        if edit.few_shot_indexed_at is not None:
            logger.info("few_shot.already_indexed", edit_id=edit_id)
            return

        ai_val, user_val = _extract_values(edit)
        context_tags = [edit.template_id, edit.field_type]
        repr_str = _search_repr(
            edit.field_or_section_name,
            edit.field_type,
            ai_val,
            user_val,
            context_tags,
        )

        vec = (await self._embedder.embed([repr_str]))[0]
        vec_str = "[" + ",".join(str(f) for f in vec) + "]"

        # Atomic update — guards concurrent worker race via WHERE few_shot_indexed_at IS NULL
        result = await session.execute(
            text(
                "UPDATE app.edits "
                "SET embedding = CAST(:vec AS vector), few_shot_indexed_at = NOW() "
                "WHERE id = :id AND few_shot_indexed_at IS NULL"
            ),
            {"vec": vec_str, "id": edit_id},
        )
        if result.rowcount == 0:
            logger.info("few_shot.concurrent_index_race", edit_id=edit_id)

    async def retrieve(
        self,
        template_id: str,
        field_or_section_name: str,
        *,
        session: AsyncSession,
        field_type: str = "field",
        chunk_context: str | None = None,
        top_k: int = 3,
    ) -> list[FewShotResult]:
        """Return top-k most similar past edits for this template+field."""
        query_repr = _search_repr(
            field_or_section_name,
            field_type,
            "<NEW DRAFT>",
            "",
            [template_id, field_type],
            chunk_context=chunk_context,
        )
        vec = (await self._embedder.embed([query_repr]))[0]
        qvec_str = "[" + ",".join(str(f) for f in vec) + "]"

        sql = """
            SELECT id, ai_value, user_value, context, prompt_fingerprint,
                   (embedding <=> CAST(:qvec AS vector)) AS distance
            FROM app.edits
            WHERE template_id = :tid
              AND field_or_section_name = :name
              AND few_shot_indexed_at IS NOT NULL
            ORDER BY embedding <=> CAST(:qvec AS vector)
            LIMIT :k
        """
        params = {"qvec": qvec_str, "tid": template_id, "name": field_or_section_name, "k": top_k}

        ctx = (
            session.begin_nested()
            if session.in_transaction()
            else session.begin()
        )
        async with ctx:
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {settings.HNSW_EF_SEARCH}"))
            await session.execute(text(f"SET LOCAL work_mem = '{_WORK_MEM}'"))
            await session.execute(text(f"SET LOCAL statement_timeout = '{_STMT_TIMEOUT}'"))
            result = await session.execute(text(sql), params)
            rows = result.fetchall()

        examples = []
        for row in rows:
            ctx_data = row.context or {}
            tags = ctx_data.get("context_tags", [template_id, field_type])
            examples.append(
                FewShotResult(
                    edit_id=str(row.id),
                    ai_value=row.ai_value,
                    user_value=row.user_value,
                    context_tags=tags,
                    distance=float(row.distance),
                    prompt_fingerprint=str(row.prompt_fingerprint or ""),
                )
            )
        return examples


def chunks_to_context(chunks: list) -> str | None:
    """Build a chunk-context string from the top-3 retrieved chunks."""
    if not chunks:
        return None
    parts = [chunk.text[:600] for chunk in chunks[:3]]
    return "\n\n".join(parts)


def _render_field_few_shot(examples: list[FewShotResult]) -> str:
    if not examples:
        return ""
    lines = ["\nPast operator corrections for this field:\n"]
    for i, ex in enumerate(examples, 1):
        ai_v = (ex.ai_value or {}).get("value", "")
        user_v = (ex.user_value or {}).get("value", "")
        ai_str = json.dumps(ai_v, default=str)[:600]
        user_str = json.dumps(user_v, default=str)[:600]
        lines.append(
            f"Example {i}:\n"
            f"  Previous extraction: {ai_str}\n"
            f"  Operator corrected to: {user_str}\n"
        )
    lines.append("\nPrefer the corrected form when the document supports it.")
    return "\n".join(lines)


def _render_section_few_shot(examples: list[FewShotResult]) -> str:
    if not examples:
        return ""
    lines = ["\nPast edits relevant to this section (most similar first):\n"]
    for i, ex in enumerate(examples, 1):
        ai_text = str((ex.ai_value or {}).get("text", ""))[:600]
        user_text = str((ex.user_value or {}).get("text", ""))[:600]
        lines.append(
            f'Example {i}:\n'
            f'  AI draft was:\n'
            f'    """{ai_text}"""\n'
            f'  Operator changed it to:\n'
            f'    """{user_text}"""\n'
        )
    lines.append(
        "\nApply these patterns when relevant. "
        "Do not invent citations; cite only chunks shown above."
    )
    return "\n".join(lines)


def _search_repr(
    field_or_section_name: str,
    field_type: str,
    ai_value: Any,
    user_value: Any,
    context_tags: list[str],
    chunk_context: str | None = None,
) -> str:
    """Build a symmetric search representation for indexing and retrieval.

    Pure function — same inputs always produce the same output.
    """
    label = f"[FIELD {field_or_section_name} | {field_type}]"
    is_section = field_type == "section"

    ai_str = _render_value(ai_value, is_section=is_section)
    user_str = _render_value(user_value, is_section=is_section)
    tags_str = ", ".join(str(t) for t in context_tags)

    lines = [
        label[:_LINE_MAX],
        f"AI: {ai_str}"[:_LINE_MAX],
        f"USER: {user_str}"[:_LINE_MAX],
        f"TAGS: {tags_str}"[:_LINE_MAX],
    ]
    if chunk_context is not None:
        lines.append(f"CONTEXT: {chunk_context}"[:_LINE_MAX])

    return "\n".join(lines)[:_BLOCK_MAX]


def _render_value(value: Any, *, is_section: bool = False) -> str:
    if value is None:
        return ""
    if is_section:
        return str(value)[:800]
    if isinstance(value, list):
        return "; ".join(_stringify_item(item) for item in value)
    return str(value).strip()


def _stringify_item(item: Any) -> str:
    if isinstance(item, dict):
        name = item.get("name", "")
        role = item.get("role", "")
        if role:
            return f"{name} ({role})"
        return name
    return str(item)


def _extract_values(edit: Any) -> tuple[Any, Any]:
    """Extract (ai_val, user_val) from an Edit row based on field_type."""
    if edit.field_type == "field":
        ai_val = (edit.ai_value or {}).get("value")
        user_val = (edit.user_value or {}).get("value")
    else:
        ai_val = (edit.ai_value or {}).get("text")
        user_val = (edit.user_value or {}).get("text")
    return ai_val, user_val
