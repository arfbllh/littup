# M7 — Draft Engine (Templates + Extraction + Generation)

**Estimated time:** 3 hours
**Dependencies:** M2, M6
**Rubric impact:** Draft Quality (10 pts) + the generation side of Grounding (25 pts)

## Goal

A working `DraftEngine.generate(template_id, document_ids)` that produces a structured `Draft`: extracted fields + prose sections, every claim citing chunks by ID. Two production templates ship fully (`case_fact_summary`, `title_review_summary`) + one stub (`document_checklist`). Template versioning + immutable snapshot per generation is correctly enforced (NN-5).

Citation validation is M8 — generation can produce raw cited drafts here.

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-5**
2. `docs/architecture/03-components/draft-engine.md` — full spec
3. `docs/architecture/03-components/retrieval.md` — multi-query interface

## Non-Negotiables that apply

- **NN-5** — template loaded once at top of `generate()`; `prompt_fingerprint` flows through every sub-call; no method below `generate()` calls the registry

## Files to create

### Template system

- `app/draft/__init__.py`
- `app/draft/templates/__init__.py`
- `app/draft/templates/schema.py` — Pydantic models:
  - `FieldSpec(name, type, required, description, must_cite)`
  - `SectionSpec(name, description, target_length: tuple[int,int], must_cite)`
  - `ValidatorSpec(field, validator_id, params)`
  - `FewShotExample(field_or_section, ai_value, user_value, context_tags)`
  - `DraftTemplate(id, version, display_name, description, extraction_schema, sections, retrieval_queries, system_prompt, appended_rules, few_shot_examples, validators)`
  - `DraftTemplate.compute_fingerprint() -> str` — `sha256` of canonical JSON of `system_prompt + appended_rules + extraction_schema + sections`
- `app/draft/templates/registry.py`:
  - `TemplateRegistry` loads `config/templates/*.yaml` on init
  - On startup, syncs file-based templates into `app.templates` table — if the YAML's content fingerprint differs from the latest version stored, insert a new version row
  - `get_latest(template_id) -> DraftTemplate` — reads from DB, returns a Pydantic snapshot
  - `list() -> list[TemplateInfo]`
- `app/draft/templates/validators.py` — `ValidatorRegistry`:
  - Built-in validators: `non_empty`, `is_date`, `is_money`, `length_between(min,max)`, `cites_at_least_one`, `entity_in_corpus(entity_type)`
  - Templates reference validators by id; this registry resolves to actual callables

### Template YAML files

- `config/templates/case_fact_summary.yaml`:
  ```yaml
  id: case_fact_summary
  display_name: "Case Fact Summary"
  description: "First-pass case fact summary for internal use"
  system_prompt: |
    You are a paralegal at Pearson Specter Litt drafting an internal case fact
    summary. You only state facts supported by the provided documents. After
    every factual claim, cite the supporting chunk as [chunk:CHUNK_ID]. If
    evidence is missing for a field, say "Not stated in the provided documents."
    Use plain, declarative sentences.
  extraction_schema:
    parties:
      type: list[party]
      required: true
      description: "Named parties (plaintiff, defendant, etc.) with their role"
      must_cite: true
    jurisdiction:
      type: string
      required: true
      description: "Court and venue"
      must_cite: true
    filing_date:
      type: date
      required: false
      description: "When the matter was filed"
      must_cite: true
    claims:
      type: list[string]
      required: true
      description: "Causes of action / claims for relief"
      must_cite: true
    damages_sought:
      type: money
      required: false
      description: "Total damages sought, if stated"
      must_cite: true
  sections:
    - name: procedural_history
      description: "Chronological procedural history of the matter"
      target_length: [80, 250]
      must_cite: true
    - name: factual_background
      description: "Underlying facts, in operator-friendly prose"
      target_length: [150, 400]
      must_cite: true
    - name: key_issues
      description: "Issues to be decided"
      target_length: [60, 200]
      must_cite: true
  retrieval_queries:
    parties: "parties plaintiff defendant claimant respondent named in caption"
    jurisdiction: "court venue district filed in jurisdiction"
    filing_date: "date filed initiated commenced"
    claims: "causes of action allegations claims relief sought count one count two"
    damages_sought: "damages amount monetary harm injury sought"
    procedural_history: "filed served motion order ruling hearing"
    factual_background: "facts events conduct occurred"
    key_issues: "issues questions whether court must decide"
  validators:
    - field: parties
      validator: non_empty
    - field: parties
      validator: cites_at_least_one
    - field: filing_date
      validator: is_date
    - field: damages_sought
      validator: is_money
  ```
- `config/templates/title_review_summary.yaml` — same shape; fields are different (`property_description`, `owners`, `encumbrances`, `liens`, `easements`)
- `config/templates/document_checklist.yaml` — stub: only validators that check "is doc type X present"

### Engine

