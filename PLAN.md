# PLAN — M7: Draft Engine (Templates + Extraction + Generation)


**Depends on (already shipped):** M0 (errors/logging/middleware), M1 (DB schemas, job queue, `app.templates`/`app.drafts`/`app.sections`/`app.citations` tables), M2 (`LLMRouter` + `task=extraction|generation` tiers + cache + budget + `llm_requests` log), M3 (ingest API + jobs), M5 (chunks with `entities`, `prompt_fingerprint`, `legal_en` FTS), M6 (`HybridRetriever.multi_retrieve`).
**Blocks:** M8 (citation validation), M9 (edit-loop few-shot reads `Draft.prompt_fingerprint`), M11 (UI calls `POST /api/drafts`).
**Rubric:** Draft Quality (10) + Grounding generation side (25).

## Goal (one paragraph)

Ship `DraftEngine.generate(template_id, document_ids, trace_id)` that produces a `Draft` row containing extracted fields + prose sections, every prose claim citing chunk IDs that exist in `app.chunks`. Templates live in `config/templates/*.yaml`, sync into `app.templates` at startup, and are **snapshotted once per `generate()` call** with a `prompt_fingerprint = sha256(canonical_json(system_prompt + appended_rules + extraction_schema + sections))` flowing into every sub-call (NN-5). Generation runs off the job queue under a new `DRAFT_GENERATION` kind so `POST /api/drafts` returns in <200ms. Two full templates (`case_fact_summary`, `title_review_summary`) + one stub (`document_checklist`). Citation validation (M8) and few-shot retrieval (M9) are stubs with the right plumbing.

## Non-negotiables in scope

- **NN-5** Template snapshot immutability. `registry.get_latest()` is called exactly once at the top of `generate()`. Sub-modules (`FieldExtractor`, `SectionGenerator`, `regenerate_section`) receive the snapshot + fingerprint by argument; they never re-query the registry. Enforced by a unit/integration test that promotes v2 mid-call and asserts the in-flight run kept v1's fingerprint.
- **NN-7** LLM cache content-addressed. We rely on the router's existing cache; we do not pass `template_version` into the cache key. Resolved system+user content goes in via `messages`, so any rule append busts the cache automatically.
- **NN-12** Observability. `trace_id` (request ID) flows from the route → job payload → `DraftEngine.generate` → every `LLMRouter.generate(..., trace_id=trace_id)` call. The route reads `request_id` from `structlog.contextvars.get_contextvars()` (bound by `RequestIDMiddleware` in `app/core/middleware.py`) and writes it into the `DRAFT_GENERATION` job payload as `trace_id`; the worker hands it to the engine; the engine threads it through every router call. structlog binds `draft_id`, `template_id`, `prompt_fingerprint`, `trace_id`. (Note: `LLMRouter.generate()` does not currently accept `prompt_fingerprint` and so `llm_log.llm_requests.prompt_fingerprint` stays NULL for draft calls — linkage uses `trace_id`. Threading `prompt_fingerprint` into the router signature is a router-internals change deferred to a router-touching milestone.)
- **NN-3** Backpressure. Reuses existing queue-depth gate — no new logic required for the upload backpressure; we add the new `DRAFT_GENERATION` kind to the same `jobs.jobs` table.

## Files to create / modify

### New — template subsystem

