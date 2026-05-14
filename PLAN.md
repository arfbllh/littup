# PLAN — M8 Citation Validation

**Spec:** `docs/milestones/M8-citation-validation.md`
**Depends on:** M7 (shipped) — `DraftEngine.generate()` already produces sections with `[chunk:UUID]` citations and persists `Citation` rows with `validation_status='unchecked'`.
**Rubric:** Retrieval & Grounding — make citations inspectable; control unsupported generation.

## Goal

Insert a Pass 3 (citation validation) between section generation and persistence. Each cited chunk is checked against the claim it supports. Status persisted per `Citation`. `Draft.groundedness_score` persisted. Sections with `unsupported_count > 0` regenerated once with a stricter prompt. Response shape exposes per-citation status + per-section groundedness.

## Current state (verified)

- `app.db.models.draft.Citation` already has `validation_status: text default 'unchecked'` and `validation_reason: text null` — **no Citation migration needed**.
- `app.db.models.draft.Draft` has NO `groundedness_score` column — **migration required**.
- `app.db.models.draft.Section` has NO `groundedness` column. **Decision:** persist per-section groundedness in `ai_output["sections_groundedness"]: dict[str, float | None]` (parallels existing `ai_output["sections_text"]`). Avoids a second migration and keeps API read path identical to existing pattern. A future v1.1 can promote to a real column once we know we want to index/sort on it.
- `config/router.yaml` already configures a `validation` tier (Qwen 2.5 7B → claude-haiku-4-5). M8 calls `router.generate(task="validation", ...)`.
- `app/draft/citations.py` has `parse_citations` and `strip_dangling` — fabricated chunk_ids are already stripped from section text by the generator, so by the time we see persisted citations the chunk_id is in the retrieved set. We must still cover "fabricated" defensively because the validator runs on the in-memory citation list before persistence.
- `app/api/schemas/drafts.py::CitationView` already exposes `validation_status` + `validation_reason`. Needs `groundedness` added to `SectionView` and `groundedness_score` added to `DraftResponse`.
- `SectionSpec` (`app/draft/templates/schema.py:19-26`) has **no** `must_cite` attribute and we will **not add one** — any new field would change `compute_fingerprint()` (NN-5) and bust every template's existing fingerprint / LLM cache / edit-log identity (NN-7). The existing signal for "required citation" is the `cites_at_least_one` validator, used by all three templates today. M8 detects it via a helper:
  ```python
  def _section_requires_citations(template, section_name: str) -> bool:
      sec = next((s for s in template.sections if s.name == section_name), None)
      if sec and "cites_at_least_one" in sec.validators:
          return True
      return any(
          v.id == "cites_at_least_one" and v.args.get("section") == section_name
          for v in template.validators
      )
  ```
- `engine.regenerate_section()` (`app/draft/engine.py:133-197`) and `DraftRepo.replace_section()` (`draft_repo.py:158-200`) currently write fresh citations with `validation_status='unchecked'`. M8 **must** wire `CitationValidator` into this path too — otherwise the first regen via `POST /api/drafts/{id}/sections/{name}/regenerate` re-introduces unchecked citations and breaks the acceptance criterion "every draft response includes per-citation validation status."
- Engine insertion point for Pass 3 in `generate()`: after `run_validators(...)` (`engine.py:89-92`) and the token aggregation block (`engine.py:95-98`), before the persist block at `engine.py:114-129`.

## Files touched

### New

1. **`app/draft/validator.py`** — `CitationValidator`
   - Dataclasses: `Claim`, `ClaimValidation`, `ValidationReport`.
   - `async validate_section(section_text, citations_for_section, chunks_by_id, fingerprint, trace_id) -> ValidationReport`
     1. `segment_claims(section_text, citations_for_section)` → `list[Claim]` (each carries `text`, `cited_chunk_ids`, `char_span`).
     2. Sentences with empty `cited_chunk_ids` → mark `unsupported`, reason `"claim has no citation"`. Skip LLM call.
     3. For each `(claim, cited_chunk_id)`:
        - **Trivial pass:** if `chunk_id not in chunks_by_id` → `unsupported`, reason `"chunk_id not in retrieved set"`. Skip LLM.
        - **Substring pass:** extract high-signal terms from claim (proper nouns via capitalized-token heuristic, numbers, dates via regex). If any appears verbatim in chunk text → record a `substring_match=True` boost (does not finalize status; only feeds into the merge).
     4. **Semantic pass:** batch up to 10 surviving `(claim_text, chunk.text)` pairs per LLM call. JSON schema enforced (passed as `schema=` to `router.generate`):
        ```json
        {
          "type": "object",
          "properties": {
            "results": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "pair_idx": {"type": "integer"},
                  "status": {"type": "string", "enum": ["supported","partial","unsupported","contradicted"]},
                  "reason": {"type": "string"}
                },
                "required": ["pair_idx","status"]
              }
            }
          },
          "required": ["results"]
        }
        ```
        Prompt (system + user) as in spec §validator. Call: `await self._router.generate([sys, user], task="validation", schema=SCHEMA, sampling=SamplingParams(max_tokens=1024, temperature=0.0), trace_id=trace_id)`. Pin `temperature=0.0` for reproducibility + meaningful LLM cache hits (NN-7). Parse response defensively: prefer `response.structured`, fall back to `json.loads(response.text)` (some providers populate only `.text` even when a schema is passed). If both parses fail → mark every pair in the batch `unsupported`, reason `"validator response unparseable"`.
     5. **Final merge per claim:** if ANY cited chunk is `contradicted` → claim is `contradicted`. Else if ANY is `unsupported` → `unsupported`. Else if ANY is `partial` → `partial`. Else `supported`. (Conservative: bad evidence wins over good evidence on the same claim.)
   - Returns `ValidationReport(per_claim, supported_count, partial_count, unsupported_count, contradicted_count, total_claims)`.
   - Tracks `tokens_in/tokens_out/cost_usd/model_used` (mirrors `FieldExtractor`/`SectionGenerator`) so engine aggregates totals.

