# Edit Loop

**Purpose:** Without this, edits are just user history. The Edit Loop is what turns operator corrections into reusable signal — both for the next call (few-shot) and across many calls (extracted rules).

**Inputs:** A final operator-saved draft (post-edit JSON).
**Outputs:** A structured `Edit` record per changed field/section, embedded into the few-shot store; eventually, an extracted rule appended to the template's `appended_rules`.
**Owns:** Canonical `Edit` records, the few-shot store (`edits.embedding`), the rule extraction state.
**Depends on:** Draft Engine (for the AI output to diff against); LLM Router (for the analysis tier); Embedder (to index edits).
**Failure mode:** A failed embedding does not lose the edit — the reconciliation sweep retries (NN-11). A failed rule extraction is a no-op; the loop continues with few-shot only.

---

## Two stages, deliberately separated

| Stage | Time horizon | Mechanism | Where it shows up |
|---|---|---|---|
| **Stage 1: Few-shot retrieval** | Immediate (next call) | Vector-search similar past edits, inject as in-context examples | Generation prompt for the same template + field/section |
| **Stage 2: Rule extraction** | Aggregated (every 6h or admin-triggered) | LLM analyzes recent edits per template+field, extracts a durable instruction, appends to template's system prompt | New template version with bumped `prompt_fingerprint` |

Stage 1 is the always-on, individual-edit-aware path. Stage 2 is the periodic, pattern-aware path that survives prompt redesign. Both must work; either alone is half the story.

Stage 3 (fine-tuning / DPO on accumulated edits) is **designed for, not built**.

---

## Stage 1 — Structured diff + few-shot

### Diff is field-aware, not text-diff

A raw text diff between `ai_output` and `final_output` produces unusable signal (whitespace, ordering noise). The diff operates on the draft's *structure*:

```python
def compute_diff(template, ai_output, user_output) -> StructuredDiff:
    """
    Per FIELD:
      {
        "field": "parties",
        "ai": [{"name": "Smith Industries", "role": "defendant"}],
        "user": [{"name": "Smith Industries, LLC", "role": "defendant"}],
        "operation": "modified",
        "added_items": [],
        "removed_items": [],
        "modified_items": [{"path": "0.name", "ai": "...", "user": "..."}]
      }
    Per SECTION:
      Char-level diff via difflib, collapsed into replace/insert/delete operations.
    Excluded: unchanged fields/sections.
    """
```

### Edit row carries full context

Every edit row stores:

- `template_id`, `template_version`, `prompt_fingerprint` — NN-5 integrity
- `field_or_section_name`, `field_type`
- `ai_value`, `user_value`, `diff` (JSONB)
- `context = {source_chunks: [chunk_id, ...], model_used, retrieved_queries, tokens_in, tokens_out}` — snapshotted at edit time
- `embedding VECTOR(1024)` (nullable)
- `few_shot_indexed_at TIMESTAMPTZ` (NN-11)

The `context` JSONB is what makes the few-shot store retrievable later — without it, you have an AI/user pair with no notion of *when* this kind of edit applies.

### Few-shot indexing

After save, an `index_edit` job is enqueued (immediate path) that:
1. Builds a "search representation" string: `ai_value || " --> " || user_value || " || " || context_tags`
2. Embeds it via the same embedder used by retrieval
3. Writes `embedding` and `few_shot_indexed_at`

Failures (transient embedder timeout) are caught by the **reconciliation sweep** (NN-11) — a periodic query for `few_shot_indexed_at IS NULL AND created_at < NOW() - INTERVAL '1 minute'` that re-enqueues.

### Few-shot retrieval

At draft-generation time, for each field and section about to be generated, the Generator calls:

```python
examples = await few_shot_store.retrieve(
    template_id=template.id,
    field_or_section=name,
    current_context=embedding_of_currently_retrieved_chunks,
    top_k=3,
)
```

Filters: same template, same field/section, `few_shot_indexed_at IS NOT NULL`. Then a pgvector similarity search ordered by cosine to the current-context embedding. Returns up to 3 examples.

These get injected into the generation prompt as:

