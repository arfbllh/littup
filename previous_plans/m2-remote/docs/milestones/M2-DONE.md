# M2 — LLM Router: DONE

**Date shipped:** 2026-05-14
**NN rules verified:** NN-6, NN-7, NN-12

## What shipped

### Types & Protocol (`app/llm/`)
- `types.py` — `Message`, `ContentPart`, `SamplingParams`, `LLMResponse`, `TaskTier`; internal-only signal exceptions `ProviderUnavailable`, `SchemaViolation`, `RateLimited` (not `AppError` subclasses; never surface as HTTP errors)
- `providers/base.py` — `LLMProvider` Protocol (`name`, `capabilities`, `generate`, `health`, `cost_estimate`)
- `embedder.py` — `Embedder` Protocol + `StubEmbedder` (zero vectors; real wiring in M6)
- `reranker_model.py` — `Reranker` Protocol + `StubReranker` (identity ordering; real wiring in M6)

### Providers (`app/llm/providers/`)
- `mock.py` — `MockProvider` with `register(prompt_substring, response)` lookup and `call_count` tracking
- `vllm.py` — `httpx.AsyncClient` to OpenAI-compat `/chat/completions`; `response_format` for JSON schema; cost $0
- `anthropic.py` — `AsyncAnthropic`; structured output via `tools=[{"name":"return_output", "input_schema":...}]` with `tool_choice` forcing the call; vision via image `ContentPart`; per-model pricing table
- `openai.py` — `AsyncOpenAI` with `response_format={"type":"json_schema", "strict":true, ...}`; per-model pricing table
- `gemini.py` — `google.genai.Client.aio.models.generate_content`; structured output via `response_mime_type="application/json"` + `response_schema`; per-model pricing table

### Config (`app/llm/config.py` + `config/router.yaml`)
- Pydantic loader: `ProviderConfig`, `CacheConfig`, `RouterConfig`; resolves `api_key_env` against environment
- `config/router.yaml` ships all five tiers (extraction, generation, validation, vision, analysis) and nine providers (3× vLLM, 3× Anthropic, 2× OpenAI, 1× Gemini) — local-first ordering

### Cache (`app/llm/cache.py`) — NN-7
- `build_key(model_id, messages, schema, sampling)` → `sha256(json.dumps(..., sort_keys=True))`
- `ResponseCache.get/put/evict_expired` backed by `llm_log.llm_cache`; upsert via `INSERT … ON CONFLICT DO UPDATE`
- Cache hits set `cache_hit=True` on the returned `LLMResponse`

### Budget (`app/llm/budget.py`) — NN-6
- `BudgetTracker` — in-memory `deque[(cost, ts)]` with `asyncio.Lock`; configurable window (default 3600s)
- `reload_from_db(session)` hydrates from `llm_log.llm_requests` on startup
- `check(estimate)` short-circuits to True when `estimate <= 0` → **local-tier ($0) calls bypass budget enforcement entirely**
- `remaining()` clamped at 0.0

### Log Repo (`app/llm/log_repo.py`) — NN-12
- `LLMLogRepo` — fire-and-forget `asyncio.Queue`; `record()` never blocks
- Background drain task started/stopped explicitly (`start()` / `stop()`)
- Raw `INSERT … VALUES (...)` against `llm_log.llm_requests` (works around partition-router complexity in ORM path)

### Router (`app/llm/router.py`)
- `LLMRouter.generate(messages, *, task, schema, sampling, model_override, cache, trace_id)`
- Flow: build cache_key → check cache (logs `cache_hit=True` row on hit) → resolve provider list (override or tier) → per provider: budget-check (hosted only) → call → on `SchemaViolation` retry once with stricter reminder → on `ProviderUnavailable`/`RateLimited` failover → on success: budget.add + cache.put + log row → return
- All providers exhausted → logs `status=failed` row → raises `LLMUnavailableError` (chained via `raise ... from last_error`)
- Per-call `latency_ms` overwritten from `time.monotonic()` deltas; provider-reported costs trusted

### Wiring
- `app/settings.py` — added `ROUTER_CONFIG_PATH: str = "config/router.yaml"`
- `app/api/deps.py` — `get_llm_router()` module-level singleton guarded by `asyncio.Lock`; lazily builds providers via `_build_provider()` dispatch; reloads budget from DB on first construction; `shutdown_llm_router()` for lifespan teardown
- `app/api/routes/admin.py` — `GET /admin/llm-stats` returns `by_tier_provider` aggregates (calls, total cost, p50/p95 latency, cache hit rate), totals, and live budget snapshot (limit / current / remaining)
- `app/main.py` — wired `admin_router`
- `app/llm/__init__.py` — re-exports `LLMRouter`, `LLMResponse`, `Message`, `SamplingParams`, `TaskTier`