2. **`app/db/migrations/versions/0004_draft_groundedness.py`** — add `app.drafts.groundedness_score NUMERIC(4,3) NULL`. Follow `0003_legal_en_overrides.py` style.

3. **Tests** (names per spec):
   - `tests/unit/test_claim_segmentation.py` — mixed cited/uncited sentences → correct claim spans. Fixture text avoids `Inc.` / `U.S.C.` / `et al.` so the simple sentence regex isn't tripped (limit documented in M8-DONE.md).
   - `tests/integration/test_citation_validator_supported.py` — `MockProvider` returns `supported` for every pair → all claims supported, groundedness = 1.0.
   - `tests/integration/test_citation_validator_unsupported.py` — two sub-cases: (a) `MockProvider` returns prose citing a real `chunk_id` whose chunk does not support it; semantic pass returns `unsupported` → validator catches it. (b) `MockProvider` returns prose with a fabricated UUID that `strip_dangling` somehow misses (force via direct `CitationDraft` injection) → trivial pass marks `unsupported` with reason `"chunk_id not in retrieved set"` without any LLM call.
   - `tests/integration/test_validation_regenerate.py` — section with `unsupported_count > 0` AND `cites_at_least_one` validator triggers exactly one regeneration (assert by counting `MockProvider` `generation`-tier invocations for that section's substring marker; assert exactly 2 generation calls total — initial + retry — and that the second call's prompt contains the "unsupported or contradicted claims" suffix). Also assert a section *without* `cites_at_least_one` does **not** retry even if unsupported.
   - `tests/integration/test_groundedness_score.py` — 7 supported / 2 partial / 1 unsupported across 10 claims → `Draft.groundedness_score = supported / total = 0.7`. **Weighting decision documented inline:** strict ratio of `supported / total` (partial does not count as half). Doc decision in `M8-DONE.md`.
   - `tests/integration/test_regenerate_endpoint_validates.py` (NEW — covers item 5b / 9): hit `POST /api/drafts/{id}/sections/{name}/regenerate`, then `GET /api/drafts/{id}` and assert the returned citations have `validation_status != "unchecked"` and the section's `groundedness` is populated.

### Modified

4. **`app/draft/citations.py`** — add:
   ```python
   @dataclass
   class Claim:
       text: str
       cited_chunk_ids: list[str]
       char_span: tuple[int, int]

   def segment_claims(section_text: str, citations: list[CitationDraft]) -> list[Claim]: ...
   ```
   Algorithm: walk citation tags in order; a claim spans from the previous boundary (sentence start or end of last citation) up to the next citation bracket; sentences that contain no citation tag still become claims (with empty `cited_chunk_ids`). Sentence boundaries via simple regex (`[.!?]+\s+`) — acceptable for legal prose; document the limit.

