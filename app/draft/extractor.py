"""FieldExtractor — Pass 1 of the draft engine: structured field extraction."""
from __future__ import annotations
import asyncio
import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.llm.errors import SchemaViolation
from app.llm.types import Message, SamplingParams
from app.db.models.chunk import Chunk

log = structlog.get_logger(__name__)


@dataclass
class FieldExtraction:
    value: Any
    supporting_chunk_ids: list[str]
    confidence: float
    error_code: str | None = None


class FieldExtractor:
    def __init__(
        self,
        llm_router,
        *,
        few_shot_store=None,
        current_template_id: str | None = None,
        session=None,
    ) -> None:
        self._router = llm_router
        self._few_shot_store = few_shot_store
        self._current_template_id = current_template_id
        self._session = session
        # Parallel extract_all shares one AsyncSession; serialize the brief
        # few-shot DB lookup so concurrent SQLAlchemy calls don't collide.
        # The slow LLM call still runs in parallel.
        self._session_lock = asyncio.Lock()
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self.cost_usd: float = 0.0
        self.model_used: str = "unknown"

    async def extract_all(
        self,
        template,  # DraftTemplate
        retrieved: dict[str, list[Chunk]],
        fingerprint: str,
        trace_id: str | None,
    ) -> dict[str, FieldExtraction]:
        """Extract every field in one LLM call.

        We send a union schema (one sub-object per field) plus per-field
        labeled evidence so the model can stay focused on each field while
        we save N-1 round trips.
        """
        all_specs = list(template.extraction_schema)
        if not all_specs:
            return {}

        per_field_chunks: dict[str, list[Chunk]] = {
            spec.name: retrieved.get(spec.retrieval_key, []) for spec in all_specs
        }

        # Fields without evidence skip the LLM entirely and get a null stub.
        # Saves tokens and keeps the prompt focused on answerable fields.
        specs = [s for s in all_specs if per_field_chunks[s.name]]
        results: dict[str, FieldExtraction] = {
            s.name: FieldExtraction(value=None, supporting_chunk_ids=[], confidence=0.0)
            for s in all_specs
            if not per_field_chunks[s.name]
        }
        if not specs:
            return results

        schema = _build_batch_schema(specs)

        # Per-field few-shot lookups — serialized via the session lock because
        # they all hit the same AsyncSession; each is fast (<50 ms).
        few_shot_blocks: dict[str, str] = {}
        if (
            self._few_shot_store is not None
            and self._session is not None
            and self._current_template_id is not None
        ):
            from app.edits.few_shot_store import (
                _render_field_few_shot,
                chunks_to_context,
            )
            from app.settings import settings as _settings

            for spec in specs:
                chunks = per_field_chunks[spec.name]
                if not chunks:
                    continue
                chunk_context = chunks_to_context(chunks)
                async with self._session_lock:
                    examples = await self._few_shot_store.retrieve(
                        self._current_template_id,
                        spec.name,
                        session=self._session,
                        field_type="field",
                        chunk_context=chunk_context,
                        top_k=_settings.FEW_SHOT_TOP_K,
                    )
                if examples:
                    few_shot_blocks[spec.name] = _render_field_few_shot(examples)

        # Build the prompt: one labeled section per field with its evidence.
        sections: list[str] = []
        for spec in specs:
            chunks = per_field_chunks[spec.name]
            evidence = "\n\n".join(
                f"[chunk:{c.id}] {c.text[:1000]}" for c in chunks
            )
            block = (
                f"## Field: {spec.name} ({spec.type})\n"
                f"Description: {spec.description}\n"
                f"Evidence:\n{evidence}"
            )
            if spec.name in few_shot_blocks:
                block += "\n\n" + few_shot_blocks[spec.name]
            sections.append(block)

        system_msg = Message(
            role="system",
            content=(
                "Extract the requested fields from the provided document excerpts.\n"
                "Each field is independent; only use the evidence in its own block.\n"
                "You MUST emit one entry per requested field — do not omit any field.\n"
                "If the evidence does not support a value, return null for that field.\n"
                "Return the value directly (matching the field's declared type), "
                "not wrapped in any object."
            ),
        )

        user_content = (
            "Extract the following fields. For each field, return the value "
            "directly (or null when unsupported). Do not wrap values in any "
            "object or stringify them.\n\n"
            + "\n\n".join(sections)
        )
        user_msg = Message(role="user", content=user_content)

        try:
            response = await self._router.generate(
                [system_msg, user_msg],
                task="extraction",
                schema=schema,
                sampling=SamplingParams(
                    max_tokens=max(1024, 512 * len(specs)),
                    temperature=0.0,
                ),
                trace_id=trace_id,
            )
        except SchemaViolation:
            log.warning("extractor.batch_schema_violation", fields=[s.name for s in specs])
            for spec in specs:
                results[spec.name] = FieldExtraction(
                    value=None,
                    supporting_chunk_ids=[],
                    confidence=0.0,
                    error_code="SCHEMA_VIOLATION",
                )
            return results

        ti = getattr(response, "tokens_in", None)
        to = getattr(response, "tokens_out", None)
        cu = getattr(response, "cost_usd", None)
        mu = getattr(response, "model_used", None)
        if isinstance(ti, (int, float)):
            self.tokens_in += int(ti)
        if isinstance(to, (int, float)):
            self.tokens_out += int(to)
        if isinstance(cu, (int, float)):
            self.cost_usd += float(cu)
        if isinstance(mu, str):
            self.model_used = mu

        structured = response.structured or {}
        results: dict[str, FieldExtraction] = {}
        for spec in specs:
            raw = structured.get(spec.name)
            results[spec.name] = self._coerce_field_result(
                spec, raw, per_field_chunks[spec.name]
            )
        return results

    def _coerce_field_result(
        self, spec, raw: Any, chunks: list[Chunk]
    ) -> FieldExtraction:
        """Normalize one field's slot from the batch response.

        The schema asks for the value directly (e.g., ``parties: [Party,...]``),
        but the model occasionally returns the legacy wrapper
        ``{"value": ..., "supporting_chunk_ids": [...], "confidence": ...}``
        or, under tool-use stringification, a JSON-encoded string. Recover
        from each shape.
        """
        # Unwrap legacy {value, supporting_chunk_ids, confidence} envelope.
        supporting_ids: list[str] = []
        confidence: float = 0.8
        if isinstance(raw, dict) and "value" in raw and not (
            spec.type == "list[party]" and "name" in raw
        ):
            value = raw.get("value")
            supporting_ids = [
                str(c) for c in (raw.get("supporting_chunk_ids") or [])
            ]
            confidence = float(raw.get("confidence", 0.8) or 0.8)
        else:
            value = raw

        # Tool-use sometimes JSON-stringifies list/object values.
        if isinstance(value, str) and spec.type in ("list[string]", "list[party]"):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                pass
        if isinstance(value, str) and value.strip().lower() == "null":
            value = None

        if value is None:
            return FieldExtraction(
                value=None,
                supporting_chunk_ids=[],
                confidence=0.0,
            )

        # Default the supporting chunks to all retrieved chunks if the model
        # didn't volunteer any — every field block was retrieved from `chunks`.
        if not supporting_ids:
            supporting_ids = [str(c.id) for c in chunks]

        # Grounding check only on plain-text `string` fields. Dates ("April 28,
        # 2025" → "2025-04-28") and money ("$2.45M" → "$2,450,034.62") get
        # normalized by the model, so a strict substring match produces false
        # negatives. The citation validator (Pass 3) is the real grounding gate.
        if spec.type == "string" and isinstance(value, str) and value:
            if not _has_grounding(value, chunks):
                log.warning(
                    "extractor.substring_mismatch",
                    field=spec.name,
                    value=value[:80],
                )
                return FieldExtraction(
                    value=None,
                    supporting_chunk_ids=[],
                    confidence=0.0,
                    error_code="SUBSTRING_MISMATCH",
                )

        return FieldExtraction(
            value=value,
            supporting_chunk_ids=supporting_ids,
            confidence=confidence,
        )


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def _has_grounding(value: str, chunks: list[Chunk]) -> bool:
    """Looser grounding for `string` fields.

    Accept either an exact (normalized) substring match, OR ≥60% of the
    value's non-trivial tokens appearing in some chunk. The strict-substring
    rule rejected valid extractions where the model concatenated header +
    subdivision (e.g., "Circuit Court of Cook County, Illinois — Law
    Division") or composed a property description from multiple paragraphs.
    """
    norm_val = _normalize(value)
    if not norm_val:
        return True
    chunk_texts = [_normalize(c.text) for c in chunks]
    if any(norm_val in t for t in chunk_texts):
        return True
    tokens = [tok for tok in norm_val.split() if len(tok) > 2]
    if not tokens:
        return True
    for t in chunk_texts:
        hits = sum(1 for tok in tokens if tok in t)
        if hits / len(tokens) >= 0.6:
            return True
    return False


