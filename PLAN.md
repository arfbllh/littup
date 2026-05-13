# PLAN — M2: LLM Router

**Status:** Draft — awaiting approval before any code is written.
**Dependencies satisfied:** M0 (structlog, AppError hierarchy, middleware) · M1 (DB models, session)
**NN rules in scope:** NN-6, NN-7, NN-12

---

## Goal

Ship a working `LLMRouter` with pluggable providers (vLLM, Anthropic, OpenAI, Gemini, Mock), tier-based routing with failover, content-addressed response cache, sliding-window budget tracker, and async LLM log writes. The router is the only place that touches LLM SDKs.

---

## Files to create / modify

### New dependencies (pyproject.toml)
- `google-genai>=1.0.0` — Gemini SDK
- `pyyaml>=6.0.0` — router.yaml parsing

### Phase 1 — Foundation (main thread, all others depend on these)

| File | What goes in it |
|------|----------------|
| `app/llm/types.py` | `Message`, `ContentPart`, `SamplingParams`, `LLMResponse`, `TaskTier`; `ProviderUnavailableError`, `SchemaViolationError`, `RateLimitedError` sub-typed from `AppError` |
| `app/llm/providers/base.py` | `LLMProvider` Protocol — `name`, `capabilities`, `generate()`, `health()`, `cost_estimate()` |
| `app/llm/providers/mock.py` | `MockProvider` — `register(substring, response)` lookup; tracks `call_count` per registration; raises `ProviderUnavailableError` when configured to do so |
| `app/llm/config.py` | Pydantic models `ProviderConfig`, `TierConfig`, `CacheConfig`, `RouterConfig`; `load_router_config(path) -> RouterConfig` (yaml + env var substitution for api_key_env) |
| `config/router.yaml` | Full config: all 5 tiers, 7 providers (3× vLLM, 2× Anthropic, 1× OpenAI, 1× Gemini), cache block |
| `app/llm/cache.py` | `ResponseCache` — `build_key()` (NN-7: sha256 of json.dumps sorted), `get()`, `put()`, `evict_expired()` — backed by `llm_log.llm_cache` ORM |
| `app/llm/budget.py` | `BudgetTracker` — in-memory deque sliding window (1h), `add(provider, cost, ts)`, `current_spend()`, `check(estimate) -> bool`; `reload_from_db(session)` for startup hydration (NN-6) |
| `app/llm/log_repo.py` | `LLMLogRepo` — async background queue; `record(...)` enqueues, background task drains; non-blocking to callers (NN-12) |
| `app/llm/providers/mock.py` | (same row above — placeholder for ordering) |
| `app/llm/router.py` | `LLMRouter.__init__(config, providers, cache, budget, log_repo)`; `async generate(messages, *, task, schema, sampling, model_override, cache, trace_id) -> LLMResponse`; full routing flow: cache-check → tier loop → budget-check → provider.generate → schema-retry once → failover → log → raise `LLMUnavailableError` |
| `app/llm/embedder.py` | `Embedder` Protocol + `StubEmbedder` (returns zeroed vectors) — real wiring in M6 |
| `app/llm/reranker_model.py` | `Reranker` Protocol + `StubReranker` (returns original order) — real wiring in M6 |

### Phase 2 — Providers (parallel sub-agents after Phase 1 is merged)

| File | Notes |
|------|-------|
| `app/llm/providers/vllm.py` | `VLLMProvider` — `httpx.AsyncClient` to OpenAI-compat API; `response_format` for JSON schema; cost $0 (local); health = GET /health |
| `app/llm/providers/anthropic.py` | `AnthropicProvider` — `anthropic` SDK; tool-use pattern for structured output; supports vision `ContentPart`; cost from response usage |
| `app/llm/providers/openai.py` | `OpenAIProvider` — `openai` SDK; `response_format={"type":"json_schema",...}` for structured; cost from response usage |
| `app/llm/providers/gemini.py` | `GeminiProvider` — `google-genai` SDK; structured output via `response_mime_type="application/json"` + `response_schema`; cost from response metadata |

### Phase 3 — Wiring

| File | What changes |
|------|-------------|
| `app/settings.py` | Add `ROUTER_CONFIG_PATH: str = "config/router.yaml"` |
| `app/api/deps.py` | `get_llm_router()` — constructs `LLMRouter` once at startup (module-level singleton with `asyncio.Lock` guard); reads config from `settings.ROUTER_CONFIG_PATH`; reloads budget from DB on first call |
| `app/api/routes/admin.py` | `GET /admin/llm-stats` — last-hour aggregates from `llm_log.llm_requests` (by tier, by provider: calls, p50/p95 latency, total cost, cache hit rate) + budget remaining |
| `app/main.py` | Wire `admin_router` |
| `app/llm/__init__.py` | Re-export `LLMRouter`, `TaskTier`, `LLMResponse` |

### Phase 4 — Tests