5. **`app/draft/engine.py`** — insert Pass 3 between Step 7 (validators) and Step 8 (persist), inside the existing `try:` block so failures route through `repo.fail()`:
   - Build `chunks_by_id = {chunk.id: chunk for chunks in retrieved.values() for chunk in chunks}`.
   - Group `citations` (the in-memory `list[CitationDraft]`) by `section_name`.
   - `validator = CitationValidator(self._router)`
   - Add the `_section_requires_citations(template, section_name)` helper (see "Current state" above) — it inspects existing `cites_at_least_one` validator config; **no new template schema field**, so `compute_fingerprint()` is unaffected (NN-5).
   - For each section, run validation; allow at most one regeneration:
     ```python
     section_groundedness: dict[str, float | None] = {}
     total_supported = total_claims = 0
     for section in sections:
         section_cits = [c for c in citations if c.section_name == section.section_name]
         report = await validator.validate_section(section.text, section_cits, chunks_by_id, fingerprint, trace_id)
         needs_retry = (
             (report.unsupported_count + report.contradicted_count) > 0
             and _section_requires_citations(template, section.section_name)
         )
         if needs_retry:
             section_spec = next(s for s in template.sections if s.name == section.section_name)
             suffix = (
                 f"\n\nYour previous output had {report.unsupported_count + report.contradicted_count} "
                 f"unsupported or contradicted claims. Use only the evidence provided. "
                 f"Cite or remove unsupported statements."
             )
             retry_section, retry_cits = await generator._generate_section(
                 section_spec, template, fields, retrieved, set(chunks_by_id.keys()), trace_id,
                 extra_instructions=suffix,
             )
             retry_report = await validator.validate_section(
                 retry_section.text, retry_cits, chunks_by_id, fingerprint, trace_id
             )
             # Use whichever has higher groundedness; tie → keep retry.
             def _gnd(r): return (r.supported_count / r.total_claims) if r.total_claims else 0.0
             if _gnd(retry_report) >= _gnd(report):
                 # replace section + citations in-place
                 idx = sections.index(section)
                 sections[idx] = retry_section
                 citations = [c for c in citations if c.section_name != section.section_name] + retry_cits
                 section_cits, report = retry_cits, retry_report
         # Annotate CitationDraft instances in-place with status + reason
         _apply_report_to_citations(report, section_cits)
         section_groundedness[section.section_name] = (
             report.supported_count / report.total_claims if report.total_claims else None
         )
         total_supported += report.supported_count
         total_claims += report.total_claims
     groundedness_score = (total_supported / total_claims) if total_claims else None
     ```
   - Aggregate validator token / cost stats: `tokens_in += validator.tokens_in`, etc. `model_used` precedence stays generator → extractor; validator model is logged via `LLMLogRepo` (NN-12) but doesn't override the top-level `Draft.model_used`.
   - Pass `groundedness_score` and `section_groundedness` into `repo.finalize(...)`.

5b. **`app/draft/engine.py::regenerate_section()`** (NEW for M8): after `generator._generate_section(...)` and before `repo.replace_section(...)`, run the validator against the new section + citations, annotate the `CitationDraft` instances in-place, and pass the recomputed per-section groundedness through `replace_section()` so it can update `ai_output["sections_groundedness"][section_name]` and the draft-level `groundedness_score` (recompute from all sections via a small repo helper or read-modify-write on `ai_output`). One-shot — no nested retry in the regenerate path.

6. **`app/draft/generator.py`** — `CitationDraft` dataclass: add `validation_status: str = "unchecked"` and `validation_reason: str | None = None`. Add optional `extra_instructions: str | None = None` parameter to `_generate_section` that appends to the user prompt verbatim when present.

7. **`app/draft/draft_repo.py`**:
   - `finalize()` signature gains `groundedness_score: float | None = None` and `section_groundedness: dict[str, float | None] | None = None`. Use the citation's now-populated `validation_status` / `validation_reason` when inserting `Citation` rows (replace the hardcoded `"unchecked"` at `draft_repo.py:89`). Add `section_groundedness` into the `ai_output` blob at `draft_repo.py:114-119` under key `"sections_groundedness"`. Persist `groundedness_score` via the same UPDATE statement at `draft_repo.py:121` (add `groundedness_score=:gnd` to the SET clause).
   - `replace_section()` (`draft_repo.py:158`): same fix at `draft_repo.py:196` — use citation's `validation_status` / `validation_reason` instead of hardcoded `"unchecked"`. Additionally gain a `section_groundedness: float | None = None` kwarg and update the draft's stored `ai_output["sections_groundedness"][section_name]` and the top-level `Draft.groundedness_score` (recompute from the now-updated per-section map, via a read-modify-write `UPDATE ... SET ai_output = jsonb_set(...), groundedness_score = :gnd ...` or load the draft, mutate `ai_output`, and update). Single atomic SQL statement preferred to avoid stale-read races.

8. **`app/api/schemas/drafts.py`**:
   - `SectionView`: add `groundedness: float | None = None`.
   - `DraftResponse`: add `groundedness_score: float | None = None`.
   - Both are nullable so drafts with zero claims (or pre-M8 drafts read back) deserialize cleanly.

