"""FieldExtractor — Pass 1 of the draft engine: structured field extraction."""
from __future__ import annotations
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, create_model

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
    def __init__(self, llm_router) -> None:
        self._router = llm_router
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
        results: dict[str, FieldExtraction] = {}
        for field_spec in template.extraction_schema:
            results[field_spec.name] = await self._extract_field(
                field_spec, retrieved, fingerprint, trace_id
            )
        return results

    async def _extract_field(self, field_spec, retrieved, fingerprint, trace_id):
        chunks = retrieved.get(field_spec.retrieval_key, [])
        if not chunks:
            return FieldExtraction(value=None, supporting_chunk_ids=[], confidence=0.0)

        # Build dynamic Pydantic model for structured output
        DynamicModel = _build_extraction_model(field_spec.type)
        schema = DynamicModel.model_json_schema()

        # Build evidence context
        evidence_lines = []
        for chunk in chunks:
            evidence_lines.append(f"[chunk:{chunk.id}] {chunk.text[:500]}")
        evidence = "\n\n".join(evidence_lines)

        system_msg = Message(
            role="system",
            content=(
                f"Extract the field '{field_spec.name}' from the provided document excerpts.\n"
                f"Field description: {field_spec.description}\n"
                f"Return JSON matching the schema. Use null if not found."
            ),
        )
        user_msg = Message(
            role="user",
            content=(
                f"Document excerpts:\n{evidence}\n\n"
                f"Extract '{field_spec.name}' and list the chunk IDs that support your answer."
            ),
        )

        try:
            response = await self._router.generate(
                [system_msg, user_msg],
                task="extraction",
                schema=schema,
                sampling=SamplingParams(max_tokens=512, temperature=0.0),
                trace_id=trace_id,
            )
        except SchemaViolation:
            return FieldExtraction(
                value=None, supporting_chunk_ids=[], confidence=0.0, error_code="SCHEMA_VIOLATION"
            )

        # Accumulate cost/token stats from real LLM responses
        ti = getattr(response, 'tokens_in', None)
        to = getattr(response, 'tokens_out', None)
        cu = getattr(response, 'cost_usd', None)
        mu = getattr(response, 'model_used', None)
        if isinstance(ti, (int, float)):
            self.tokens_in += int(ti)
        if isinstance(to, (int, float)):
            self.tokens_out += int(to)
        if isinstance(cu, (int, float)):
            self.cost_usd += float(cu)
        if isinstance(mu, str):
            self.model_used = mu

        structured = response.structured or {}
        value = structured.get("value")
        supporting_ids = [str(c) for c in (structured.get("supporting_chunk_ids") or [])]
        confidence = float(structured.get("confidence", 0.8) or 0.8)

        # Substring sanity check for string scalars
        if isinstance(value, str) and value:
            norm_val = _normalize(value)
            found = any(norm_val in _normalize(c.text) for c in chunks)
            if not found:
                log.warning(
                    "extractor.substring_mismatch",
                    field=field_spec.name,
                    value=value[:80],
                )
                return FieldExtraction(
                    value=None, supporting_chunk_ids=[], confidence=0.0,
                    error_code="SUBSTRING_MISMATCH"
                )

        return FieldExtraction(
            value=value,
            supporting_chunk_ids=supporting_ids,
            confidence=confidence,
        )


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def _build_extraction_model(field_type: str) -> type[BaseModel]:
    """Build a Pydantic model for extracting a field of the given type."""
    if field_type == "string":
        value_type = str | None
    elif field_type == "date":
        value_type = str | None
    elif field_type == "money":
        value_type = str | None
    elif field_type in ("list[string]", "list[party]"):
        value_type = list[str] | None
    else:
        value_type = Any

    ExtractionModel = create_model(
        "ExtractionModel",
        value=(value_type, None),
        supporting_chunk_ids=(list[str], []),
        confidence=(float, 0.8),
    )
    return ExtractionModel
