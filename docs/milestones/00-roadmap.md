# Milestone Roadmap

This is the build plan. Each milestone is a separate spec doc in this folder (`M0-bootstrap.md` … `M13-submission.md`) that you feed to Claude Code as the unit of work.

## How to use a milestone with Claude Code

For each milestone, paste this into Claude Code:

```
Context files to read first (in this order):
  1. docs/architecture/00-summary.md
  2. docs/architecture/10-fixes-and-non-negotiables.md     ← always
  3. docs/architecture/project-skeleton.md
  4. docs/architecture/02-architecture.md
  5. <component docs listed in this milestone>
  6. <previous milestone(s) listed as dependencies>

Then read this milestone spec and produce:
  a) PLAN.md  — your implementation plan, the files you'll touch,
                acceptance check, risk notes. Do not write code yet.
  b) After I approve PLAN.md, implement everything in the spec.
  c) Tests in tests/ as listed in the spec.
  d) Update docs/milestones/M<N>-DONE.md with what shipped, deviations, and follow-ups.

Honor every NN-N rule listed under "Non-Negotiables that apply".
```

You review the `PLAN.md`, approve or push back, then approve the build. Three artifacts per milestone: **spec → plan → implementation + tests**.

## Dependency graph

```mermaid
flowchart LR
    M0[M0: Bootstrap] --> M1[M1: DB + Jobs]
    M1 --> M2[M2: LLM Router]
    M1 --> M3[M3: Ingestion API]
    M2 --> M4[M4: OCR Pipeline]
    M3 --> M4
    M4 --> M5[M5: Layout + Chunking]
    M1 --> M5
    M2 --> M6[M6: Retrieval]
    M5 --> M6
    M2 --> M7[M7: Draft Engine]
    M6 --> M7
    M7 --> M8[M8: Citation Validation]
    M8 --> M9[M9: Edit Capture + Few-shot]
    M9 --> M10[M10: Rule Extractor]
    M7 --> M11[M11: UI]
    M8 --> M11
    M9 --> M11
    M10 --> M12[M12: Eval + Samples]
    M11 --> M12
    M12 --> M13[M13: Submission]
```

## Schedule (Wed May 13 → Fri May 15)

| Day | Hrs | Milestones | Rubric points it unlocks |
|---|---|---|---|
| **Wed eve** | 5–6h | M0, M1, M2 | Foundation; no rubric points yet but unblocks everything |
| **Thu** | 9–10h | M3, M4, M5, M6 | Document Processing (25 pts) + Retrieval recall side of (25 pts) |
| **Fri AM** | 5h | M7, M8, M9 | Draft Quality (10 pts) + Grounding side of (25 pts) + start of Edits (25 pts) |
| **Fri PM** | 5h | M10, M11, M12, M13 | Improvement from Edits (25 pts) + Documentation (5 pts) + Code Quality (10 pts) |

Hours are work-on-the-system; Claude Code is doing most of the typing, so much of this is reading PLAN.md, pushing back, and verifying tests. The biggest risk is M4 (OCR) and M11 (UI) — both have failure modes that eat hours. Mitigations in their individual specs.

## Milestone list at a glance

| # | Milestone | Est. | NN-N rules wired | Rubric category |
|---|---|---|---|---|
| **M0** | Bootstrap: repo, deps, FastAPI hello, Docker, logging, errors | 1h | NN-12 | Code Quality |
| **M1** | Database + job queue + migrations + pgbouncer | 2h | NN-1, NN-3, NN-4, NN-8 | Code Quality |
| **M2** | LLM Router + providers + cache + budget | 2h | NN-6, NN-7, NN-12 | Code Quality |
| **M3** | Ingestion API + idempotency + reconciler + SSE | 1.5h | NN-1, NN-2, NN-3, NN-10 | Document Processing |
| **M4** | OCR pipeline: classifier + extractors + VLM with budget | 3h | NN-6 | Document Processing |
| **M5** | Layout parsing + semantic chunking + entity extraction + embedding | 2h | NN-1, NN-8 | Document Processing |
| **M6** | Retrieval: BM25 + dense + tri-gram + RRF + reranker | 2h | NN-4, NN-8 | Retrieval + Grounding |
| **M7** | Draft Engine: templates + extraction + generation | 3h | NN-5 | Draft Quality |
| **M8** | Citation Validation: per-claim verifier, unsupported flagging | 1.5h | — | Retrieval + Grounding |
| **M9** | Edit capture + structured diff + few-shot store | 2h | NN-5, NN-11 | Improvement from Edits |
| **M10** | Rule Extractor + scheduler + admin endpoint | 1.5h | NN-9 | Improvement from Edits |
| **M11** | UI: upload, draft view with click-cites, edit, admin | 3h | NN-10 | Documentation |
| **M12** | Sample documents + eval harness + report | 2h | NN-8 | Retrieval + Edits + Docs |
| **M13** | README + architecture pointers + demo script + submission | 1h | — | Documentation |

**Total: ~27h.** With Claude Code, realistic at ~20h for you (you're reviewing, not typing).

## When to delegate to sub-agents

Claude Code supports sub-agents (parallel work via the Task tool / subagent prompts). Use them where the work is naturally independent:

- **M2 — Providers as sub-agents.** Each `LLMProvider` implementation (`vllm.py`, `anthropic.py`, `openai.py`, `gemini.py`) is independent. Spawn one sub-agent per provider after the base interface and router are merged. Each follows the same pattern, so they parallelize cleanly.
- **M4 — OCR engines as sub-agents.** `pdfplumber_ocr.py`, `paddle_ocr.py`, `vlm_ocr.py`, `preprocess.py` are independent. Same pattern.
- **M11 — UI screens as sub-agents.** Upload page, Document detail, Draft view, Admin can each be built by a sub-agent after the API client (`ui/lib/api.ts`) is shared.
- **M12 — Eval scripts as sub-agents.** `run_retrieval.py`, `run_citation_validity.py`, `run_edit_improvement.py` are independent.

When in doubt, **don't** spawn sub-agents. Sequential debugging is faster than parallel debugging when you're learning the codebase.

## What to do if you fall behind

**Cut, in this order:**

1. **First cut: M11 → Streamlit.** A 200-line Streamlit app does the demo. Lose visual polish, keep functionality. Saves ~2h.
2. **Second cut: M10 rule-extractor sophistication.** Stage 1 (few-shot retrieval, which is M9) is the minimum credible improvement loop. M10 stage-2 (LLM-extracted rules) is the second half. Ship stage 1 well; ship stage 2 with a working endpoint but a simple clustering rule.
3. **Third cut: M4 VLM fallback.** Ship without VLM escalation. PaddleOCR handles 90% of realistic samples. Document the gap; the per-document budget cap (NN-6) still applies to make the architecture coherent.
4. **Never cut: M1's job queue, M3's idempotency, M5's reconciliation gate, M8's citation validator, M9's structured diff.** These are what the rubric is actually testing.

## Submission checklist (M13)

- [ ] Repo on GitHub, `tsensei` and `abubakarsiddik31` invited
- [ ] `README.md` runs from a cold clone to a working demo in < 15 min
- [ ] `docs/architecture/` complete (this directory, copied in)
- [ ] `docs/DEMO.md` walks the reviewer through: upload → draft → edit → see-the-loop in < 5 min
- [ ] `eval/reports/` contains the latest eval run
- [ ] `tests/` passes `pytest`
- [ ] Sample documents in `tests/fixtures/docs/` covering all OCR cases
- [ ] Sample input/output pairs in `docs/samples/`
- [ ] Email sent to `talha@ideabuilders.studio` with repo link + intro
