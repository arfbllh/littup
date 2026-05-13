# M13 — Submission

**Estimated time:** 1 hour
**Dependencies:** all previous milestones complete
**Rubric impact:** Documentation (5 pts directly) + the difference between a great system and a great submission

## Goal

Polish everything that's visible to the reviewer. README runs from a cold clone to a working demo in under 15 minutes. The demo script is rehearsed and works. ADRs explain the non-obvious decisions. The reviewer's path from "git clone" to "I understand this system" is short and pleasant.

This is the milestone where you stop building and start delivering.

## Context Claude Code must read

1. Every `M<N>-DONE.md` file
2. `docs/architecture/00-summary.md`
3. `eval/reports/report.md`
4. The assessment PDF (`AI_Engineer_-_Assessment.pdf`) one more time to verify nothing is missed

## Non-Negotiables that apply

None new. Every NN should already be tested and documented.

## Files to create / modify

### Top-level README

- `README.md` — opens with:
  - **One-paragraph what** — "A system that turns messy legal-style PDFs into grounded, template-driven first drafts and learns from operator edits."
  - **One-paragraph why this design** — the two key bets (templates over a fixed draft type; pluggable LLM router with local default)
  - **Quickstart** — 6 commands max, copy-paste-runnable:
    ```bash
    git clone <repo>
    cd littup
    cp .env.example .env  # fill in API keys for hosted providers (optional if you have a GPU)
    make up               # docker compose up -d (api, worker, postgres, pgbouncer, vllm if GPU)
    make migrate          # apply migration 0001
    make seed             # load fixture documents
    open http://localhost:3000   # UI
    ```
  - **Demo path** — link to `docs/DEMO.md`
  - **What's inside** — pointers to `docs/architecture/`, `eval/reports/report.md`, `docs/runbook.md`, `docs/ADR/`
  - **Key numbers from the eval** — three rows: retrieval recall@5, % supported claims, edit improvement ratio
  - **Limitations & tradeoffs** — frank one-paragraph statement of what v1 isn't
  - **Tech stack at a glance**

### Demo script

- `docs/DEMO.md` — a tight 5-minute walkthrough:
  ```
  0:00–0:30  Show README quickstart; point at the running app
  0:30–1:30  Upload a mixed batch (native_clean.pdf + scan_skewed.pdf + handwriting_excerpt.pdf)
             Show pipeline progress in real time via SSE
             Point at the document detail page, OCR'd text alongside the page image
  1:30–3:00  Generate a case_fact_summary draft against the 3 docs
             Open the draft view; click a citation; show the bbox highlight
             Point at the groundedness score and validation markers
  3:00–4:00  Toggle edit mode; change "Smith Industries" → "Smith Industries, LLC"
             Save; show the edit appearing in the admin edit-rate dashboard
             Click "Re-extract rules now"; show the new rule appearing on the template
  4:00–5:00  Generate a second draft against a similar document
             Show that the rule has been applied and the same edit is no longer needed
             Point at eval/reports/report.md numbers
  ```

### Runbook

- `docs/runbook.md` — operational doc:
  - How to add a new template (one YAML file in `config/templates/`)
  - How to add a new LLM provider (one file in `app/llm/providers/`)
  - How to swap embedder (set env var)
  - How to raise VLM budget (config)
  - How to clear the LLM cache
  - How to reset the database
  - How to read the logs
  - Common failure modes and what to check (job stuck, embedding lagging, vLLM down)

### Architecture Decision Records

- `docs/ADR/001-postgres-for-everything.md` — why not a separate vector DB; revisit at 5M+ chunks
- `docs/ADR/002-llm-router-tiers.md` — why capability tiers, not vendor tiers; how to swap
- `docs/ADR/003-template-snapshot.md` — NN-5 rationale; the bug it prevents
- `docs/ADR/004-job-queue-not-celery.md` — why Postgres FOR UPDATE SKIP LOCKED beats Redis + Celery for v1
- `docs/ADR/005-hybrid-retrieval.md` — why BM25 + dense + trigram + rerank, not pure dense
- `docs/ADR/006-edit-loop-two-stages.md` — why few-shot AND rule extraction, not just one

