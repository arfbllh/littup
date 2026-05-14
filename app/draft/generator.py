"""SectionGenerator — Pass 2 of the draft engine: prose generation with citations."""
from __future__ import annotations
import re
from dataclasses import dataclass

import structlog

from app.draft.citations import parse_citations, strip_dangling
from app.llm.types import Message, SamplingParams
from app.db.models.chunk import Chunk

log = structlog.get_logger(__name__)

_SENTENCE_END_RE = re.compile(r'(?<=[.!?])\s+')


@dataclass
class SectionDraft:
    section_name: str
    text: str
    word_count: int
    dangling_citations: list[str]
    metadata: dict


@dataclass
class CitationDraft:
    section_name: str
    chunk_id: str
    claim_span_start: int
    claim_span_end: int


class SectionGenerator:
    def __init__(self, llm_router) -> None:
        self._router = llm_router
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self.cost_usd: float = 0.0
        self.model_used: str = "unknown"

    async def generate_all(
        self,
        template,
        fields: dict,
        retrieved: dict[str, list[Chunk]],
        fingerprint: str,
        few_shot: list,
        trace_id: str | None,
    ) -> tuple[list[SectionDraft], list[CitationDraft]]:
        all_chunk_ids: set[str] = set()
        for chunks in retrieved.values():
            for chunk in chunks:
                all_chunk_ids.add(chunk.id)

        all_sections: list[SectionDraft] = []
        all_citations: list[CitationDraft] = []

        for section_spec in template.sections:
            section_draft, citations = await self._generate_section(
                section_spec, template, fields, retrieved, all_chunk_ids, trace_id
            )
            all_sections.append(section_draft)
            all_citations.extend(citations)

        return all_sections, all_citations

    async def _generate_section(
        self, section_spec, template, fields, retrieved, all_chunk_ids, trace_id
    ) -> tuple[SectionDraft, list[CitationDraft]]:
        chunks = retrieved.get(section_spec.retrieval_key, [])

        system_content = template.system_prompt
        if template.appended_rules:
            rules_text = "\n".join(template.appended_rules)
            system_content = f"{system_content}\n\nAdditional rules:\n{rules_text}"

        system_msg = Message(role="system", content=system_content)

        evidence_lines = [f"[chunk:{c.id}] {c.text[:600]}" for c in chunks]
        evidence = "\n\n".join(evidence_lines) if evidence_lines else "No relevant chunks retrieved."

        fields_summary = {
            name: (ext.value if hasattr(ext, "value") else ext)
            for name, ext in fields.items()
        }

        user_content = (
            f"Section: {section_spec.name}\n"
            f"Description: {section_spec.description}\n"
            f"Target length: {section_spec.target_length_min}–{section_spec.target_length_max} words\n\n"
            f"Extracted fields:\n{_json_safe(fields_summary)}\n\n"
            f"Document evidence:\n{evidence}\n\n"
            "Instructions:\n"
            "- Write the section as flowing prose.\n"
            "- Cite every factual claim using [chunk:UUID] immediately after the claim.\n"
            "- Use only the chunk IDs shown in the evidence above.\n"
            "- If evidence is insufficient, write: \"Insufficient evidence in provided documents.\"\n"
        )
        user_msg = Message(role="user", content=user_content)

        response = await self._router.generate(
            [system_msg, user_msg],
            task="generation",
            sampling=SamplingParams(
                max_tokens=1200,
                temperature=0.2,
            ),
            trace_id=trace_id,
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

        raw_text = response.text or ""
        cleaned_text, dangling = strip_dangling(raw_text, all_chunk_ids)

        if dangling:
            log.warning(
                "generator.dangling_citations",
                section=section_spec.name,
                count=len(dangling),
            )

        # Length overrun check
        words = cleaned_text.split()
        word_count = len(words)
        max_words = section_spec.target_length_max
        if word_count > max_words * 1.5:
            cleaned_text = _truncate_at_sentence(cleaned_text, max_words)
            word_count = len(cleaned_text.split())
            log.info(
                "section.overrun",
                section=section_spec.name,
                original_words=len(words),
                truncated_words=word_count,
            )

        # Parse citations from cleaned text
        citation_spans = parse_citations(cleaned_text)
        citations = [
            CitationDraft(
                section_name=section_spec.name,
                chunk_id=cs.chunk_id,
                claim_span_start=cs.start,
                claim_span_end=cs.end,
            )
            for cs in citation_spans
        ]

        section_draft = SectionDraft(
            section_name=section_spec.name,
            text=cleaned_text,
            word_count=word_count,
            dangling_citations=dangling,
            metadata={"dangling_citation_count": len(dangling)},
        )

        return section_draft, citations


def _truncate_at_sentence(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text

    target = " ".join(words[:max_words])
    sentences = _SENTENCE_END_RE.split(text)

    result = ""
    for sentence in sentences:
        candidate = (result + " " + sentence).strip() if result else sentence
        if len(candidate.split()) > max_words:
            break
        result = candidate

    return result if result else target


def _json_safe(d: dict) -> str:
    import json
    try:
        return json.dumps(d, default=str, indent=2)
    except Exception:
        return str(d)
