# M8-DONE — Citation Validation

## What shipped

- **`app/draft/validator.py`** — `CitationValidator` with three-pass logic:
  1. Trivial pass: `chunk_id not in chunks_by_id` → `unsupported` (no LLM)
  2. Substring pass: high-signal terms (proper nouns, numbers, dates) → `substring_match` boost
  3. Semantic pass: batched LLM call (`task="validation"`, `temperature=0.0`, JSON schema enforced), up to 10 pairs per call
  - Defensive parsing: prefer `response.structured`, fall back to `json.loads(response.text)`, then mark all `unsupported` with reason `"validator response unparseable"`

- **`app/draft/citations.py`** — Added `Claim` dataclass and `segment_claims()`:
  - Splits section text on `[chunk:UUID]` boundaries
  - Trailing punctuation-only text filtered out (not treated as uncited claims)
  - Documented limit: simple `[.!?]\s+` sentence split; abbreviations like `Inc.` or `U.S.C.` can cause false boundaries — v1.1 fix

- **`app/draft/generator.py`** — `CitationDraft` gains `validation_status: str = "unchecked"` and `validation_reason: str | None = None`; `_generate_section` gains `extra_instructions: str | None` parameter

- **`app/draft/engine.py`** — Pass 3 inserted between Step 7 (template validators) and Step 8 (persist):
  - Validator runs per-section; sections with `cites_at_least_one` validator and `unsupported_count + contradicted_count > 0` are regenerated exactly once with stricter prompt
  - `_section_requires_citations()` helper reads existing `cites_at_least_one` validator config — no `SectionSpec` field added (NN-5 preserved)
  - `regenerate_section()` also runs validator one-shot (no nested retry)
  - Validator token/cost stats aggregated into draft totals

- **`app/db/migrations/versions/0004_draft_groundedness.py`** — `app.drafts.groundedness_score NUMERIC(4,3) NULL`

- **`app/db/models/draft.py`** — `Draft.groundedness_score` column

- **`app/draft/draft_repo.py`** — `finalize()` persists `validation_status` / `validation_reason` from `CitationDraft`, `groundedness_score`, and `sections_groundedness` in `ai_output`; `replace_section()` same, plus atomic `jsonb_set` to update per-section groundedness and recompute draft-level score

- **`app/api/schemas/drafts.py`** — `SectionView.groundedness: float | None`, `DraftResponse.groundedness_score: float | None`

- **`app/api/routes/drafts.py`** — Populated from `ai_output["sections_groundedness"]` and `Draft.groundedness_score`

## Deviations from plan

- `ValidationReport` uses `_ONLY_PUNCT_RE` filter in `segment_claims` to avoid trailing punctuation (`.`) becoming a spurious uncited claim — minor implementation detail not in plan
- `test_regenerate_endpoint_validates.py` tests `_apply_report_to_citations` at unit level rather than hitting the HTTP endpoint (would require a full DB + engine stack to wire properly; the integration test `test_validation_regenerate.py` covers the engine path end-to-end with mocks)

## Groundedness weighting decision

**Strict ratio: `supported / total_claims`.**

Partial does not count as half. Rationale: a partial citation is still presenting unverified content to the operator; treating it as 0.5 would inflate the score for drafts with many hedged claims. This is the conservative choice and keeps the score interpretable as "fraction of claims with full support."

## Sample draft JSON (all four validation statuses)

```json
{
  "draft_id": "...",
  "status": "ready",
  "groundedness_score": 0.5,
  "sections": [
    {
      "name": "background",
      "groundedness": 0.5,
      "citations": [
        {
          "chunk_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
          "validation_status": "supported",
          "validation_reason": "source directly states the claim"
        },
        {
          "chunk_id": "11111111-2222-3333-4444-555555555555",
          "validation_status": "partial",
          "validation_reason": "source partially addresses the claim"
        },
        {
          "chunk_id": "22222222-3333-4444-5555-666666666666",
          "validation_status": "unsupported",
          "validation_reason": "no overlap between claim and source"
        },
        {
          "chunk_id": "33333333-4444-5555-6666-777777777777",
          "validation_status": "contradicted",
          "validation_reason": "source states the opposite"
        }
      ]
    }
  ]
}
```

## NN compliance

- **NN-5**: `compute_fingerprint()` unaffected — no new `SectionSpec` fields; `_section_requires_citations` reads existing `cites_at_least_one` validator config
- **NN-7**: validator calls use `temperature=0.0` for deterministic LLM cache hits
- **NN-12**: all validator LLM calls logged via `LLMLogRepo` through `LLMRouter`
- No new SDK imports outside `app/llm/`
