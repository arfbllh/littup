# Overview

## Problem statement

An operator at a small legal practice receives a folder of mixed legal-style documents — some clean digital PDFs, some scanned and rotated, some with handwritten margins, all formatted differently. They need a usable first draft (a case fact summary, title review, checklist, etc.) on the desk in minutes, not hours. The draft must be auditable: every claim has to point back to the source. And when the operator edits the draft (because they will), those edits should make the next draft better, not vanish into a git log.

## One-sentence framing

> This system turns **messy legal-style PDFs** into **grounded, template-driven drafts with inspectable citations**, and uses **operator edits as labeled training signal** to improve subsequent drafts.

## Functional requirements

1. **Ingest** multiple files at once (PDF primary; PNG/JPG/TIFF/DOCX secondary).
2. **Classify** each document by quality (native text, clean scan, low-quality scan, handwriting-heavy, mixed) before processing.
3. **Extract text and structured fields** with coordinate preservation per span.
4. **Build a block tree** (sections, paragraphs, tables, figures, captions, lists) — not a flat string.
5. **Index** each document into a hybrid (BM25 + dense) searchable corpus.
6. **Retrieve** the top-N chunks relevant to a draft template's queries, with reranking.
7. **Generate** a draft using a `DraftTemplate` — extraction schema + prompt + citation enforcement + output schema.
8. **Validate** each claim against its cited chunk; flag unsupported claims in the UI.
9. **Render** the draft with inline, clickable citations that highlight the source span.
10. **Accept operator edits**, capture a structured per-field diff with full context.
11. **Reuse edits** in two ways: few-shot example retrieval into future prompts; offline rule extraction appended to template system prompts.
12. **Switch the underlying LLM** between local (vLLM) and hosted (Anthropic / OpenAI / Gemini) without touching business logic.

## Non-functional requirements

| Concern | Target | Notes |
|---|---|---|
| Latency, ingest → draft ready | < 60s p95 for a 20-page doc | Driven by OCR; vLLM batching helps |
| Latency, draft regen after edits | < 15s p95 | Smaller-context regeneration |
| Throughput, MVP | 1 concurrent operator, 10 docs/hour | Demo bar |
| Throughput, 10× | 10 operators, 100 docs/hour | Documented scaling path, not built |
| OCR accuracy on clean scans | ≥ 95% CER | PaddleOCR baseline; verified on sample set |
| Retrieval recall@10 | ≥ 0.85 on a small eval set | Hybrid + rerank |
| Grounding rate | 100% of claims cited; ≥ 95% of citations verified | Hard requirement; validator catches the rest |
| Cost per draft, hosted path | < $0.50 | Sum of LLM + embedding + reranker calls |
| Cost per draft, local path | ~$0 marginal | Compute already paid |
| Availability | Best-effort for demo | No SLA |

## Implicit requirements (not stated, but required to ship)

- A way to inspect a document and its extracted blocks (debug page in the UI).
- Persistence across restarts — restart shouldn't lose drafts or edits.
- Versioned prompts and template configs (so an edit logged against `template_v3` doesn't get reused under `template_v4` without a check).
- Versioned model identifiers in every generation log entry (provider + model name + parameters).
- Idempotent ingestion (re-uploading the same file by hash doesn't double-index).
- Structured error responses — never a 500 with an HTML page; every failure is a typed error code the UI can render.
- Local-only by default — no document leaves the machine unless the hosted path is explicitly enabled per draft.

## Out of scope (v1)

- Multi-tenant isolation, SSO, RBAC, audit trail UI.
- Fine-tuning / DPO from accumulated edits — designed for, not built.
- Cross-document reasoning beyond a single matter / case bundle.
- Live collaborative editing (two operators on the same draft).
- Mobile UI; landscape desktop only.
- Real production observability (Prometheus / OpenTelemetry / Sentry are documented in `08-infra-slo.md`, not wired up for the demo).
- Legal correctness verification. The system grounds, the human owns correctness.

## Success criteria

A reviewer can:

1. Drop a folder of mixed messy PDFs and a chosen template into the system.
2. Get a structured draft back in under a minute with every claim citing a chunk that visibly supports it.
3. Edit the draft, save it, generate a second draft for a similar input, and see that the second draft applied the pattern from the first edit (either as a retrieved few-shot, an extracted rule visible in the rule store, or a measurable drop in the same-field edit rate).
4. Swap the LLM backend from vLLM to Anthropic via config (one env var, no code change) and re-run the same pipeline.
5. Read the architecture and the code and feel they could start contributing on Monday.

## Stated assumptions

- Documents are English. (Multilingual is a v1.5 concern.)
- Per-document size is bounded (< 200 pages) — multi-thousand-page corpora are out of scope for MVP.
- The operator is a single trusted user — no adversarial inputs, but malformed files are still handled gracefully.
- A local GPU (single 24GB+ card, e.g. RTX 4090 / L4 / A10) is available for vLLM. If absent, the router transparently uses a hosted model for generation while keeping embeddings and reranker on CPU.