| Path | Purpose |
|---|---|
| `app/draft/templates/schema.py` | Pydantic models: `FieldSpec`, `SectionSpec`, `ValidatorSpec`, `FewShotExample`, `DraftTemplate` with `compute_fingerprint() -> str` (canonical JSON, `sort_keys=True`, UTF-8, NFC-normalized strings). Field types restricted to `Literal["string","date","money","list[string]","list[party]"]` per the architecture doc. |
| `app/draft/templates/registry.py` | `TemplateRegistry`: (a) `load_from_disk(config_dir)` parses all `config/templates/*.yaml` into `DraftTemplate` Pydantic instances; (b) `sync_to_db(session)` — for each template, compares the on-disk fingerprint to the max-version row in `app.templates`; if different (or absent), inserts a new `TemplateVersion` row with `version = max+1`, persisting `yaml_body`, `system_prompt`, `appended_rules`, `prompt_fingerprint`; (c) `get_latest(template_id) -> DraftTemplate` — async, reads max-version row from DB and reconstructs a `DraftTemplate` snapshot by parsing the stored `yaml_body`; (d) `get_by_version(template_id, version) -> DraftTemplate` — async, reads the specific `(template_id, version)` row and reconstructs the snapshot (used by `regenerate_section` to pin the original template at draft time); (e) `list_templates() -> list[TemplateInfo]`. Process-local cache keyed by `(template_id, version)` to avoid re-parsing YAML on every generate; cache invalidated when a `sync_to_db` writes a new version. |
| `app/draft/templates/validators.py` | `ValidatorRegistry` with built-ins `non_empty`, `is_date` (ISO-8601 or US date forms via dateutil), `is_money` (currency regex + numeric parse), `length_between(min,max)` (operates on a section's word count or a field's string length depending on target), `cites_at_least_one` (looks at `Citation` count for a section/field). Returns `list[ValidatorResult{field, validator_id, status: ok|warn|fail, message}]`. Citation-content validators are intentionally left to M8 — only the registry hook is here. **Not shipping `entity_in_corpus` in M7**: the current `chunks.entities` column is a flat `list[str]` of regex-extracted proper nouns / dollar amounts / dates (see `app/ingest/chunker.py:51`) with no entity-type tagging, so a typed `entity_in_corpus(entity_type=...)` validator has nothing to query. Listed as a follow-up for whichever milestone introduces typed entities. |

### New — engine