Each ADR is ~1 page: Context, Decision, Consequences, Alternatives considered, When to revisit.

### Sample inputs/outputs

- `docs/samples/` directory with:
  - `case_fact_summary_input/` — copy of the 3 fixture documents used
  - `case_fact_summary_output.json` — the actual generated draft (pretty-printed)
  - `case_fact_summary_output.md` — same draft rendered as markdown with citations as footnotes
  - Same for `title_review_summary`

### Submission email

- `docs/submission_email.md` — the email body to send to `talha@ideabuilders.studio`:
  ```
  Subject: AI Engineer Assessment — <your name>

  Hi Talha,

  Please find my completed assessment at <github repo URL>. I've invited
  @tsensei and @abubakarsiddik31 as collaborators.

  Quick orientation:
    - README.md → quickstart in < 15 min
    - docs/DEMO.md → 5-minute walkthrough
    - docs/architecture/ → design docs
    - eval/reports/report.md → measured results

  Key bets I made and why:
    - Built templates rather than one draft type, so the same engine handles
      case_fact_summary, title_review_summary, and is extensible to the others
      via YAML alone.
    - LLM Router with local vLLM as default and hosted (Claude / OpenAI /
      Gemini) as escalation, swappable by config. Specialized models stay
      specialized (PaddleOCR, bge-large embedder, bge-reranker-base).
    - Stage-1 (few-shot retrieval) + Stage-2 (LLM-extracted rules) for the
      improvement loop; fine-tuning is designed-for, not built.

  Happy to walk through any of it on a call.

  Best,
  <your name>
  ```

### Final repo hygiene

- `.github/workflows/ci.yml` — runs `pytest`, `ruff check`, `mypy` on push (skipped if missing time; nice-to-have)
- `LICENSE` — MIT or Apache 2.0
- `CONTRIBUTING.md` — short stub pointing at the milestone docs
- Remove dead code, TODO comments that reference completed work, stray prints
- Verify `make up && make migrate && make seed && make eval` works from a clean container

### Pre-submission checklist (run before pushing)

- [ ] Cold-clone the repo to a fresh directory, follow README, reach a working demo in < 15 min
- [ ] `pytest` passes
- [ ] `make eval` produces a fresh report
- [ ] `make lint` clean
- [ ] No secret keys in the repo (`git secrets` or similar pass)
- [ ] No `.env` committed
- [ ] All `M<N>-DONE.md` files exist
- [ ] Sample inputs/outputs in `docs/samples/`
- [ ] Demo walkthrough rehearsed once end-to-end
- [ ] GitHub collaborators invited (`tsensei`, `abubakarsiddik31`)
- [ ] Email drafted in `docs/submission_email.md` and ready to send
- [ ] All commits authored under your real name and a real email

## Acceptance criteria

- [ ] Cold-clone → working demo path verified by you, end-to-end, on the laptop you'll demo from
- [ ] README is the *first* thing a reviewer needs and *nothing else* is needed first
- [ ] Demo script timing checked with a stopwatch; under 5 min
- [ ] At least 4 ADRs written
- [ ] Sample outputs are committed and pretty

## Out of scope

- Marketing language. "Revolutionary AI-powered legal automation" stays out of the README. Honest description.
- Animated GIFs / screencasts in the README (nice-to-have, do if time permits)
- A live-deployed demo URL — not asked for, not worth the operational risk

## Definition of done

The repo is on GitHub. The collaborators are invited. The email is sent. You close the laptop. `M13-DONE.md` is the last file written, and it contains the GitHub URL and the timestamp of the submission email.

## Sub-agent delegation

Not really. README, DEMO, runbook, ADRs are sequential thinking, not parallel typing. You want one voice across the docs.

---

## One final thought

The reviewer is going to spend ~30 minutes on your submission. Half of that is reading the README and clicking around the UI. The other half is reading the code. The README and the DEMO script are *not* an afterthought — they are the surface area where the rubric's "Documentation" and "Code Quality and System Design" categories actually get scored.

Spend the full hour. Don't ship at 90%.
