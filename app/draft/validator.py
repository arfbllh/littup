"""CitationValidator — Pass 3 of the draft engine: per-claim grounding check."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import structlog

from app.draft.citations import Claim, segment_claims
from app.llm.types import Message, SamplingParams

log = structlog.get_logger(__name__)

# JSON schema for batch validation LLM call
_VALIDATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair_idx": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["supported", "partial", "unsupported", "contradicted"],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["pair_idx", "status"],
            },
        }
    },
    "required": ["results"],
}

_SYSTEM_PROMPT = (
    "You are a citation validator. For each SOURCE/CLAIM pair, determine whether "
    "the SOURCE text supports the CLAIM.\n"
    "Reply with one of: supported | partial | unsupported | contradicted.\n"
    "- supported: the source directly supports the claim\n"
    "- partial: the source partially supports the claim but is incomplete\n"
    "- unsupported: the source does not support the claim at all\n"
    "- contradicted: the source contradicts the claim\n"
    'Return JSON: {"results": [{"pair_idx": 0, "status": "...", "reason": "..."}, ...]}'
)

# Regex for high-signal terms: capitalised tokens, numbers, dates
_PROPER_NOUN_RE = re.compile(r'\b[A-Z][a-zA-Z]{2,}\b')
_NUMBER_RE = re.compile(r'\b\d[\d,\.]*\b')
_DATE_RE = re.compile(r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b')

_BATCH_SIZE = 10


@dataclass
class ClaimValidation:
    claim: Claim
    status: str  # supported | partial | unsupported | contradicted
    reason: str | None = None
    substring_match: bool = False


@dataclass
class ValidationReport:
    per_claim: list[ClaimValidation] = field(default_factory=list)

    @property
    def total_claims(self) -> int:
        return len(self.per_claim)

    @property
    def supported_count(self) -> int:
        return sum(1 for cv in self.per_claim if cv.status == "supported")

    @property
    def partial_count(self) -> int:
        return sum(1 for cv in self.per_claim if cv.status == "partial")

    @property
    def unsupported_count(self) -> int:
        return sum(1 for cv in self.per_claim if cv.status == "unsupported")

    @property
    def contradicted_count(self) -> int:
        return sum(1 for cv in self.per_claim if cv.status == "contradicted")


def _extract_high_signal_terms(text: str) -> list[str]:
    terms: list[str] = []
    terms.extend(_PROPER_NOUN_RE.findall(text))
    terms.extend(_DATE_RE.findall(text))
    terms.extend(_NUMBER_RE.findall(text))
    return terms


def _merge_statuses(statuses: list[str]) -> str:
    """Conservative merge: contradicted > unsupported > partial > supported."""
    if not statuses:
        return "unsupported"
    if "contradicted" in statuses:
        return "contradicted"
    if "unsupported" in statuses:
        return "unsupported"
    if "partial" in statuses:
        return "partial"
    return "supported"


class CitationValidator:
    def __init__(self, llm_router) -> None:
        self._router = llm_router
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self.cost_usd: float = 0.0
        self.model_used: str = "unknown"

    async def validate_section(
        self,
        section_text: str,
        citations_for_section: list,
        chunks_by_id: dict,
        fingerprint: str,
        trace_id: str | None = None,
    ) -> ValidationReport:
        log.debug("citation_validator.start", fingerprint=fingerprint, trace_id=trace_id)
        claims = segment_claims(section_text, citations_for_section)
        report = ValidationReport()

        if not claims:
            return report

        # pairs that need an LLM semantic pass: (claim_idx, chunk_id)
        pending_pairs: list[tuple[int, str]] = []
        # per-claim intermediate state
        claim_results: list[dict] = [
            {"statuses": [], "reasons": [], "substring_match": False}
            for _ in claims
        ]

        for idx, claim in enumerate(claims):
            if not claim.cited_chunk_ids:
                # No citation → auto-unsupported, no LLM call
                claim_results[idx]["statuses"].append("unsupported")
                claim_results[idx]["reasons"].append("claim has no citation")
                continue

            for chunk_id in claim.cited_chunk_ids:
                if chunk_id not in chunks_by_id:
                    claim_results[idx]["statuses"].append("unsupported")
                    claim_results[idx]["reasons"].append("chunk_id not in retrieved set")
                    continue

                chunk = chunks_by_id[chunk_id]
                chunk_text = chunk.text if hasattr(chunk, "text") else str(chunk)

                # Substring pass
                high_signal = _extract_high_signal_terms(claim.text)
                hit = any(term in chunk_text for term in high_signal)
                if hit:
                    claim_results[idx]["substring_match"] = True

                pending_pairs.append((idx, chunk_id))

        # Semantic pass — batch in groups of _BATCH_SIZE
        if pending_pairs:
            await self._run_semantic_pass(
                claims, chunks_by_id, pending_pairs, claim_results, trace_id
            )

        # Build final per-claim validations
        for idx, claim in enumerate(claims):
            state = claim_results[idx]
            statuses = state["statuses"]
            reasons = state["reasons"]

            if not statuses:
                # All pairs were queued for semantic and something went wrong
                status = "unsupported"
                reason = "no validation result"
            else:
                status = _merge_statuses(statuses)
                # Use the first reason that matches the merged status
                matched_reasons = [r for s, r in zip(statuses, reasons) if s == status and r]
                reason = matched_reasons[0] if matched_reasons else (reasons[0] if reasons else None)

            report.per_claim.append(ClaimValidation(
                claim=claim,
                status=status,
                reason=reason,
                substring_match=state["substring_match"],
            ))

        return report

    async def _run_semantic_pass(
        self,
        claims: list[Claim],
        chunks_by_id: dict,
        pending_pairs: list[tuple[int, str]],
        claim_results: list[dict],
        trace_id: str | None,
    ) -> None:
        # Chunk pairs into batches of _BATCH_SIZE
        for batch_start in range(0, len(pending_pairs), _BATCH_SIZE):
            batch = pending_pairs[batch_start : batch_start + _BATCH_SIZE]
            await self._call_llm_batch(claims, chunks_by_id, batch, claim_results, trace_id)

    async def _call_llm_batch(
        self,
        claims: list[Claim],
        chunks_by_id: dict,
        batch: list[tuple[int, str]],
        claim_results: list[dict],
        trace_id: str | None,
    ) -> None:
        user_lines: list[str] = []
        for local_idx, (claim_idx, chunk_id) in enumerate(batch):
            chunk = chunks_by_id[chunk_id]
            chunk_text = chunk.text if hasattr(chunk, "text") else str(chunk)
            claim_text = claims[claim_idx].text
            user_lines.append(
                f'Pair {local_idx}:\nSOURCE: """{chunk_text[:800]}"""\nCLAIM:  """{claim_text}"""'
            )

        user_content = "\n\n".join(user_lines)
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=user_content),
        ]

        try:
            response = await self._router.generate(
                messages,
                task="validation",
                schema=_VALIDATION_SCHEMA,
                sampling=SamplingParams(max_tokens=1024, temperature=0.0),
                trace_id=trace_id,
            )
            self._accumulate_stats(response)

            # Parse response — prefer structured, fall back to json.loads(text)
            parsed = None
            if response.structured:
                parsed = response.structured
            elif response.text:
                try:
                    parsed = json.loads(response.text)
                except json.JSONDecodeError:
                    pass

            if parsed is None or "results" not in parsed:
                raise ValueError("unparseable validator response")

            results_map = {r["pair_idx"]: r for r in parsed["results"] if "pair_idx" in r and "status" in r}

            for local_idx, (claim_idx, chunk_id) in enumerate(batch):
                entry = results_map.get(local_idx)
                if entry:
                    claim_results[claim_idx]["statuses"].append(entry["status"])
                    claim_results[claim_idx]["reasons"].append(entry.get("reason") or "")
                else:
                    claim_results[claim_idx]["statuses"].append("unsupported")
                    claim_results[claim_idx]["reasons"].append("validator response unparseable")

        except Exception as exc:
            log.warning(
                "citation_validator.batch_failed",
                trace_id=trace_id,
                error=str(exc),
            )
            for claim_idx, _chunk_id in batch:
                claim_results[claim_idx]["statuses"].append("unsupported")
                claim_results[claim_idx]["reasons"].append("validator response unparseable")

    def _accumulate_stats(self, response) -> None:
        ti = getattr(response, "tokens_in", None)
        to_ = getattr(response, "tokens_out", None)
        cu = getattr(response, "cost_usd", None)
        mu = getattr(response, "model_used", None)
        if isinstance(ti, (int, float)):
            self.tokens_in += int(ti)
        if isinstance(to_, (int, float)):
            self.tokens_out += int(to_)
        if isinstance(cu, (int, float)):
            self.cost_usd += float(cu)
        if isinstance(mu, str):
            self.model_used = mu


def _apply_report_to_citations(report: ValidationReport, citations: list) -> None:
    """Annotate CitationDraft instances in-place using per-claim validation results.

    Each citation is matched to the first ClaimValidation whose cited_chunk_ids
    contain that citation's chunk_id.
    """
    chunk_to_status: dict[str, tuple[str, str | None]] = {}
    for cv in report.per_claim:
        for chunk_id in cv.claim.cited_chunk_ids:
            if chunk_id not in chunk_to_status:
                chunk_to_status[chunk_id] = (cv.status, cv.reason)

    for cit in citations:
        if cit.chunk_id in chunk_to_status:
            status, reason = chunk_to_status[cit.chunk_id]
            cit.validation_status = status
            cit.validation_reason = reason
        else:
            # chunk_id never appeared in any claim — mark unsupported
            cit.validation_status = "unsupported"
            cit.validation_reason = "chunk_id not matched to any claim"