| Path | Purpose |
|---|---|
| `app/draft/engine.py` | `DraftEngine(retriever, llm_router, registry, draft_repo)`. **Draft state machine: `queued` (inserted by the API route) → `generating` (engine entry) → `ready` \| `failed`.** `async generate(draft_id, template_id, document_ids, trace_id) -> Draft` (`draft_id` already exists, written by the route as `status='queued'`): 1) `template = await registry.get_latest(template_id)` ← **the only call site (NN-5)**; 2) `fingerprint = template.compute_fingerprint()`; 3) `DraftRepo.mark_generating(draft_id, template_version=template.version, prompt_fingerprint=fingerprint)` — atomic update from `queued` to `generating`; 4) `retrieved = await retriever.multi_retrieve(template.retrieval_queries, document_ids, top_k_per_query=5)`; 5) `fields = await FieldExtractor(llm_router).extract_all(template, retrieved, fingerprint, trace_id)`; 6) `sections, citations = await SectionGenerator(llm_router).generate_all(template, fields, retrieved, fingerprint, few_shot=[], trace_id=trace_id)`; 7) `validators = ValidatorRegistry.run(template, fields, sections, citations)`; 8) persist via `DraftRepo.finalize`, set `status='ready'`, `generated_at=now()`, `ai_output={fields, sections_text, validators, retrieval_meta}`. On any exception inside steps 4–7: `DraftRepo.fail(draft_id, error_code, error_message)` and re-raise. `async regenerate_section(draft_id, section_name)`: loads `Draft` row → reads `template_id, template_version` → `registry.get_by_version(template_id, version)` → reuses the **original** template snapshot; calls only `SectionGenerator` for the single section; updates that `Section.ai_text` and replaces its `Citation` rows in one transaction. |
| `app/draft/extractor.py` | `FieldExtractor(llm_router)`. `async extract_all(template, retrieved, fingerprint, trace_id) -> dict[str, FieldExtraction]`. Per field: build a Pydantic `DynamicModel(value, supporting_chunk_ids: list[UUID], confidence: float)` typed off the `FieldSpec.type`; resolve evidence as the top chunks for that field's retrieval key with explicit `[chunk:CHUNK_ID]` labels in the user message; call `llm_router.generate(task="extraction", schema=DynamicModel.model_json_schema(), sampling=SamplingParams(max_tokens=512, temperature=0.0), trace_id=trace_id)`. Substring sanity-check: if `value` is a string scalar, at least one supporting chunk's `text` (NFKC-normalized, lower-cased, whitespace-collapsed) must contain the value (same normalization). On failure → `confidence=0.0`, `value=None`, `error_code='SUBSTRING_MISMATCH'`. On `SchemaViolation` (router already retries once) → `value=None`, `error_code='SCHEMA_VIOLATION'`, `confidence=0.0`. |
| `app/draft/generator.py` | `SectionGenerator(llm_router)`. `async generate_all(template, fields, retrieved, fingerprint, few_shot, trace_id) -> tuple[list[SectionDraft], list[CitationDraft]]`. Per section: system message = `template.system_prompt + "\n\nAdditional rules:\n" + "\n".join(template.appended_rules)`; user message bundles section description, target_length, extracted fields summary (as JSON), evidence chunks labeled `[chunk:CHUNK_ID]`, and few-shot examples (empty list in M7); explicit instruction to cite every factual claim and to write "Insufficient evidence in provided documents." if no chunks support a claim. Call `llm_router.generate(task="generation", sampling=SamplingParams(max_tokens=1200, temperature=0.2), trace_id=trace_id)`. Parse citations with `re.compile(r"\[chunk:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]")` (strict UUID). For each match, check the chunk exists in `retrieved` for that section (or globally for the doc set, via a chunk-id allowlist passed in); unknown chunk IDs are stripped from the text and recorded as `dangling_citations` in section metadata. Each accepted citation becomes a `CitationDraft(section_name, chunk_id, claim_span_start=match.start(), claim_span_end=match.end())` using positions in the *cleaned* (dangling-stripped) section text. `section_name` is logical here; `DraftRepo.finalize` maps it to `section_id` after inserting `Section` rows (see below). `validation_status` defaults to `'unchecked'` (M8 fills). Length-overrun policy from architecture doc edge-cases: if word count > `target_length_max * 1.5`, truncate at last sentence-end ≤ max and log `event="section.overrun"`. |
| `app/draft/citations.py` | Pure helpers — `parse_citations(text) -> list[(chunk_id, span)]`, `strip_dangling(text, allowlist) -> (clean_text, dangling)`. Imported by `generator.py` and (later) M8's validator. |
| `app/draft/draft_repo.py` | `DraftRepo(session)`. CRUD for `Draft`, `Section`, `Citation`. Methods: `create_queued(template_id, document_ids) -> draft_id` — inserts row with `status='queued'`, placeholder `template_version=0` and `prompt_fingerprint=''` (will be overwritten on `mark_generating`; the DB column default `'generating'` is bypassed by an explicit `'queued'` value at insert); `mark_generating(draft_id, template_version, prompt_fingerprint)` — atomic `UPDATE ... WHERE id=:id AND status='queued'`, raises `ConflictError` if no row updated; `finalize(draft_id, fields, sections, citations, validators, model_used, tokens_in, tokens_out, cost_usd)` — inserts `Section` rows first, builds a `name -> section_id` map, then inserts `Citation` rows referencing those IDs (the generator returns `CitationDraft.section_name`; the repo resolves to `section_id`); `fail(draft_id, error_code, message)`; `get(draft_id) -> DraftWithSectionsAndCitations`; `replace_section(draft_id, section_name, new_text, new_citations)` (deletes existing `Citation` rows for that section then re-inserts under the same `section_id`). All writes inside one transaction per call. |

### New — API + job