```
Past edits relevant to this section (most similar first):

Example 1:
  AI draft was:
    """{ai_value (truncated)}"""
  Operator changed it to:
    """{user_value (truncated)}"""

(2 more examples...)

Apply these patterns when they fit. Do not copy verbatim if they don't.
```

This is the "system learns from your last edit" experience. Zero training. Immediate effect. Surprisingly strong.

---

## Stage 2 — Rule extraction

The Rule Extractor is offline (cron + admin endpoint, per NN-9). It reads edits since the last run, groups by `(template_id, field_or_section)`, and for each group with enough edits (default ≥ 3) asks the LLM to extract a *durable* rule.

### Prompt shape

```
You are reviewing how a legal operator edits AI-generated drafts to extract a
durable house-style rule.

Below are {N} cases where the AI produced one value and the operator changed
it to another. Look for a CONSISTENT pattern. If you find one, output ONE
concise rule the AI can follow next time, phrased as an instruction.

If no consistent pattern emerges, output: NO_RULE.

Cases:
1. AI said: "Smith Industries" — operator changed to: "Smith Industries, LLC"
2. AI said: "Acme Corp" — operator changed to: "Acme Corp., Inc."
3. AI said: "TechCo" — operator changed to: "TechCo Holdings, LLC"

Rule:
```

Expected good output: `"When mentioning company parties, always include their full legal entity form (e.g., LLC, Inc., Corp.) as it appears in the documents."`

### Idempotency via similarity dedup

Before appending, the new rule is embedded and compared to existing `appended_rules` for the template. If cosine similarity > 0.85 with any existing rule, skip. This makes re-running the extractor a no-op when no new patterns exist.

### Template version bump

If at least one new rule is added:
1. Build a new `TemplateVersion` row with `version = current + 1` and the new `appended_rules` list
2. Recompute `prompt_fingerprint` (NN-5) — `sha256(system_prompt + appended_rules + extraction_schema + sections)`
3. Insert. The next `DraftEngine.generate()` for this template loads this latest version.

`template_extractor_state(template_id, last_run_at, last_edit_id, edits_processed, rules_added)` tracks progress so each run only processes new edits.

---

## What this is **not**

- **Not fine-tuning.** The LLM weights never change. All learning lives in retrieval (Stage 1) and prompts (Stage 2).
- **Not real-time after the first edit.** Stage 1 needs the embedding to land (typically < 1s) and Stage 2 runs every 6h or on admin click. The "loop closes" demo uses the admin endpoint for an immediate effect.
- **Not cross-template.** Edits to `case_fact_summary` don't influence `title_review_summary`. Could be a v1.5 feature; not v1.

---

## Interface

```python
class EditService:
    async def save_edit(self, draft_id: UUID, user_output: dict) -> list[Edit]: ...
    async def metrics(self, template_id: str, days: int = 30) -> EditMetrics: ...

class FewShotStore:
    async def index(self, edit_id: UUID) -> None: ...
    async def retrieve(
        self, template_id: str, field_or_section: str,
        current_context_embedding: np.ndarray, top_k: int = 3,
    ) -> list[FewShotExample]: ...
    async def backfill_unindexed(self) -> int: ...   # NN-11 reconciler hook

class RuleExtractor:
    async def run(self, template_id: str, since: datetime | None = None) -> ExtractionResult: ...
```

## Metrics that matter

- **Edit rate per field** = edits-touching-field / drafts-using-template-with-field. The single rubric-relevant metric.
- **Edit rate trend** — is it going down for fields the rule extractor has touched?
- **Few-shot hit rate** — % of generations where ≥ 1 few-shot was retrieved.
- **Rule application rate** — % of generations where the system prompt's `appended_rules` is non-empty.

All exposed at `/admin/edit-metrics` for the demo dashboard.

## Open questions

- **Operator approval before rule applies?** Right now, the extractor writes rules directly to the template. A human-in-the-loop "approve this rule" step is safer; deferred to v1.1 because the demo loop needs to close *visibly* in a session.
- **Rule conflict detection.** If a new rule contradicts an old one, neither dedup nor blind append is right. v1: blind append, document the limitation. v1.1: detect via LLM comparison and surface to admin.
- **Per-operator rules vs. global.** v1 is single-tenant single-operator. Multi-operator would need to either segment edit history or fold into one global rule set — choice depends on whether operators share house style.