| File | Covers |
|------|--------|
| `tests/unit/test_cache_key.py` | NN-7: identical inputs → same key; dict reorder → same key; single char change → new key |
| `tests/unit/test_budget.py` | Sliding window expires correctly; `check()` blocks at threshold; local ($0) always passes |
| `tests/unit/test_router_failover.py` | Primary `MockProvider` raises `ProviderUnavailableError` → falls to secondary → succeeds; all fail → `LLMUnavailableError` |
| `tests/unit/test_router_cache.py` | First call hits provider (call_count=1); second identical call hits cache (call_count still 1) |
| `tests/unit/test_router_budget.py` | `LLM_HOURLY_BUDGET_USD=0.01`; expensive call → `BudgetExceededError`; `validation` tier with $0 local provider bypasses budget |
| `tests/integration/test_llm_log.py` | After router call, `llm_log.llm_requests` has correct `trace_id`, `tier`, `tokens_*`, `cost_usd` |

---

## Routing logic (exact flow)

```
generate(messages, task, schema, sampling, model_override, cache, trace_id):
  1. build cache_key (NN-7)
  2. if cache enabled: check ResponseCache → return hit (still logs as cache_hit=True)
  3. provider_names = [model_override] if override else config.tiers[task]
  4. for provider_name in provider_names:
       provider = providers[provider_name]
       if provider.cost_estimate(est_in, est_out) > 0:   # hosted tier
           if not budget.check(estimate): raise BudgetExceededError
       t0 = now()
       try:
           response = await provider.generate(messages, schema=schema, sampling=sampling)
       except SchemaViolationError:
           # retry once on same provider with stricter reminder
           response = await provider.generate(messages_with_retry, schema=schema, ...)
       except (ProviderUnavailableError, RateLimitedError):
           log warning; continue
       budget.add(provider.name, response.cost_usd, now())
       cache.put(cache_key, response, ttl_hours=config.cache.ttl_hours)
       log_repo.record(trace_id, tier, provider, model, tokens, cost, latency, status="ok")
       return response
  5. raise LLMUnavailableError("all providers exhausted for tier={task}")
     log_repo.record(..., status="failed")
```

---

## Key design decisions

1. **Budget bypasses local ($0) providers.** `VLLMProvider.cost_estimate()` returns 0. Budget check is `if estimate > 0`, so local tiers always proceed even at `LLM_HOURLY_BUDGET_USD=0`.

2. **Cache backed by Postgres `llm_log.llm_cache`.** `LLMCache` ORM model already exists from M1. `ResponseCache.get()` deserialises the `response` JSONB column back to `LLMResponse`.

3. **`LLMLogRepo` is fire-and-forget.** It maintains an `asyncio.Queue`; `record()` puts without waiting; a background task drains. Avoids blocking the router on DB writes.

4. **Schema retry is in the router, not providers.** One retry on the same provider with an appended "Return valid JSON matching the schema" instruction. If it fails again, escalate to next provider in tier.

5. **Sub-agent delegation for providers.** After Phase 1 is committed, four sub-agents run in parallel — one per provider file. The router itself never changes.

6. **`get_llm_router()` is a module-level singleton.** Not a new instance per request. Budget state must survive across requests; recreating it each time would reset the sliding window.

---

## Acceptance criteria (from spec)

- [ ] All four real provider classes import without error with mocked HTTP
- [ ] Mock-driven tests cover: failover, cache hit, schema violation retry, budget block
- [ ] Cache key is content-addressed (changing a single character changes the key)
- [ ] `/admin/llm-stats` returns valid JSON (zeros if no calls yet)
- [ ] `LLM_HOURLY_BUDGET_USD=0` blocks hosted calls but local-tier ($0) calls proceed
- [ ] `pytest --live` smoke test exists for Anthropic (skipped unless flag set)
- [ ] Every router call writes a row to `llm_log.llm_requests`

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Gemini SDK (`google-genai`) API shape differs from OpenAI compat | Isolate behind provider interface; only the provider file changes if SDK changes |
| Partitioned `llm_requests` table complicates ORM inserts | Use raw `INSERT` SQL in `LLMLogRepo.record()` targeting the table name; SQLAlchemy partition routing handles it at the DB level |
| `asyncio.Queue` in log_repo leaks if no task drains it | Background task started in `get_llm_router()` at singleton creation; cancelled on app shutdown via lifespan |
| `google-genai` not in pyproject.toml | Add to `[project.dependencies]` before Phase 2 |

---

## Sequence

```
Phase 1 (main thread):   types → base → mock → config → router.yaml → cache → budget → log_repo → router → embedder → reranker
Phase 2 (4 sub-agents):  vllm.py | anthropic.py | openai.py | gemini.py
Phase 3 (main thread):   deps.py → admin.py → main.py → llm/__init__.py → pyproject.toml
Phase 4 (main thread):   all 6 test files
Done:                     M2-DONE.md
```