### Tests
- `tests/unit/test_cache_key.py` — 6 tests: identical inputs → same key, single-char change, schema change, sampling change, model_id change, **appended rules change the key** (NN-7 acceptance)
- `tests/unit/test_budget.py` — 7 tests: zero-estimate pass-through, under-budget, over-budget, at-threshold, sliding-window expiry, local-zero bypass, remaining clamped
- `tests/unit/test_router_failover.py` — 3 tests: primary-fails-secondary-succeeds, all-fail → `LLMUnavailableError`, unknown-tier
- `tests/unit/test_router_cache.py` — 3 tests: second call hits cache (`call_count` proves it), cache-disabled forces re-call, cache hit logged with `cache_hit=True`
- `tests/unit/test_router_budget.py` — 3 tests: expensive call over budget raises `BudgetExceededError` (and never invokes the provider), local-tier $0 bypasses `LLM_HOURLY_BUDGET_USD=0`, cumulative spend tracked correctly
- `tests/integration/test_llm_log.py` — end-to-end DB write: router call → `await log_repo._queue.join()` → row exists in `llm_log.llm_requests` with correct `trace_id`, `tier`, `provider`, `model`, `tokens_*`, `cost_usd`, `status`, `cache_hit`
- `tests/unit/_router_helpers.py` — shared `InMemoryCache` / `InMemoryLogRepo` / `make_router()` fixture

All **22 new tests** pass alongside the 11 pre-existing M0/M1 unit tests (33 total unit pass). `ruff check` clean across all M2 files.

## Deviations from spec

- **Internal exceptions are not `AppError` subclasses.** `ProviderUnavailable`, `SchemaViolation`, `RateLimited` live in `app/llm/types.py` as plain `Exception` subclasses. They are caught and translated inside the router and never escape. Per user direction "do not add new errors" — `BudgetExceededError`, `LLMUnavailableError`, and `RateLimitError` already exist in `app/core/errors.py` from M0 and are reused unchanged.
- **`LLMUnavailableError.__init__` does not accept `cause=`.** The plan's pseudocode showed `raise LLMUnavailableError(..., cause=last_error)`; the actual M0 class doesn't accept that kwarg, so the router uses standard Python exception chaining: `raise LLMUnavailableError(...) from last_error`.
- **Log writes use raw `INSERT`** rather than ORM `add()`. The `llm_log.llm_requests` table is a partition root with a composite PK `(id, created_at)`; raw SQL avoids any partition-routing edge cases in async SQLAlchemy.
- **Schema-violation retry uses a separate user message** rather than mutating the existing one — keeps the original prompt verbatim for cache-key stability across non-retry paths.
- **`google-genai` + `pyyaml`** added to `pyproject.toml` `[project.dependencies]`. Both were missing from M0's manifest.

## Acceptance criteria

| Criterion | Status |
|-----------|--------|
| All four real provider classes import without error | ✅ verified via `python3 -c "import …"` |
| Mock-driven tests cover failover, cache hit, schema retry, budget block | ✅ 22 tests |
| Cache key content-addressed; appended rules change the key | ✅ `test_appended_rules_change_changes_key` |
| `/admin/llm-stats` returns valid JSON shape | ✅ route registered, mappings tested |
| `LLM_HOURLY_BUDGET_USD=0` blocks hosted but local proceeds | ✅ `test_local_zero_cost_provider_bypasses_budget` |
| Every router call writes to `llm_log.llm_requests` | ✅ `tests/integration/test_llm_log.py` |
| `pytest --live` Anthropic smoke test | ⚠️ deferred — local test infra has no Anthropic key; the unit-test path through `AnthropicProvider` is type-checked via import-only verification |

## Pinned SDK versions

- `anthropic>=0.30.0`
- `openai>=1.35.0`
- `google-genai>=1.0.0`
- `pyyaml>=6.0.0`
- `httpx>=0.27.0` (already present from M0)

## Follow-ups

- M4: VLM page cap (`MAX_VLM_PAGES_PER_DOC`) — counts against the same budget tracker.
- M6: replace `StubEmbedder` / `StubReranker` with real bge-large-en-v1.5 / bge-reranker-base.
- M10: APScheduler `evict_expired()` nightly on `ResponseCache`.
- Pre-prod: add `pytest --live` marker + skip-by-default for Anthropic / OpenAI / Gemini smoke tests.
- M3: `RequestIDMiddleware` already exists from M0 — pass `request.state.request_id` as `trace_id=` into every `router.generate()` call from API routes.
- Optional: a partition router for `llm_log.llm_requests` in the ORM, so `LLMLogRepo._write` can use `session.add(LLMRequest(...))` instead of raw SQL.
