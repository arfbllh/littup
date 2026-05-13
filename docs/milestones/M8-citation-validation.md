# M8 — Citation Validation

**Estimated time:** 1.5 hours
**Dependencies:** M7
**Rubric impact:** Retrieval and Grounding (back half of 25 pts — "supporting evidence can be inspected", "unsupported generation is controlled")

## Goal

Every claim in a generated draft is checked against the chunk it cites. The system tags claims as `supported`, `partial`, `unsupported`, or `contradicted`. Unsupported claims are flagged in the draft response and rendered specially in the UI. The reviewer can demonstrate this in one click.

## Context Claude Code must read

1. `docs/architecture/03-components/draft-engine.md` — citation validation section
2. `docs/architecture/10-fixes-and-non-negotiables.md`

## Non-Negotiables that apply

None new; reuses NN-5 (fingerprint flow) and NN-12 (every validator call writes to LLM log).

## Files to create / modify

### Validator

- `app/draft/validator.py` — `CitationValidator`:
  - `async validate_section(section_text, citations: list[Citation], chunks_by_id, fingerprint) -> ValidationReport`
  - For each (claim sentence, cited chunks) pair:
    1. **Trivial pass**: chunk_id exists in retrieved set
    2. **Substring pass**: high-signal terms from the claim (proper nouns, numbers, dates) appear in chunk text → confidence boost
    3. **Semantic pass**: batch up to 10 (claim, chunk) pairs into a single LLM call (`task="validation"`); prompt:
       ```
       For each pair, reply with one of: supported | partial | unsupported | contradicted.
       Return JSON: [{"pair_idx": 0, "status": "...", "reason": "..."}, ...]
       
       Pair 0:
       SOURCE: """{chunk.text}"""
       CLAIM:  """{claim_text}"""
       
       Pair 1: ...
       ```
    4. Final status: combine trivial + substring + semantic; conservative — if semantic says `unsupported`, that wins
  - Returns `ValidationReport(per_claim: list[ClaimValidation], unsupported_count, partial_count, supported_count, total_claims)`

### Claim segmentation

- `app/draft/citations.py` — `segment_claims(section_text, citations) -> list[Claim]`:
  - Splits section text into claim spans: a claim is "the text from the start of a sentence (or end of previous citation) up to the next citation bracket"
  - Each claim has: `text`, `cited_chunk_ids`, `char_span`
  - Sentences without citations also become claims (their `cited_chunk_ids` is empty — these are auto-marked `unsupported`)

### Engine integration

- `app/draft/engine.py` — extend `generate()`:
  - After section generation, run `CitationValidator.validate_section` per section
  - Persist `Citation.validation_status` and `validation_reason`
  - If `unsupported_count > 0` for a required-citation section, optionally regenerate that section once with a stricter prompt: "Your previous output had {N} unsupported claims. Use only the evidence provided. Cite or remove unsupported statements." After one regenerate attempt, surface what's left to the operator.
  - Compute `Draft.groundedness_score = supported / total_claims` and persist

### API response shape

- `Section` response includes `validation` block:
  ```json
  {
    "text": "...",
    "citations": [
      {"chunk_id": "...", "char_span": [120, 145], "validation_status": "supported", "reason": null},
      {"chunk_id": "...", "char_span": [200, 230], "validation_status": "unsupported", "reason": "no overlap"}
    ],
    "groundedness": 0.83
  }
  ```

### Tests

- `tests/unit/test_claim_segmentation.py` — given a section with mixed cited and uncited sentences, assert correct claim spans
- `tests/integration/test_citation_validator_supported.py` — mock router returns `supported`; assert all claims marked supported; groundedness = 1.0
- `tests/integration/test_citation_validator_unsupported.py` — generate a draft where one section deliberately contains a hallucination (use `MockProvider` to return text citing a real chunk_id but with content the chunk doesn't support); assert the validator catches it
- `tests/integration/test_validation_regenerate.py` — a section with `unsupported_count > 0` triggers exactly one regeneration attempt
- `tests/integration/test_groundedness_score.py` — given 10 claims, 7 supported / 2 partial / 1 unsupported → groundedness = 0.7 (or whatever weighting we pick; document it)

## Acceptance criteria

- [ ] Every draft response includes per-citation validation status
- [ ] Unsupported claims trigger one regeneration attempt
- [ ] `groundedness_score` persisted on `Draft`
- [ ] Validator handles a fabricated `chunk_id` gracefully (status = `unsupported`, reason = "chunk_id not in retrieved set")
- [ ] Validator handles sentences with no citation (status = `unsupported`, reason = "claim has no citation")

## Out of scope

- Multi-pass validation (per word vs per claim) — sentence-level is enough
- Auto-rewriting unsupported claims into "Not stated in the documents" — operator's job
- Numeric/date-specific validators (e.g., "does the cited chunk literally contain the date '2024-03-14'") — v1.1 if eval reveals weakness

## Definition of done

The reviewer can click a citation in the UI, see the source chunk, and see whether it's marked supported. The validator's effect is visible in the response JSON. `M8-DONE.md` written with a sample draft showing all four validation statuses.

## Sub-agent delegation

Not really — claim segmentation, validator, and engine integration are tightly coupled. Sequential build.
