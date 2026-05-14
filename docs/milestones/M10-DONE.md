# M10 Rule Extractor — Done

## What shipped

Stage-2 edit loop: an offline rule extractor that groups recent operator edits per template,
asks the `analysis`-tier LLM for a generalizable rule, and (on novel rule) inserts a new
`TemplateVersion` with `version+1` and a recomputed `prompt_fingerprint`.

### New files

- `app/db/migrations/versions/0006_rule_extractor_state.py` — Alembic migration for `app.template_extractor_state`
- `app/db/models/template_extractor_state.py` — ORM model
- `app/edits/rule_extractor.py` — `RuleExtractor`, `ExtractionResult`, and all private helpers
- `app/jobs/scheduler.py` — `RuleExtractorScheduler` (APScheduler 3.x wrapper)
- `app/core/trace.py` — `current_trace_id()` extracted from `edits.py`
- `app/api/schemas/admin.py` — Pydantic models for admin endpoints

### Modified files

- `app/draft/templates/registry.py` — `append_rules()` method added
- `app/db/session.py` — `direct_session_factory()` lazy singleton (pool_size=2, bypasses pgbouncer)
- `app/jobs/worker.py` — starts/stops `RuleExtractorScheduler` in the worker process
- `app/api/routes/admin.py` — three new endpoints: `POST /admin/rule-extractor/run`, `GET /admin/templates`, `GET /admin/templates/{id}/versions`
- `app/api/routes/edits.py` — imports `current_trace_id` from `app.core.trace` (pure refactor)
- `app/api/deps.py` — `get_rule_extractor` async generator dependency
- `app/core/errors.py` — `RuleExtractorBusyError` (409), `LLMAnalysisFailedError` (502)
- `app/settings.py` — 8 new `RULE_EXTRACTOR_*` settings
- `pyproject.toml` — `apscheduler>=3.10,<4`

### Tests added

**Unit tests (all passing):**
- `tests/unit/test_rule_extractor_parse.py` — 8 tests for `_parse_rule`
- `tests/unit/test_rule_extractor_prompt.py` — 6 tests for prompt building / case rendering
- `tests/unit/test_rule_similarity.py` — 4 tests for `_is_similar`

**Integration tests:**
- `tests/integration/test_rule_extractor_basic.py`
- `tests/integration/test_rule_extractor_no_pattern.py`
- `tests/integration/test_rule_extractor_idempotent.py`
- `tests/integration/test_rule_extractor_min_edits.py`
- `tests/integration/test_rule_extractor_version_lookback.py`
- `tests/integration/test_rule_extractor_advisory_lock.py`
- `tests/integration/test_rule_extractor_via_admin.py`
- `tests/integration/test_rule_applied_to_next_draft.py`

## Deviations from plan

1. **Migration numbering:** `docs/milestones/M10-rule-extractor.md` referenced `0002_rule_extractor_state.py`. Actual migration is `0006_rule_extractor_state.py` (follows the 5 prior migrations).

2. **`_re._try_acquire` / `_re._release` patching in integration tests:** Rather than spinning up a real two-connection advisory lock test in every CI environment, the basic/no-pattern/min-edits/idempotent/version-lookback tests mock these functions. The advisory lock behavior is tested end-to-end in `test_rule_extractor_advisory_lock.py` which uses real direct connections.

3. **`no_edits` is not in spec's skipped_reason enum but was added** to cleanly distinguish "template not found" from "no edits in window" — both result in no work but have different log events. The `ExtractionResultRow` schema accepts `None` as "ran to completion" and the three string literals for early exit.

## Sample admin-endpoint output

```json
POST /admin/rule-extractor/run
{"template_id": "case_fact_summary"}

→ 200 OK
{
  "results": [
    {
      "template_id": "case_fact_summary",
      "skipped_reason": null,
      "edits_processed": 5,
      "groups_evaluated": 1,
      "groups_skipped_min_edits": 0,
      "new_rules": ["When listing parties, always include their role in parentheses."],
      "new_version": 2,
      "prompt_fingerprint": "a1b2c3...",
      "trace_id": "01932f..."
    }
  ],
  "partial": false
}
```

## GET /admin/templates/{id}/versions — v2 resolved_system_prompt

```json
{
  "template_id": "case_fact_summary",
  "versions": [
    {"version": 1, "appended_rules": [], "resolved_system_prompt": "You are a legal...", ...},
    {
      "version": 2,
      "appended_rules": ["When listing parties, always include their role in parentheses."],
      "rules_added_vs_previous": ["When listing parties, always include their role in parentheses."],
      "resolved_system_prompt": "You are a legal...\n\nWhen listing parties, always include their role in parentheses.",
      ...
    }
  ]
}
```

## Operational note: YAML reload and extracted rules

`TemplateRegistry.sync_to_db()` inserts a new template version whenever the YAML fingerprint
changes. If an operator edits the YAML file and the worker restarts, `sync_to_db` writes a
new version using only the YAML's `appended_rules` (typically `[]`), dropping any rules
extracted by M10.

**Mitigation (v1):** Do not reload YAML after M10 has extracted rules. To remove a bad
extracted rule, delete the row from `app.templates` directly or wait for the rule-deletion
endpoint (out of scope for M10).

**Proper fix (follow-up):** Split `appended_rules` into `yaml_rules + extracted_rules`
columns so `sync_to_db` only overwrites `yaml_rules`.
