# M10 — Rule Extractor (Stage 2 Loop)

**Estimated time:** 1.5 hours
**Dependencies:** M9
**Rubric impact:** Improvement from Edits — closes the loop the rubric is looking for ("reusable patterns are learned", "future outputs improve meaningfully")

## Goal

An offline process (triggered both on a schedule and via admin button) that reads recent edits, detects persistent patterns the operator keeps making, and writes them as `appended_rules` on the template — bumping the template version. The next draft uses the new rules baked into the system prompt. This is the *aggregated, durable* improvement signal, complementing M9's per-call few-shot retrieval.

This is the milestone that turns the system from "remembers your last edit" to "learns your house style."

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-5, NN-9**
2. `docs/architecture/03-components/edit-loop.md`
3. `docs/architecture/03-components/llm-router.md` — `task="analysis"` tier

## Non-Negotiables that apply

- **NN-5** — rule appends bump the template version; `prompt_fingerprint` changes
- **NN-9** — extractor is triggered by both APScheduler **and** an admin endpoint; idempotent on re-run

## Files to create / modify

### Extractor

- `app/edits/rule_extractor.py` — `RuleExtractor`:
  - `async run(template_id, since: datetime | None = None) -> ExtractionResult`:
    1. Load recent `Edit` rows for `template_id` (default since = last extraction time stored in `template_extractor_state` table; M1 didn't create this — create in M10 migration `0002_rule_extractor_state.py`)
    2. Group edits by `field_or_section_name`
    3. For each group with >= `MIN_EDITS_FOR_RULE` (default 3):
       - Build an analysis prompt:
         ```
         You are reviewing how a legal operator edits AI-generated drafts to
         extract a durable house-style rule.
         
         Below are {N} cases where the AI produced one value and the operator
         changed it to another. Look for a CONSISTENT pattern. If you find one,
         output ONE concise rule the AI can follow next time, phrased as an
         instruction (e.g., "When listing parties, always include their role in
         parentheses after the name.").
         
         If no consistent pattern emerges, output: NO_RULE.
         
         Cases:
         1. AI said: "{ai_value}" — operator changed to: "{user_value}"
         2. ...
         ```
       - Call `llm_router.generate(task="analysis", ...)` — uses larger local or hosted model
       - Parse output: if `NO_RULE`, skip; otherwise the rule string
    4. Compare extracted rules against the template's current `appended_rules`:
       - If a rule is semantically similar (cosine of embeddings > threshold) to an existing rule, skip
       - Otherwise add it
    5. If any new rules were added:
       - Load the latest template version, build a new version (`version + 1`), append the new rules
       - Recompute `prompt_fingerprint`
       - Insert the new `TemplateVersion` row
    6. Update `template_extractor_state` with `last_run_at`, `edits_processed`, `rules_added`
  - Idempotent: running again immediately produces no new rules (covered by the "similar rule already exists" check)

### Schema

- New table: `app.template_extractor_state(template_id PK, last_run_at TIMESTAMPTZ, last_edit_id UUID, edits_processed INT, rules_added INT)`
- Migration: `0002_rule_extractor_state.py`

### Scheduler

- `app/jobs/scheduler.py`:
  - Uses `APScheduler` (async)
  - Default schedule: every 6h, run `RuleExtractor.run` for each known template
  - Honors `RULE_EXTRACTOR_INTERVAL_HOURS` env var
  - Starts in `app/jobs/worker.py` lifecycle (single scheduler instance — guarded against multiple worker processes via a Postgres advisory lock so only one worker actually runs the schedule)

### Admin endpoint

- `app/api/routes/admin.py`:
  - `POST /api/admin/rule-extractor/run` `{template_id?: string}` → triggers `RuleExtractor.run` for one or all templates synchronously (within reason — cap at a few minutes); returns the extraction result with the list of new rules
  - `GET /api/admin/templates` → lists all templates with their versions and last extraction time
  - `GET /api/admin/templates/{id}/versions` → version history with diffs of appended rules

### UI hooks (minimal — M11 finishes)

- The admin page exposes a "Re-extract rules now" button hitting the endpoint and displays the result. Enough to demo the loop in a session.

### Tests

- `tests/integration/test_rule_extractor_basic.py`:
  - Seed 5 edits where the operator consistently changed "Smith Industries" → "Smith Industries, LLC"
  - Run `RuleExtractor.run` with a `MockProvider` configured to return `"When mentioning company parties, always use their full legal entity form (e.g., 'Smith Industries, LLC' not 'Smith Industries')."`
  - Assert a new template version is created with this rule appended
  - Assert `prompt_fingerprint` differs from the prior version
- `tests/integration/test_rule_extractor_no_pattern.py` — seed edits with no pattern; mock returns `NO_RULE`; assert no new version created
- `tests/integration/test_rule_extractor_idempotent.py` — run extractor twice; second run produces zero new rules (similar-rule dedup)
- `tests/integration/test_rule_extractor_via_admin.py` — POST to admin endpoint; assert synchronous response includes the new rule
- `tests/integration/test_rule_applied_to_next_draft.py`:
  - Generate draft A; edits made
  - Run rule extractor (mock confirms a rule)
  - Generate draft B; assert the system prompt for draft B contains the new rule (via `Draft.prompt_fingerprint` lookup → template versions table → resolved `system_prompt + rules`)
  - **The full close-the-loop demo test.**

## Acceptance criteria

- [ ] Both scheduler and admin endpoint trigger the extractor
- [ ] New rule → new template version → new fingerprint, observable in DB
- [ ] Re-running with no new edits is a no-op
- [ ] Next draft uses the new rules
- [ ] APScheduler runs only once across workers (advisory lock)
- [ ] No-pattern case is gracefully handled

## Out of scope

- Real fine-tuning / DPO — explicitly out of v1 (documented in `09-risks-tradeoffs.md`)
- Rule deletion / human approval flow before applying — admin can manually edit YAMLs to remove a bad rule and reload
- Cross-template rule generalization — out of v1

## Definition of done

The reviewer can: edit a few drafts, click "Re-extract rules now", see a rule appear, generate another draft and verify the rule is applied (visible in the rendered system prompt on the admin page). `M10-DONE.md` written.

## Sub-agent delegation

Not really — tight coupling between extractor logic, scheduler, and tests.
