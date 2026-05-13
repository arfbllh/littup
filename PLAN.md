# Implementation Plan — M2 (LLM Router)

## Pre-flight state

M0 and M1 are complete (see `docs/milestones/M0-DONE.md`, `M1-DONE.md`).
`app/llm/` contains only empty `__init__.py` files. `config/router.yaml`
does not exist. There are no `app/api/deps.py` or admin route. The
`llm_log.llm_requests` (partitioned) + `llm_log.llm_cache` tables already
exist from M1.

## Goal

Ship the `LLMRouter` per `docs/architecture/03-components/llm-router.md` and
the M2 milestone spec: pluggable providers (vLLM, Anthropic, OpenAI, Gemini,
Mock), tier routing with failover, content-addressed cache, sliding-window
budget tracker (NN-6), fire-and-forget logging to `llm_log.llm_requests`
(NN-12), and `GET /admin/llm-stats`.

## Non-negotiables that apply

- **NN-6** — Per-document VLM page cap is config only at M2; the **hourly
  USD spend cap** is enforced in `BudgetTracker.check()` with a real
  `BudgetExceededError`.
- **NN-7** — Cache key is `sha256(model_id || messages || schema || sampling)`
  computed via `json.dumps(..., sort_keys=True)`.
- **NN-12** — Every call writes a row to `llm_log.llm_requests`, including
  cache hits, with `trace_id`.

## Files to create

### Core

| File | Purpose |
|------|---------|
| `app/llm/types.py` | `Message`, `ContentPart`, `SamplingParams`, `LLMResponse`, `TaskTier`, `ProviderCapability` |
| `app/llm/errors.py` | `ProviderUnavailable`, `RateLimited`, `SchemaViolation`, `ContextOverflowError` (internal; surface `LLMUnavailableError`/`BudgetExceededError` already in `app/core/errors.py`) |
| `app/llm/config.py` | Pydantic `RouterConfig`, `ProviderConfig`, `TierConfig`, `CacheConfig`; `load_router_config(path)` |
| `app/llm/cache.py` | `ResponseCache`: `build_key()`, `get()`, `put()` against `llm_log.llm_cache` |
| `app/llm/budget.py` | `BudgetTracker`: in-memory sliding window, `add()`, `current_spend()`, `check(estimate)`; reload from DB on `prime()` |
| `app/llm/log_repo.py` | `LLMLogRepo.record(...)` — fire-and-forget background task, inserts into `llm_log.llm_requests` |
| `app/llm/router.py` | `LLMRouter.generate(...)` — cache lookup → tier failover → budget gate → log |
| `app/llm/embedder.py` | `Embedder` Protocol + `StubEmbedder` (M6 fills in) |
| `app/llm/reranker_model.py` | `Reranker` Protocol + `StubReranker` |

### Providers

| File | Purpose |
|------|---------|
| `app/llm/providers/base.py` | `LLMProvider` Protocol; common helpers (`_now_ms()`, token-cost calc skeleton) |
| `app/llm/providers/mock.py` | `MockProvider` with `register(substring_or_predicate, response_or_exception)`; supports `call_count`, ordered queue |
| `app/llm/providers/vllm.py` | `VLLMProvider` over OpenAI-compatible HTTP using `httpx.AsyncClient` |
| `app/llm/providers/anthropic.py` | `AnthropicProvider` using `anthropic` SDK; tool-use for JSON schema |
| `app/llm/providers/openai.py` | `OpenAIProvider` using `openai` SDK; `response_format={"type":"json_schema"}` |
| `app/llm/providers/gemini.py` | `GeminiProvider` using `google-genai` SDK; structured output |

### Config

| File | Purpose |
|------|---------|
| `config/router.yaml` | Tiers prioritize local first; all four providers listed; cache + budget block |

### Wiring

| File | Purpose |
|------|---------|
| `app/api/deps.py` | `get_llm_router()` constructs lazily, caches in module-level singleton |
| `app/api/routes/admin.py` | `GET /admin/llm-stats` with last-hour aggregates + budget remaining |
| `app/main.py` | Mount admin router |
| `app/settings.py` | Add `ROUTER_CONFIG_PATH: str = "config/router.yaml"` |
| `pyproject.toml` | Add `google-genai`, `pyyaml` |

### Tests

| File | What it verifies |
|------|------------------|
| `tests/unit/test_cache_key.py` | NN-7: identical inputs → identical key; key changes when system prompt changes one char; dict-order independence (sort_keys) |
| `tests/unit/test_budget.py` | sliding-window expiration; `check()` blocks at threshold |
| `tests/unit/test_router_failover.py` | Primary `ProviderUnavailable` → secondary succeeds; all-fail → `LLMUnavailableError` |
| `tests/unit/test_router_cache.py` | Second identical call doesn't increment provider `call_count`; `cached_hit=True` |
| `tests/unit/test_router_budget.py` | `LLM_HOURLY_BUDGET_USD=0.01` blocks expensive tier with `BudgetExceededError`; tier flagged `bypass_budget` proceeds |
| `tests/unit/test_admin_stats_shape.py` | `/admin/llm-stats` returns expected JSON shape with zeros when empty |
| `tests/integration/test_llm_log.py` | After a router call (mocked provider), `llm_log.llm_requests` has a row with `trace_id`, `tier`, tokens, cost |

## Acceptance gates

- All four provider files import without error (SDKs declared in `pyproject.toml`).
- Mock-driven tests cover failover, cache hit, budget block, log write.
- Cache key is content-addressed and stable across dict ordering.
- `GET /admin/llm-stats` returns a real JSON shape with zeros at cold start.
- `LLM_HOURLY_BUDGET_USD=0` blocks hosted calls; local-tier (cost $0) proceeds.

## Out of scope

- Real bge-large / bge-reranker model loading (M6).
- Streaming.
- Provider-side tokenizer for pre-flight token counting (use server counts).
- Pinning vLLM container (already in compose from M0/M1).
- Live API smoke test (documented behind `pytest --live` skip — no key in CI).

## Key design decisions

| Decision | Rationale |
|----------|-----------|
| Logging is fire-and-forget via `asyncio.create_task` | NN-12 wants every call logged but not at the cost of latency |
| Budget tracker is in-memory + DB-primed | Survives restart without per-call DB read; primed once at startup |
| `cost_estimate` lives on the provider, not the tier | Different providers price differently for the same model class |
| Cache reads/writes use a `bypass_cache` flag, not skipping the call | Eval harness can force fresh runs without disabling the cache globally |
| `bypass_budget: true` set on the local-default tier providers | NN-6 hourly cap only applies to spend; local cost is $0 |

## Sequence

1. `pyproject.toml` — add `google-genai`, `pyyaml`
2. `app/llm/types.py`, `errors.py`, `config.py`
3. `app/llm/cache.py`, `budget.py`, `log_repo.py`
4. `app/llm/providers/base.py`, `mock.py`
5. `app/llm/router.py`
6. `app/llm/providers/{vllm,anthropic,openai,gemini}.py` (structurally near-identical)
7. `app/llm/embedder.py`, `reranker_model.py` (stubs)
8. `config/router.yaml`
9. `app/api/deps.py` + `app/api/routes/admin.py` + mount in `app/main.py`
10. Tests
11. `docs/milestones/M2-DONE.md`