9. **`app/api/routes/drafts.py`**:
   - `GET /api/drafts/{id}` (`drafts.py:100-123`): populate `SectionView.groundedness` from `ai_output["sections_groundedness"].get(section.name)` (a sentence with no citation has no `Citation` row, so deriving from persisted citations alone would silently miscount — this is why we persist the per-section map in `ai_output`). Populate `DraftResponse.groundedness_score` from `Draft.groundedness_score`.
   - `POST /api/drafts/{id}/sections/{name}/regenerate` (`drafts.py:207-222`): after `replace_section()` updates `ai_output`, return the recomputed per-section groundedness on `SectionView.groundedness`. (Citation `validation_status` / `validation_reason` already flow through naturally once item 7 is done.)

## Acceptance checks (mapped to spec)

- [ ] Every draft response includes per-citation `validation_status` + `validation_reason` (currently always `unchecked` → now real values). Covered for both initial generation and the regenerate-section API.
- [ ] Section with `unsupported_count + contradicted_count > 0` AND a `cites_at_least_one` validator (per-section or template-level) triggers exactly one regeneration attempt (test_validation_regenerate.py).
- [ ] `Draft.groundedness_score` column exists and is persisted (migration + repo + verified in integration test).
- [ ] Per-section groundedness exposed on `SectionView.groundedness` (`null` when section has zero claims).
- [ ] Fabricated `chunk_id` → `unsupported`, reason `"chunk_id not in retrieved set"` (unit test in test_citation_validator_unsupported.py).
- [ ] Sentence with no citation → `unsupported`, reason `"claim has no citation"` (covered by test_claim_segmentation.py + supported integration test).
- [ ] All four statuses (`supported | partial | unsupported | contradicted`) demonstrable in `M8-DONE.md` sample.
- [ ] NN-5: `prompt_fingerprint` unchanged for existing templates after M8 ships (no new `SectionSpec` field; verified by re-hashing all `config/templates/*.yaml` and asserting equality with stored fingerprints from M7).
- [ ] NN-5: fingerprint snapshotted at `generate()` entry is reused for validator calls (passed through to `validator.validate_section`).
- [ ] NN-7: validator calls use `temperature=0.0` so identical (claim, chunk) pairs hit the LLM cache deterministically.
- [ ] NN-12: every validator LLM call writes to `llm_log.llm_requests` (free — already done by `LLMRouter`).
- [ ] No new SDK imports outside `app/llm/` (validator uses `LLMRouter`).
- [ ] `make test` green; new unit + integration tests added.

## Risks / mitigations

- **Regeneration loop:** stricter prompt may still produce unsupported claims. Guard with a hard cap of 1 retry per section (boolean flag in the engine loop). Surface remainder to operator via the API response.
- **Token cost on huge drafts:** batching capped at 10 pairs per LLM call; one section with 30 claims × 1 citation = 3 validation calls. Budget guard already in `LLMRouter` enforces hourly USD cap (NN-6 spirit).
- **Sentence segmentation false splits** (abbreviations like "Inc." or "U.S."): legal prose is full of these. Document the limit; v1.1 could use a more robust segmenter. Tests use synthesized text that avoids the pathological cases.
- **Citation tag inside a quoted sentence:** the spec's "from start of sentence (or end of previous citation)" rule handles this — citation marker always terminates a claim span.
- **`MockProvider` interface drift:** `MockProvider.register()` matches on substring of the flattened message string. Tests must use a unique marker per pair-batch (e.g., the claim text) and return a `dict` with `text=json.dumps({"results": [...]})` so the validator's `json.loads(response.text)` fallback picks it up (MockProvider doesn't populate `response.structured`). Add a small helper fixture `make_validator_response(statuses: list[str]) -> dict` in `tests/integration/conftest.py`.
- **Validator response parsing:** prefer `response.structured`, fall back to `json.loads(response.text)`, then to "all unsupported with reason=unparseable" rather than raising — keeps Pass 3 from blowing up an otherwise-good draft. Logged via `log.warning` with the trace_id.
- **Atomicity:** validation runs before persistence so partial-failure leaves no half-validated `Citation` rows. If validator raises, engine routes through the existing `repo.fail()` path.
- **Regenerate-section endpoint also runs validator:** adds one extra LLM call per regen but is required to keep "every draft response includes per-citation validation status" true after the first edit-loop iteration.
- **NN-5 fingerprint stability:** adding any field to `SectionSpec` would change `compute_fingerprint()` and bust every existing template's identity (cache + edit log). M8 avoids this entirely by reusing the existing `cites_at_least_one` validator as the "must-cite" signal — no `SectionSpec` change.

## Out of scope (per spec)

- Per-word validation; auto-rewriting unsupported claims; numeric/date-specific validators; multi-pass validation.

## Definition of done

- All acceptance checks tick.
- `docs/milestones/M8-DONE.md` written with: deviations from plan, a sample draft JSON showing one each of `supported / partial / unsupported / contradicted`, and the groundedness-weighting decision (strict `supported / total`).