def _value_schema(field_type: str) -> dict[str, Any]:
    """JSON Schema for one field's ``value``, inlined (no $refs).

    Pydantic-generated schemas use ``$defs`` / ``$ref`` for nested models, and
    Anthropic's tool-use validator handles those poorly — when every field is
    required, the model satisfies the schema by emitting *stringified* values
    instead of nested objects. Hand-rolling the schema with everything inline
    sidesteps that.
    """
    if field_type == "list[party]":
        return {
            "anyOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                        },
                        "required": ["name", "role"],
                    },
                },
                {"type": "null"},
            ]
        }
    if field_type == "list[string]":
        return {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "null"},
            ]
        }
    # string, date, money — all carried as a nullable string at the wire level
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _build_batch_schema(specs) -> dict[str, Any]:
    """Inline JSON Schema for the batch extraction call.

    Flat shape: ``{field_a: value_or_null, field_b: value_or_null, ...}``.
    The original design wrapped each value in ``{value, supporting_chunk_ids,
    confidence}`` but Anthropic's tool-use validator handles the nested
    object weakly — it accepted JSON-stringified values for the inner
    ``value`` field, producing flat strings where we expected structured
    objects. Eliminating the wrapper removes the failure mode entirely;
    supporting chunks default to the retrieved set, which is what we'd cite
    anyway.
    """
    props: dict[str, Any] = {spec.name: _value_schema(spec.type) for spec in specs}
    return {
        "type": "object",
        "properties": props,
        "required": [spec.name for spec in specs],
    }