- `app/draft/engine.py` — `DraftEngine`:
  - `__init__(retriever, llm_router, template_registry, draft_repo)`
  - `async generate(template_id, document_ids, trace_id)`:
    1. `template = self.registry.get_latest(template_id)` — ONLY call site (NN-5)
    2. `fingerprint = template.compute_fingerprint()`
    3. Create a `Draft` row with `status='generating'`, `template_version`, `prompt_fingerprint`
    4. `retrieved = await self.retriever.multi_retrieve(template.retrieval_queries, document_ids, top_k_per_query=5)`
    5. `fields = await self.extractor.extract_all(template, retrieved, fingerprint)` — passes `template` and `fingerprint` explicitly
    6. `sections = await self.generator.generate_all(template, fields, retrieved, fingerprint, few_shot=[])` — few-shots empty for now (M9 fills)
    7. Run validators from template (M8 fills citation validators)
    8. Update Draft row to `status='ready'`, persist ai_output JSONB
    9. Return `Draft`
  - `async regenerate_section(draft_id, section_name)` — reuses same template snapshot from the original draft (loaded from `draft.template_version`)

### Extractor

- `app/draft/extractor.py` — `FieldExtractor`:
  - `async extract_all(template, retrieved_chunks_by_field, fingerprint) -> dict[str, FieldExtraction]`
  - For each field: build a JSON schema from `FieldSpec` (Pydantic dynamic model); call `llm_router.generate(task="extraction", messages=..., schema=DynamicModel, ...)`
  - Prompt structure: `system: "You extract structured fields from legal documents..."` + `user: "Field: {description}\n\nEvidence:\n{chunks with chunk_id labels}\n\nReturn JSON: {value, supporting_chunk_ids, confidence}"`
  - Substring sanity check: if `value` is a string, at least one supporting chunk must contain it (with normalization); else mark `confidence=0`

### Generator

- `app/draft/generator.py` — `SectionGenerator`:
  - `async generate_all(template, fields, retrieved_chunks_by_section, fingerprint, few_shot)`
  - For each section, build a prompt:
    - System prompt = `template.system_prompt` + `"\n\nAdditional rules:\n" + "\n".join(template.appended_rules)`
    - User message includes: section description, target length, extracted fields, retrieved chunks (with `[chunk:CHUNK_ID]` labels), few-shot examples
    - Generation instruction: "Write the section. Cite every factual claim with [chunk:CHUNK_ID]. If evidence is missing, state so explicitly."
  - Call `llm_router.generate(task="generation", ...)`
  - Parse out citations via regex: `\[chunk:([a-f0-9-]+)\]`
  - Build `Citation` records per cited chunk per section

### Persistence

- `app/draft/draft_repo.py` — CRUD for `Draft`, `Section`, `Citation` rows

### API

- `app/api/schemas/drafts.py` — request/response shapes
- `app/api/routes/drafts.py`:
  - `POST /api/drafts` `{template_id, document_ids}` → kicks off `DraftEngine.generate`; returns `{draft_id, status}` immediately; client polls or subscribes via SSE
  - `GET /api/drafts/{id}` → full draft with citations
  - `POST /api/drafts/{id}/sections/{name}/regenerate`
  - `GET /api/templates` → list

### Tests

- `tests/unit/test_template_fingerprint.py` — change `system_prompt` by one char → fingerprint changes; same content → same fingerprint
- `tests/unit/test_template_snapshot.py` (NN-5) — start a `generate()` call with template v1; while in flight, the registry promotes v2; assert the draft's extractor/generator used v1 properties; assert `draft.prompt_fingerprint` matches v1's fingerprint
- `tests/integration/test_draft_e2e_case_fact_summary.py` — upload `native_clean.pdf` (which contains a fake case caption); generate a `case_fact_summary` draft; assert `fields['parties']` is non-empty; assert at least one section has citations; assert all citation `chunk_id`s exist
- `tests/integration/test_draft_e2e_title_review.py` — same for title review
- `tests/integration/test_draft_empty_evidence.py` — generate a draft from a document that doesn't mention "damages"; assert `fields['damages_sought']` is null with `confidence=0`; assert the related section's output says "Not stated"

## Acceptance criteria

- [ ] Both production templates produce valid drafts on appropriate fixture docs
- [ ] NN-5 snapshot test green
- [ ] No fabricated chunk IDs (every citation references a real chunk)
- [ ] Per-call cost recorded in `llm_log.llm_requests` per generate
- [ ] `POST /api/drafts` returns `< 200ms`; the generation happens via the job queue (so a slow LLM doesn't time out HTTP) — implement this via a new `DRAFT_GENERATION` job kind

## Out of scope

- Citation validation — M8
- Few-shot retrieval from edits — M9 (the `few_shot=[]` plumbing is here, gets filled in M9)
- Section streaming to UI — v1.1

## Definition of done

Two fully-baked templates + a stub. Drafts persist with their fingerprints. NN-5 enforced and tested. `M7-DONE.md` written with sample draft JSON outputs from both templates inlined.

## Sub-agent delegation

After `app/draft/templates/schema.py` and `registry.py` are merged:

- Sub-agent A: Template YAMLs + validator registry
- Sub-agent B: `app/draft/extractor.py` + extractor tests
- Sub-agent C: `app/draft/generator.py` + generator tests
- Sub-agent D: `app/api/routes/drafts.py` + draft job kind + E2E test

The engine.py sequential after all four are in.
