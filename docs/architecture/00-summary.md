# Summary

## The system in one sentence

Turn messy legal-style documents into grounded first-pass drafts an operator can edit, and use those edits to make next week's drafts better.

## Approach

Four pipelines, one data plane, one model router.

1. **Ingest → OCR → Parse.** Route every file by type. Native PDFs go through `pdfplumber` (free, exact). Scans go through PaddleOCR with preprocessing. Handwriting and busted layouts escalate to a vision LLM through the router. Every text span keeps `(page, bbox, confidence)`. Layout parser (`docling`) produces a block tree, not a string blob.
2. **Index → Retrieve.** Semantic chunking on the block tree (not naive token windows). Dual index: BM25 in Postgres FTS + dense embeddings (`bge-large-en-v1.5`) in pgvector. Hybrid retrieval with reciprocal-rank fusion, then `bge-reranker-base` cross-encoder rerank. Top 5–8 chunks survive.
3. **Draft via templates.** A `DraftTemplate` declares: required extraction fields, retrieval queries, prompt skeleton with chunk-ID citation enforcement, output schema, per-claim validators. Two templates ship fully (case fact summary, title review summary). Third (document checklist) is stubbed to prove adding one is config-only.
4. **Edit loop.** Operator edits in the UI. System captures a structured diff per field with full context (template, source chunks, model version, prompt version). Two reuse paths: few-shot retrieval injects similar past edits into the next prompt; an offline LLM analyzer extracts persistent rules and appends them to the template's system prompt. Edit rate per field is the regression metric.

## Key decisions

| Decision | Why |
|---|---|
| Pluggable LLM router (local vLLM default, hosted as escalation) | User constraint; also the right design — same interface across Qwen 2.5 local, Claude, GPT-4o, Gemini. Capability-tier routing, not vendor lock-in. |
| Templates over a fixed draft type | Rubric rewards system design; templates let the same engine produce any of the five suggested draft outputs. |
| Postgres + pgvector for everything | Documents, chunks, embeddings, edits, drafts all in one DB. Transactions across them. Under 5M embeddings, pgvector is fine. No vector-DB sprawl in v1. |
| Hybrid retrieval + reranker, not pure dense | Legal docs have rare terms (party names, citations) that BM25 catches and dense embeddings miss. Reranker is +1 model, big precision win. |
| Citation validation as a post-processing step | The LLM cites by chunk ID; a verifier confirms each cited chunk actually contains the claim. Unsupported claims are flagged in the UI, not silently shipped. |
| Structured-diff edit capture (not raw text diff) | Field-level diffs are reusable signal. Raw text diffs are just history. |

## What this is **not**

- Not multi-tenant. Single workspace.
- No auth in v1 (single-operator demo).
- No fine-tuning. Stage-3 of the edit loop is documented but out of scope for the 3-day build.
- No queue/Celery. In-process background worker via FastAPI `BackgroundTasks` is enough for the demo throughput.
- No legal correctness guarantee. Grounding is the bar; the operator owns correctness.

## Stack at a glance

Python 3.11 · FastAPI · Postgres 16 + pgvector · `pdfplumber` · PaddleOCR · `docling` · `bge-large-en-v1.5` · `bge-reranker-base` · vLLM (Qwen 2.5 14B Instruct) · Anthropic / OpenAI / Gemini SDKs · Next.js 15 (App Router) for the operator UI · Docker Compose for local run.

## Where to read next

- `01-overview.md` — full requirements (functional, non-functional, implicit, out-of-scope)
- `02-architecture.md` — diagram, data flow, component list
- `03-components/` — per-component design
- `04-ai-decisions.md` — every AI touchpoint with the justification block
- `09-risks-tradeoffs.md` — what's deferred, what could fail under scale
