# Project Skeleton

The exact file layout Claude Code will build into. Every milestone creates or modifies files inside this tree — none introduce new top-level concerns. Designed so:

- Each subsystem (ingest, retrieval, draft, edits, llm, jobs) is a Python package with a clear surface.
- AI providers are pluggable behind interfaces in `app/llm/providers/`.
- Templates are config files in `config/templates/`, not code.
- Migrations live in `app/db/migrations/`.
- `docs/milestones/` holds the build plan; `docs/architecture/` holds the design (copy of `/mnt/user-data/outputs/architecture/`).
- The UI is a separate Next.js app in `ui/` — could be swapped for Streamlit if time runs out.

```
littup/                                   # repo root
├── README.md                             # Setup, run, demo script (M13)
├── docker-compose.yml                    # api, worker, postgres, pgbouncer, vllm, ui (M0/M1)
├── Dockerfile                            # Python service image (M0)
├── Dockerfile.ui                         # Next.js image (M11)
├── pyproject.toml                        # uv / pdm / poetry — pick one (M0)
├── uv.lock                               # locked deps (M0)
├── .env.example                          # Documented env vars (M0)
├── .gitignore
├── Makefile                              # `make up`, `make test`, `make eval`, `make seed`
│
├── config/
│   ├── router.yaml                       # LLM router tier config (M2)
│   ├── ocr.yaml                          # OCR thresholds, VLM caps (M4)
│   └── templates/
│       ├── case_fact_summary.yaml        # M7
│       ├── title_review_summary.yaml     # M7
│       └── document_checklist.yaml       # M7 stub
│
├── app/                                  # Python package
│   ├── __init__.py
│   ├── main.py                           # FastAPI app entrypoint (M0)
│   ├── settings.py                       # Pydantic settings (M0)
│   │
│   ├── core/                             # Cross-cutting, no domain logic
│   │   ├── __init__.py
│   │   ├── logging.py                    # structlog setup (M0)
│   │   ├── errors.py                     # AppError hierarchy (M0)
│   │   ├── middleware.py                 # request_id, error handler (M0)
│   │   └── ids.py                        # uuid7, sha256 helpers
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── session.py                    # async SQLAlchemy session (M1)
│   │   ├── models/                       # ORM models — one file per aggregate
│   │   │   ├── __init__.py
│   │   │   ├── document.py               # Document, Page, Block, Span (M1)
│   │   │   ├── chunk.py                  # Chunk (M1)
│   │   │   ├── draft.py                  # Draft, Citation (M1)
│   │   │   ├── edit.py                   # Edit (M1)
│   │   │   ├── template.py               # TemplateVersion (M1, populated M7)
│   │   │   ├── job.py                    # Job, JobHistory (M1)
│   │   │   └── llm_log.py                # LLMRequest, LLMCache (M1)
│   │   └── migrations/                   # alembic (M1)
│   │       ├── env.py
│   │       ├── script.py.mako
│   │       └── versions/
│   │
│   ├── api/                              # HTTP layer — thin
│   │   ├── __init__.py
│   │   ├── deps.py                       # FastAPI deps (db session, router, etc.)
│   │   ├── schemas/                      # Pydantic request/response models
│   │   │   ├── __init__.py
│   │   │   ├── documents.py
│   │   │   ├── drafts.py
│   │   │   ├── edits.py
│   │   │   └── common.py
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── health.py                 # /healthz, /readyz (M0)
│   │   │   ├── documents.py              # upload, list, get, blocks, SSE (M3)
│   │   │   ├── drafts.py                 # generate, get, regenerate (M7)
│   │   │   ├── edits.py                  # save edit (M9)
│   │   │   ├── templates.py              # list templates (M7)
│   │   │   └── admin.py                  # llm-stats, rule-extractor trigger (M10)
│   │   └── sse.py                        # SSE helpers with Last-Event-ID (M3)
│   │
│   ├── ingest/                           # M3–M5
│   │   ├── __init__.py
│   │   ├── service.py                    # IngestService — orchestration (M3)
│   │   ├── classifier.py                 # native/scan/handwriting heuristic (M4)
│   │   ├── ocr/
│   │   │   ├── __init__.py
│   │   │   ├── base.py                   # OCRProvider protocol (M4)
│   │   │   ├── pdfplumber_ocr.py         # Native PDF text path (M4)
│   │   │   ├── paddle_ocr.py             # PaddleOCR engine (M4)
│   │   │   ├── vlm_ocr.py                # VLM fallback via router (M4)
│   │   │   ├── preprocess.py             # Deskew, denoise, binarize (M4)
│   │   │   └── routing.py                # Per-page routing decisions (M4)
│   │   ├── layout.py                     # docling wrapper → block tree (M5)
│   │   ├── chunker.py                    # Semantic chunker + entity extraction (M5)
│   │   └── reconciler.py                 # NN-1, NN-11 sweep (M3)
│   │
│   ├── retrieval/                        # M6
│   │   ├── __init__.py
│   │   ├── retriever.py                  # Hybrid retriever (M6)
│   │   ├── bm25.py                       # Postgres FTS with legal_en config (M6)
│   │   ├── dense.py                      # pgvector ANN (M6)
│   │   ├── trigram.py                    # pg_trgm proper-noun fallback (M6)
│   │   ├── fusion.py                     # RRF + entity-overlap fold (M6)
│   │   └── reranker.py                   # bge-reranker wrapper (M6)
│   │
│   ├── draft/                            # M7–M8
│   │   ├── __init__.py
│   │   ├── engine.py                     # DraftEngine.generate (M7)
│   │   ├── extractor.py                  # Field extraction pass (M7)
│   │   ├── generator.py                  # Section generation pass (M7)
│   │   ├── validator.py                  # Citation validation (M8)
│   │   ├── citations.py                  # Citation parsing, rendering helpers
│   │   └── templates/
│   │       ├── __init__.py
│   │       ├── schema.py                 # DraftTemplate dataclass + Pydantic (M7)
│   │       ├── registry.py               # Load from YAML, snapshot, fingerprint (M7)
│   │       └── validators.py             # Per-field validators registry (M7)
│   │
│   ├── edits/                            # M9–M10
│   │   ├── __init__.py
│   │   ├── service.py                    # EditService — save, log, enqueue index job (M9)
│   │   ├── diff.py                       # Structured field-level diff (M9)
│   │   ├── few_shot_store.py             # Embed + index + retrieve few-shots (M9)
│   │   └── rule_extractor.py             # Offline pattern mining → appended_rules (M10)
│   │
│   ├── llm/                              # M2
│   │   ├── __init__.py
│   │   ├── router.py                     # LLMRouter — tiers, failover, cache (M2)
│   │   ├── cache.py                      # Postgres-backed response cache (M2)
│   │   ├── budget.py                     # Hourly spend tracker — NN-6 (M2)
│   │   ├── types.py                      # Message, LLMResponse, SamplingParams
│   │   ├── providers/
│   │   │   ├── __init__.py
│   │   │   ├── base.py                   # LLMProvider protocol (M2)
│   │   │   ├── vllm.py                   # VLLMProvider (M2)
│   │   │   ├── anthropic.py              # AnthropicProvider (M2)
│   │   │   ├── openai.py                 # OpenAIProvider (M2)
│   │   │   ├── gemini.py                 # GeminiProvider (M2)
│   │   │   └── mock.py                   # MockProvider for tests (M2)
│   │   ├── embedder.py                   # bge-large via sentence-transformers (M6)
│   │   └── reranker_model.py             # bge-reranker-base (M6)
│   │
│   └── jobs/                             # M1
│       ├── __init__.py
│       ├── queue.py                      # Postgres job queue (M1)
│       ├── worker.py                     # `python -m app.jobs.worker` (M1)
│       ├── kinds.py                      # JobKind enum + handler registry (M1)
│       ├── reconciler.py                 # Stuck-job + partial-doc sweep (M1)
│       └── scheduler.py                  # APScheduler for periodic tasks (M10)
│
├── ui/                                   # Next.js 15 (App Router) — M11
│   ├── package.json
│   ├── next.config.js
│   ├── tsconfig.json
│   ├── app/
│   │   ├── layout.tsx
│   │   ├── page.tsx                      # Dashboard
│   │   ├── documents/
│   │   │   ├── page.tsx                  # List
│   │   │   ├── upload/page.tsx           # Upload
│   │   │   └── [id]/page.tsx             # Document detail / blocks debug
│   │   ├── drafts/
│   │   │   ├── new/page.tsx              # Pick template, pick docs
│   │   │   └── [id]/page.tsx             # Draft view + edit
│   │   └── admin/
│   │       └── page.tsx                  # LLM stats, edit rates, rule-extractor trigger
│   ├── components/
│   │   ├── CitedText.tsx                 # Renders chunk citations with click-to-highlight
│   │   ├── DraftEditor.tsx
│   │   ├── DocumentStatusPill.tsx
│   │   └── ...
│   └── lib/
│       ├── api.ts                        # Typed client
│       └── sse.ts                        # SSE with Last-Event-ID + polling fallback
│
├── tests/                                # pytest
│   ├── conftest.py                       # Fixtures: db, router-with-mock, ingest, etc.
│   ├── unit/
│   │   ├── test_classifier.py
│   │   ├── test_chunker.py
│   │   ├── test_fusion.py
│   │   ├── test_validator.py
│   │   ├── test_diff.py
│   │   ├── test_router_failover.py
│   │   ├── test_cache_key.py             # NN-7
│   │   └── ...
│   ├── integration/
│   │   ├── test_ingest_idempotency.py    # NN-2
│   │   ├── test_ingest_recovery.py       # NN-1
│   │   ├── test_concurrency_cap.py       # NN-3
│   │   ├── test_template_snapshot.py     # NN-5
│   │   ├── test_vlm_budget.py            # NN-6
│   │   ├── test_sse_reconnect.py         # NN-10
│   │   ├── test_few_shot_reindex.py      # NN-11
│   │   └── test_draft_end_to_end.py
│   └── fixtures/
│       ├── docs/                         # Sample synthetic PDFs (M12)
│       │   ├── native_clean.pdf
│       │   ├── scan_clean.pdf
│       │   ├── scan_skewed.pdf
│       │   ├── handwriting_mixed.pdf
│       │   ├── multi_column.pdf
│       │   ├── table_heavy.pdf
│       │   └── corrupt.pdf
│       └── prompts/
│
├── eval/                                 # M12
│   ├── README.md
│   ├── data/
│   │   ├── retrieval_queries.jsonl       # query + expected chunk_ids
│   │   ├── extraction_gold.jsonl
│   │   └── edit_pairs.jsonl              # Pre-built edit pairs to drive the loop
│   ├── run_retrieval.py                  # recall@k, mrr
│   ├── run_citation_validity.py          # % supported per draft
│   ├── run_edit_improvement.py           # before vs. after edit-rate per field
│   ├── run_all.py                        # Calls all three, writes report.md
│   └── reports/                          # gitignored; outputs land here
│
├── scripts/
│   ├── seed.py                           # Loads fixture docs into a running stack
│   ├── synthesize_edits.py               # Simulates operator edits for demo (M12)
│   ├── reset_db.py
│   └── bench_ocr.py
│
└── docs/
    ├── architecture/                     # Copy of /mnt/user-data/outputs/architecture/
    ├── milestones/                       # M0–M13 spec docs (this folder)
    ├── runbook.md                        # How to operate the system (M13)
    ├── ADR/                              # Architecture Decision Records
    │   ├── 001-postgres-for-everything.md
    │   ├── 002-llm-router-tiers.md
    │   ├── 003-template-snapshot.md
    │   └── ...
    └── DEMO.md                           # Demo script for the reviewer (M13)
```

## Notes on the layout

- **`app/api/routes/` is thin.** Routes parse input, call into services in `app/ingest/`, `app/draft/`, etc., and return responses. No business logic in routes. This makes services testable without HTTP.

- **`app/db/models/` is split by aggregate**, not one giant `models.py`. Future contributors find documents in `document.py`, not by searching.

- **`app/llm/` is the only place that talks to LLM SDKs.** Every other module calls `LLMRouter`. Test isolation comes free.

- **`config/templates/*.yaml` is code-equivalent.** Templates are versioned in git. Changes are PR-reviewable. Production templates are not edited at runtime — they're loaded fresh and snapshotted per generate call.

- **Migrations live with code.** Alembic, autogenerate-by-default, with manual review of every generated migration. The very first migration creates schemas, extensions, custom text search config, and the partition root for `llm_requests`.

- **The UI is replaceable.** If Next.js becomes a time-sink on Friday, swap `ui/` for a Streamlit app in `ui_streamlit/` (kept as a parallel skeleton, not built upfront). The API contract is the integration seam.

- **Eval is first-class.** Not a notebook. Not a one-off script. `eval/run_all.py` is a real artifact that gets re-run in CI, and `reports/` shows progress.
