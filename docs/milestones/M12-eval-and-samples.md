# M12 — Eval Harness + Sample Documents

**Estimated time:** 2 hours
**Dependencies:** M4 (fixtures), M6 (retrieval), M8 (validation), M9 (edits)
**Rubric impact:** Retrieval and Grounding (visible recall numbers) + Improvement from Edits (visible before/after) + Documentation (real metrics in the writeup)

## Goal

A reproducible eval harness with three scripts producing a `report.md` the reviewer can read. The metrics are not chosen to flatter the system — they're the metrics the rubric implicitly asks about. Numbers are computed against the fixture corpus from M4 plus a small hand-built query set.

The reviewer should be able to run `make eval` and get a fresh report in under 5 minutes.

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-8** (legal-term retrieval test belongs here)
2. `docs/architecture/03-components/retrieval.md`
3. `docs/architecture/03-components/draft-engine.md`
4. `docs/architecture/03-components/edit-loop.md`

## Files to create

### Sample documents (extends M4 fixtures)

If anything is missing from M4's fixture set, add it now:

- `tests/fixtures/docs/scan_clean.pdf` — fake case caption + parties + dates + claims
- `tests/fixtures/docs/scan_skewed.pdf`
- `tests/fixtures/docs/handwriting_excerpt.pdf`
- `tests/fixtures/docs/multi_column.pdf`
- `tests/fixtures/docs/table_heavy.pdf` — title review style with parcel/encumbrance table
- `tests/fixtures/docs/title_review_clean.pdf`
- `tests/fixtures/docs/title_review_messy.pdf` — handwritten margin notes
- `tests/fixtures/docs/corrupt.pdf`

`scripts/generate_fixtures.py` — idempotent script that produces each from raw assets in `tests/fixtures/raw/` (reportlab + Pillow + a deterministic handwriting font).

### Eval data

- `eval/data/retrieval_queries.jsonl` — 30 hand-built queries. Each row:
  ```json
  {
    "query": "§ 1983 civil rights claim",
    "document_id": "scan_clean",
    "expected_chunk_keywords": ["1983", "civil rights", "deprivation"],
    "tag": "legal_term"
  }
  ```
  Mix tags: `legal_term` (NN-8), `proper_noun`, `date`, `numeric`, `prose`. At least 6 per tag.
- `eval/data/extraction_gold.jsonl` — per template + fixture, the expected extracted fields:
  ```json
  {
    "template": "case_fact_summary",
    "document_id": "scan_clean",
    "expected": {
      "parties": [{"name": "Doe, Jane", "role": "plaintiff"}, ...],
      "jurisdiction": "United States District Court, Southern District of New York",
      "filing_date": "2024-03-14",
      "claims": ["42 U.S.C. § 1983", "Fourth Amendment violation"],
      "damages_sought": "$500,000"
    }
  }
  ```
- `eval/data/edit_pairs.jsonl` — 15 synthetic edit pairs per template, used to drive the loop-improvement eval:
  ```json
  {
    "template": "case_fact_summary",
    "field_or_section": "parties",
    "ai": [{"name": "Smith Industries", "role": "defendant"}],
    "user": [{"name": "Smith Industries, LLC", "role": "defendant"}],
    "context_tags": ["company_party"]
  }
  ```

### Eval scripts

- `eval/__init__.py`
- `eval/common.py` — boots the app context, opens DB, instantiates `LLMRouter` with a deterministic seed (temperature=0, fixed model)
- `eval/run_retrieval.py` — loads `retrieval_queries.jsonl`; for each, calls `Retriever.retrieve`; computes:
  - **Recall@5** and **Recall@10** — fraction of queries where the top-K results include a chunk containing every `expected_chunk_keyword` (substring match against `chunks.text`)
  - **MRR** — mean reciprocal rank of the first matching chunk
  - **Per-tag breakdown** (so NN-8's legal_term performance is visible)
  - Writes `eval/reports/retrieval.json` + a markdown table
- `eval/run_citation_validity.py` — for each fixture document × template, runs `DraftEngine.generate`; for each generated section, runs the citation validator; computes:
  - **% supported claims** per draft (and aggregate)
  - **% sections with groundedness ≥ 0.8**
  - **# fabricated chunk_ids** (should be 0)
  - Writes `eval/reports/citation_validity.json` + markdown
- `eval/run_edit_improvement.py` — **the rubric demo eval**:
  1. Run a draft for every fixture × template; record the diff vs. `extraction_gold.jsonl` (call this **baseline edit distance**)
  2. Seed the few-shot store with the `edit_pairs.jsonl` data (via the EditService)
  3. Run rule extraction (via `RuleExtractor.run` on each template)
  4. Run the same drafts again; record diff vs. gold (call this **post-loop edit distance**)
  5. Compute the **improvement ratio** = `1 - (post_distance / baseline_distance)` per template, per field
  6. Writes `eval/reports/edit_improvement.json` + markdown table with baseline / post-loop / Δ
  - Cache LLM responses appropriately so the same draft isn't re-paid for unnecessarily
- `eval/run_all.py` — calls all three; concatenates the markdown reports into `eval/reports/report.md` with a header summarizing key numbers; this is the file the reviewer reads.

### Synthesize edits script

- `scripts/synthesize_edits.py` — loads `edit_pairs.jsonl` and calls `POST /api/drafts/{id}/edit` for synthetic drafts so the few-shot store gets populated in a running stack (used by the demo).

### Tests for the eval harness itself

- `tests/integration/test_eval_retrieval_runs.py` — `run_retrieval` completes on a 3-query subset and writes a JSON report with expected fields
- `tests/integration/test_eval_citation_runs.py` — same for citation validity
- `tests/integration/test_eval_loop_runs.py` — same for edit improvement; assert the improvement_ratio field is in the output

## Acceptance criteria

- [ ] `make eval` runs all three scripts in < 5 min and produces `eval/reports/report.md`
- [ ] `report.md` has at least: per-tag recall@5 table, citation validity table, edit improvement table
- [ ] Legal-term recall@10 ≥ 0.85 (NN-8 verifiable)
- [ ] Fabricated chunk_ids count = 0
- [ ] Edit improvement ratio is > 0 for at least one field (proves the loop closes; rubric-critical)
- [ ] Eval is deterministic on re-run (cached LLM responses match)

## Numbers to target (not hard requirements, but goals)

| Metric | Goal | Why |
|---|---|---|
| Retrieval recall@5 (avg) | ≥ 0.80 | Tells the reviewer the index is doing real work |
| Retrieval recall@5 (legal_term tag) | ≥ 0.85 | Proves NN-8's custom config is paying off |
| % supported claims | ≥ 0.90 | Demonstrates grounding |
| Edit improvement ratio (avg) | ≥ 0.20 | Proves the loop adds value |
| Fabricated citations | 0 | Hard floor |

If a goal is missed, document the gap in the report — partial credit is better than fake numbers.

## Out of scope

- Human-rater evaluation (no time)
- Comparison against a hosted-only baseline (interesting but not asked)
- Statistical significance testing (sample sizes too small to bother)

## Definition of done

`eval/reports/report.md` exists, has real numbers, and is referenced from the top-level README. `M12-DONE.md` written with the headline numbers inlined.

## Sub-agent delegation

After `eval/common.py` and `eval/data/` are populated:

- Sub-agent A: `run_retrieval.py` + its tests
- Sub-agent B: `run_citation_validity.py` + its tests
- Sub-agent C: `run_edit_improvement.py` + its tests

Three eval scripts are fully independent. The `run_all.py` orchestrator and the report writer come after.
