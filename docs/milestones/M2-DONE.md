# M2 — LLM Router: DONE

**Date shipped:** 2026-05-14
**NN rules verified:** NN-6, NN-7, NN-12

## What shipped

### Core (`app/llm/`)
- `types.py` — `Message`, `ContentPart` (text/image), `SamplingParams`, `LLMResponse`, `TaskTier`
- `errors.py` — internal failover signals (`ProviderUnavailable`, `RateLimited`, `SchemaViolation`, `ContextOverflowError`). Caller-facing `LLMUnavailableError`/`BudgetExceededError` live in `app.core.errors`.
- `config.py` — `RouterConfig`, `ProviderConfig`, `TierConfig`, `CacheConfig`, `BudgetConfig`; YAML loader.
- `cache.py` — `ResponseCache` with content-addressed key (NN-7); Postgres-backed via `llm_log.llm_cache`, plus an in-memory fallback for tests when no session factory is supplied.
- `budget.py` — `BudgetTracker` with `asyncio.Lock`, sliding 1-hour window, `prime()` reload from `llm_log.llm_requests`.
- `log_repo.py` — fire-and-forget `LLMLogRepo.record(...)` writes to `llm_log.llm_requests` via `asyncio.create_task`; `drain()` for graceful shutdown / tests.
- `router.py` — `LLMRouter.generate()` performs: cache lookup → tier resolution → per-provider budget gate (skipped when `cost_estimate == 0` or `bypass_budget=True`) → optional schema retry → fail over to next provider on `ProviderUnavailable`/`RateLimited`; every path logs to `llm_log.llm_requests`.
- `embedder.py`, `reranker_model.py` — Protocol + stub. Real bge wiring deferred to M6.

### Providers (`app/llm/providers/`)
- `base.py` — `LLMProvider` Protocol + helpers.
- `mock.py` — `MockProvider` with substring/predicate rule registration, raising support, `call_count`, configurable cost & health.
- `vllm.py` — OpenAI-compatible `chat/completions` over `httpx.AsyncClient`; supports `response_format` JSON schema; maps 429/5xx to `RateLimited`/`ProviderUnavailable`.
- `anthropic.py` — `anthropic.AsyncAnthropic`; system prompt extracted; structured output via `tools=[{name:"return", input_schema}]` + `tool_choice`.
- `openai.py` — `openai.AsyncOpenAI` chat completions with `response_format={"type":"json_schema", "strict": True}`.
- `gemini.py` — `google.genai` async client; `response_mime_type=application/json` + `response_schema`.

### Configuration
- `config/router.yaml` — all four real providers; tiers prioritize local-first (vLLM Qwen 2.5 7B/14B/32B), then Anthropic, then OpenAI; vision tier is hosted-only; cache + budget blocks.
- `app/settings.py` — added `ROUTER_CONFIG_PATH`.
- `pyproject.toml` — added `google-genai>=0.3.0`, `PyYAML>=6.0.1`.

### Wiring
- `app/api/deps.py` — `get_llm_router()` singleton with `asyncio.Lock`; builds providers from config; misconfigured providers are logged and skipped at construction time (so missing API keys don't crash startup — they just make that tier cascade). `budget.prime()` is called once at first use.
- `app/api/routes/admin.py` — `GET /admin/llm-stats` returns last-hour aggregates (by tier and by provider; calls, tokens, cost, cache hits, p50/p95 latency) plus budget state. Tolerant of DB unavailability — returns zeros + `error` field instead of failing.
- `app/main.py` — admin router mounted.

### Tests (`tests/`)
- `unit/test_cache_key.py` — identical → identical key; one-char change → new key; dict-order independence; model id / sampling included.
- `unit/test_budget.py` — sliding-window prune, threshold block, zero-budget edge.
- `unit/test_router_failover.py` — failover on `ProviderUnavailable`; `LLMUnavailableError` when all fail.
- `unit/test_router_cache.py` — second identical call serves from cache without invoking the provider; `cached_hit=True`.
- `unit/test_router_budget.py` — `BudgetExceededError` for over-budget hosted tier; $0-cost local tier proceeds with `hourly_usd=0`; `bypass_budget=True` lets paid call through.
- `unit/test_admin_stats_shape.py` — `/admin/llm-stats` returns the documented shape with `dependency_overrides` for the router.
- `integration/test_llm_log.py` — after a routed call, a row with the correct `trace_id`, `tier`, `provider`, tokens, and `status='ok'` is present in `llm_log.llm_requests`.

`make test` is green (46 passed locally).

## Acceptance checklist

- [x] All four real provider classes import without error (CI test imports them; no live calls)
- [x] Mock-driven tests cover failover, cache hit, budget block, schema retry path (retry loop honoured)
- [x] Cache key is content-addressed; dict reordering doesn't change it; one-char change does
- [x] `/admin/llm-stats` returns a real JSON shape (with zeros at cold start)
- [x] `LLM_HOURLY_BUDGET_USD=0` blocks hosted calls but lets local-tier (cost $0) calls proceed
- [x] Every router call writes a row to `llm_log.llm_requests` (NN-12); cache hits log too
- [x] Cache key formula is `sha256(model_id || messages || schema || sampling)` via `json.dumps(sort_keys=True)` (NN-7)

## Deviations

- The local-tier "cheap" bypass works in two ways: (a) `cost_estimate == 0` skips the gate, and (b) `bypass_budget: true` on the tier forces a pass even for paid providers. The yaml uses (a) implicitly by setting `input_cost_per_1k: 0.0` on vLLM providers — sufficient and slightly safer than a tier-level override that could be flipped without re-reading the cost numbers.
- The router does not yet call `provider.health()` at startup; it relies on first-call failure to trip failover. The `/admin/llm-stats` endpoint is enough surface area for the demo; an explicit health probe matrix is a Q4 polish item.
- Live API smoke test (Anthropic key behind `pytest --live`) is not committed — the M2 spec lists it as a "skipped in CI" gate; we'll add it when the first hosted call site lands in M7.

## Follow-ups

- M6 will replace `StubEmbedder` / `StubReranker` with real bge models.
- M10 will add `/admin/rule-extractor/run`; for now `app/api/routes/admin.py` is owned by M2 and contains only `llm-stats`.
- Provider SDK versions pinned: `anthropic>=0.30.0`, `openai>=1.35.0`, `google-genai>=0.3.0`. Floors only — bump as needed when integrating with M7.