| Path | Purpose |
|---|---|
| `app/api/schemas/drafts.py` | Pydantic request/response: `DraftCreateRequest{template_id, document_ids: list[UUID]}`, `DraftCreateResponse{draft_id, status}`, `DraftResponse{draft_id, template_id, template_version, prompt_fingerprint, status, fields, sections: list[SectionView], validators, generated_at, model_used, cost_usd, error?}`, `SectionView{name, text, target_length_min, target_length_max, citations: list[CitationView]}`, `CitationView{chunk_id, claim_span_start, claim_span_end, validation_status, validation_reason}`, `TemplateInfo{id, latest_version, display_name, description, fingerprint}`. |
| `app/api/routes/drafts.py` | `POST /api/drafts` → validates `document_ids` exist and are `status='ready'` (return `409 DOCS_NOT_READY` otherwise); reads `trace_id = structlog.contextvars.get_contextvars().get("request_id")` (bound by `RequestIDMiddleware`); calls `DraftRepo.create_queued(template_id, document_ids)` → `draft_id`; enqueues `JobKind.DRAFT_GENERATION` job carrying `{draft_id, template_id, document_ids, trace_id}`; returns `{draft_id, status:"queued"}` with `201`. p95 target ≤200ms. `GET /api/drafts/{id}` → returns `DraftResponse`, 404 if missing. `POST /api/drafts/{id}/sections/{name}/regenerate` → rejects with `409` if draft `status != 'ready'` or the named section does not exist; runs **inline** (locked decision — single bounded LLM call, hard 30s timeout via `asyncio.wait_for`, matches architecture's `<8s` p95 target); returns the updated `SectionView`. |
| `app/api/routes/templates.py` | `GET /api/templates` → calls `TemplateRegistry.list_templates()`. |
| `app/jobs/handlers/draft_generation.py` | `handle_draft_generation(payload, session)`. Reads `draft_id, template_id, document_ids, trace_id`; binds `trace_id` into structlog contextvars on entry so engine/router logs carry it; resolves `LLMRouter`, `Retriever`, `Registry`, `DraftRepo`; calls `DraftEngine.generate(draft_id=..., template_id=..., document_ids=..., trace_id=...)`. Updates `Draft.status = 'failed'` on `AppError`, swallows-and-logs unknown exceptions wrapped as `DraftError("DRAFT_GENERATION_UNEXPECTED")`. Registers itself in `HANDLERS[JobKind.DRAFT_GENERATION]` and is imported by `app/jobs/handlers/__init__.py` for the registration side-effect. |
| `app/jobs/kinds.py` | Add `DRAFT_GENERATION = "draft_generation"`. |
| `app/core/errors.py` | Add `DraftError(AppError)` (code class `DRAFT_*`), `TemplateNotFoundError`. |

### Modify

| Path | Change |
|---|---|
| `app/main.py` | (a) include `drafts_router` and `templates_router`; (b) on lifespan startup, instantiate `TemplateRegistry`, call `load_from_disk` + `sync_to_db` once (after DB ready); log `event="templates.synced"` with the per-template `(id, version, fingerprint)` triples. |
| `app/api/deps.py` | Add `get_template_registry()` lazy singleton + `reset_template_registry_for_tests()`; add `get_draft_engine()` lazy singleton that composes router+retriever+registry+repo (per-request session factory for repo). |
| `app/settings.py` | Add `TEMPLATES_DIR: str = "config/templates"` (Pydantic `DirectoryPath` with fallback to repo-relative default), `DRAFT_EXTRACTION_TOP_K: int = 5`, `DRAFT_SECTION_TOP_K: int = 8`, `DRAFT_SECTION_MAX_TOKENS: int = 1200`, `DRAFT_REGENERATE_TIMEOUT_S: int = 30`. |
| `app/jobs/worker.py` | The worker already polls every `JobKind` value automatically (`all_kinds = [k.value for k in JobKind]` at `worker.py:97`) — no allow-list change is needed once `DRAFT_GENERATION` is added to `JobKind`. The only edit: extend `_get_semaphore` (`worker.py:35-44`) with a branch mapping `JobKind.DRAFT_GENERATION` → `settings.WORKER_DRAFT_CONCURRENCY` (default `2`, per architecture's `task=generation` cost profile). |

### Config — template YAMLs

| Path | Notes |
|---|---|
| `config/templates/case_fact_summary.yaml` | Verbatim from M7-spec — full system prompt, extraction schema (parties, jurisdiction, filing_date, claims, damages_sought), three sections (procedural_history, factual_background, key_issues), retrieval_queries per field/section, validators. |
| `config/templates/title_review_summary.yaml` | Same shape; fields: `property_description`, `owners` (list[party]), `encumbrances` (list[string]), `liens` (list[string]), `easements` (list[string]); sections: `property_summary`, `chain_of_title`, `encumbrance_summary`. Validators: `non_empty(owners)`, `cites_at_least_one(owners)`. System prompt mirrors `case_fact_summary` but instructs the model to surface table-shaped data faithfully. |
| `config/templates/document_checklist.yaml` | Stub — `extraction_schema` empty; one section `checklist_summary` with target_length `[40, 120]`; validators: `length_between(40, 120)` on the section (uses the same built-in word-count check the other templates rely on) and `cites_at_least_one` (forces at least one chunk reference). No typed-entity validator (`entity_in_corpus` is deferred — see validators row). Proves the YAML→engine path without inflating scope. |

## Tests (must all pass; otherwise milestone is not done)

### Unit

| File | What it asserts |
|---|---|
| `tests/unit/test_template_fingerprint.py` | (a) round-trip stability — load YAML twice, fingerprints equal; (b) one-char change to `system_prompt` flips fingerprint; (c) reordering `appended_rules` flips fingerprint (canonical JSON preserves list order — *list order is semantic*); (d) reordering fields in `extraction_schema` does **not** flip fingerprint (dict, sorted keys). |
| `tests/unit/test_template_registry_load.py` | All three production YAMLs parse; invalid YAML raises `TemplateValidationError` with file path. |
| `tests/unit/test_validators_builtins.py` | `is_date`, `is_money`, `non_empty`, `length_between`, `cites_at_least_one` happy + sad paths. |
| `tests/unit/test_citation_parsing.py` | `parse_citations` extracts only valid UUIDs; malformed `[chunk:foo]` ignored; `strip_dangling` removes citations not in allowlist and reports them. |
| `tests/unit/test_extractor_substring_check.py` | When LLM returns a value not in any supporting chunk, `confidence=0`, `value=None`. Mocked router. |
| `tests/unit/test_generator_overrun.py` | Output > 1.5× `target_length_max` is truncated at the last sentence end ≤ max; overrun is logged. |

### Integration (Postgres + mock LLM)

| File | What it asserts |
|---|---|
| `tests/integration/test_template_sync.py` | `sync_to_db` inserts v1; second call with identical YAML inserts no new row; bump system_prompt → v2 inserted; `get_latest` returns v2. |
| `tests/integration/test_template_snapshot.py` (**NN-5**) | Start `DraftEngine.generate` with template v1 mocked to slow LLM. While the call is in flight (use `asyncio.Event`), call `registry.sync_to_db` with v2 (mutated YAML). Await generate completion. Assert: `Draft.template_version == 1`, `Draft.prompt_fingerprint == fingerprint(v1)`, the user messages observed by the mock router are v1's system prompt verbatim, and `registry.get_by_version(template_id, 1)` still returns the v1 snapshot afterwards. |
| `tests/integration/test_draft_e2e_case_fact_summary.py` | Seed `native_clean.pdf` through the existing ingest pipeline → `status='ready'`; `POST /api/drafts {template_id: 'case_fact_summary', document_ids:[id]}` → 201 with `status='queued'`; drive the worker until `Draft.status='ready'`; assert: `fields['parties']` non-empty, ≥1 section has ≥1 citation, every cited `chunk_id` exists in `app.chunks`, no dangling chunk-IDs remain in section text. |
| `tests/integration/test_draft_e2e_title_review.py` | Same flow with title-review fixture. |
| `tests/integration/test_draft_empty_evidence.py` | A doc that doesn't mention damages → `fields['damages_sought'].value is None` and `confidence == 0.0`; the related section text contains the literal "Not stated" or "Insufficient evidence". |
| `tests/integration/test_draft_regenerate_section.py` | After a successful draft, `POST /api/drafts/{id}/sections/factual_background/regenerate` replaces only that section, citations are re-built, `template_version` on the draft is unchanged, fingerprint passed to the second generator call equals the original fingerprint (NN-5 across regeneration). |
| `tests/integration/test_draft_post_latency.py` | `POST /api/drafts` returns within 200ms p95 over 20 sequential requests (uses an empty worker so generate doesn't run inline). |
| `tests/integration/test_draft_llm_log.py` | After a generate, `llm_log.llm_requests` has at least N rows where N = #fields + #sections, all carrying the same `trace_id` and non-null `cost_usd`. |
| `tests/integration/test_draft_docs_not_ready.py` | `POST /api/drafts` against a `status='ocr_running'` document returns 409 `DOCS_NOT_READY`. |

### Acceptance-criteria mapping

| Spec acceptance criterion | Test |
|---|---|
| Both production templates produce valid drafts on fixture docs | `test_draft_e2e_case_fact_summary` + `test_draft_e2e_title_review` |
| NN-5 snapshot test green | `test_template_snapshot` + `test_draft_regenerate_section` |
| No fabricated chunk IDs | All e2e tests assert `chunk_id ∈ app.chunks`; `test_citation_parsing` covers the parser side |
| Per-call cost recorded in `llm_log.llm_requests` per generate | `test_draft_llm_log` |
| `POST /api/drafts` < 200ms; generation off the job queue | `test_draft_post_latency` + `test_draft_e2e_case_fact_summary` drives the worker explicitly |

## Implementation sequence (the order I will write code in)

1. **`schema.py` + `validators.py`** — pure Pydantic + canonical-JSON fingerprint. Lands with `test_template_fingerprint`, `test_validators_builtins`.
2. **`registry.py` + lifespan sync** — file → DB → snapshot. Lands with `test_template_registry_load`, `test_template_sync`.
3. **Three YAMLs** — parsing covered by step 2's tests.
4. **`citations.py` + `extractor.py` + `generator.py`** — small modules, each callable with a mock router. Lands with `test_citation_parsing`, `test_extractor_substring_check`, `test_generator_overrun`.
5. **`draft_repo.py`** — CRUD, transactional.
6. **`engine.py`** — composes everything; honors NN-5 by passing `template`/`fingerprint` arguments only. Lands with `test_template_snapshot`.
7. **Job kind + handler + worker wiring** — `DRAFT_GENERATION` end-to-end. Lands with `test_draft_post_latency` + `test_draft_docs_not_ready`.
8. **API routes (`drafts`, `templates`) + schemas** — thin. Lands with the e2e tests.
9. **Mock-router responses for fixture e2e** — extend `tests/conftest.py` with a small canned-response fixture that returns plausible JSON for each field schema and prose with valid `[chunk:<real-uuid>]` references pulled from the seeded chunks. This is the only realistic way to make e2e deterministic.

Steps 1–3 can be done by one sub-agent; 4 is splittable per file; 5–6 are sequential; 7–9 sequential after 6. Mirrors the spec's "sub-agent delegation" section.

## Things I will NOT do (preventing scope creep / breakage)

- **No citation-content validation.** That is M8. I leave `Citation.validation_status='unchecked'` and `ValidatorRegistry` returns only structural checks.
- **No few-shot retrieval.** `SectionGenerator` accepts `few_shot: list[FewShotExample] = []` and the engine passes `[]`. M9 fills.
- **No SSE for draft progress.** That is v1.1; clients poll `GET /api/drafts/{id}` (per architecture's "Open questions"). Existing document SSE is untouched.
- **No edits to retrieval / ingest / router / job queue internals.** Adding a `JobKind` value, one handler, and a per-kind branch in `_get_semaphore` is additive; everything else is read-only consumer. I will run the existing M5/M6 test suites unchanged.
- **No `DRAFT_REGENERATE_SECTION` job kind.** Regeneration runs inline in the request handler (single bounded LLM call, 30s timeout). Symmetry-with-generation arguments noted; revisit if the latency budget slips.
- **No changes to `app/db/models/template.py`, `draft.py`, `chunk.py`.** Schemas already match the spec. If a column is missing later, that's a real migration in M8/M9; I do not pre-migrate.
- **No new alembic migration** unless a deficit is discovered. Inspection of `0001_initial.py` says all needed tables/columns exist; if I find a gap mid-implementation, I will pause and produce a small additive migration `0004_<…>.py` rather than mutate `0001`.

## Risk register

| Risk | Mitigation |
|---|---|
| Mock router responses drift from real provider output shape | `MockProvider` shipped in M2 already supports schema mode; I will reuse it and assert the produced messages match the schema, not synthesize JSON ad-hoc. |
| Substring sanity check is too strict for `is_date`/`is_money` (formatted differently in source) | The check runs only when the extracted `value` is a *string scalar*. Date/money values resolve to typed objects before the check, so this is a no-op for them — the validator handles correctness. |
| Worker concurrency regression — adding `DRAFT_GENERATION` could starve OCR | Per-kind semaphore already exists (NN-3). Default `WORKER_DRAFT_CONCURRENCY=2`; document the knob in the DONE doc. |
| Fingerprint flakiness from JSON key ordering of nested Pydantic dumps | `compute_fingerprint` uses `model.model_dump(mode="json")` then `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, then UTF-8 SHA-256. Covered by `test_template_fingerprint`. |
| `regenerate_section` racing a template version bump | It loads by `(template_id, draft.template_version)`, not `get_latest`. Tested. |
| `POST /api/drafts` slow because it pre-validates document `status='ready'` | Single indexed SELECT on `app.documents`. The route holds no LLM/embedding work. Latency test enforces 200ms. |
| Dangling chunk IDs in generator output | `strip_dangling` removes them before persistence; counted as a metric in `Draft.ai_output['retrieval_meta']['dangling_citations']`. |
| LLM cost blowout on a long doc set | Both extraction and generation route to the `extraction`/`generation` tiers whose hosted fallback is Haiku/Sonnet — budget cap (NN-6) is enforced by the router; we do not bypass it. |

## Definition of done (this milestone)

1. All tests above green; existing M0–M6 tests still green.
2. `make up && make seed && curl -X POST /api/drafts {…}` produces a `ready` draft against fixture docs with valid citations.
3. NN-5 test (`test_template_snapshot`) green.
4. `llm_log.llm_requests` populated per generate with matching `trace_id`.
5. `docs/milestones/M7-DONE.md` written: shipped files, deviations, sample draft JSON for both production templates inlined, follow-ups (M8 citation validation hookup, M9 few-shot plumbing fill).

## Open questions for the reviewer (won't block start, will block merge)

1. **Per-draft idempotency.** Should `(template_id, document_ids set hash, prompt_fingerprint)` dedupe to an existing `Draft`? The architecture's "Open questions" says yes for v1. I propose: leave as a TODO with a `dedup_key` on the `DRAFT_GENERATION` job (cheap, future-compatible) but no UI affordance.
2. **`document_checklist` ambitions.** Spec says stub. Locked: one section + `length_between` + `cites_at_least_one`, no extraction fields, no typed-entity validator (entities aren't typed in v1 — see validators row). Confirms the YAML-only extensibility claim without claiming behavior we can't deliver.
