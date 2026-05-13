# M2 — LLM Router

**Estimated time:** 2 hours
**Dependencies:** M1
**Rubric impact:** Code Quality (the system's most-touched abstraction); foundation for M4, M7, M8, M10

## Goal

A working `LLMRouter` with: pluggable provider interface, four real providers (vLLM, Anthropic, OpenAI, Gemini) + a Mock for tests, capability-tier routing with failover, content-addressed response cache, sliding-window USD budget tracker, and metrics writes to `llm_log.llm_requests`. Embeddings and reranking go through the same provider pattern (defined now, models loaded in M6).

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-6, NN-7, NN-12**
2. `docs/architecture/03-components/llm-router.md` — full spec
3. `docs/architecture/project-skeleton.md`

## Non-Negotiables that apply

- **NN-6** — global hourly spend cap is in the router; `BudgetExceededError` is real
- **NN-7** — cache key is `sha256(model_id + messages + schema + sampling)`, content-addressed
- **NN-12** — every call writes a row to `llm_log.llm_requests`

## Files to create

### Core router

- `app/llm/__init__.py`
- `app/llm/types.py` — Pydantic models:
  - `Message(role: Literal["system","user","assistant"], content: str | list[ContentPart])`
  - `ContentPart` (for vision: `{"type":"text","text":...}` / `{"type":"image","source":...}`)
  - `SamplingParams(max_tokens, temperature, top_p, stop)`
  - `LLMResponse(text, structured, model_used, provider, tokens_in, tokens_out, cost_usd, latency_ms, finish_reason, cached_hit)`
  - `TaskTier = Literal["extraction","generation","validation","vision","analysis"]`
- `app/llm/providers/base.py` — `LLMProvider` Protocol with `name`, `capabilities`, async `generate(messages, *, schema=None, sampling=SamplingParams())`, async `health()`, `cost_estimate(tokens_in, tokens_out) -> float`
- `app/llm/providers/mock.py` — returns canned responses keyed by a `register(prompt_substring, response)` mechanism. Used in every router test.
- `app/llm/providers/vllm.py` — talks to vLLM's OpenAI-compatible API. Uses `httpx.AsyncClient`. Supports `response_format` for JSON-schema guided decoding.
- `app/llm/providers/anthropic.py` — `anthropic` SDK; supports tool-use for structured output (JSON via `tools=[{"name":"return","input_schema":...}]` pattern); supports vision.
- `app/llm/providers/openai.py` — `openai` SDK with `response_format={"type":"json_schema",...}`.
- `app/llm/providers/gemini.py` — `google-genai` SDK with structured output.
- `app/llm/cache.py` — `ResponseCache`:
  - `build_key(model_id, messages, schema, sampling) -> str` (NN-7: `sha256_hex(json.dumps(..., sort_keys=True))`)
  - `get(key) -> LLMResponse | None`
  - `put(key, response, ttl_hours)`
  - Backed by `llm_log.llm_cache`
- `app/llm/budget.py` — `BudgetTracker`:
  - In-memory sliding window over the last hour, with `add(provider, cost_usd, ts)` and `current_spend() -> float`
  - On startup, reload last hour from `llm_log.llm_requests`
  - `check(cost_estimate) -> bool` returns False if adding the estimate would exceed `LLM_HOURLY_BUDGET_USD`
- `app/llm/router.py` — `LLMRouter`:
  - `__init__(config: RouterConfig, providers: dict, cache: ResponseCache, budget: BudgetTracker, log_repo: LLMLogRepo)`
  - `async generate(messages, *, task, schema=None, sampling=None, model_override=None, cache=True, trace_id=None) -> LLMResponse`
  - Routing flow exactly as in `03-components/llm-router.md`
  - Budget check before any escalation tier call (cheap local path bypasses budget check by config)
  - Logs every call to `llm_log.llm_requests` whether cached or live
- `app/llm/log_repo.py` — `LLMLogRepo.record(...)` writes to `llm_log.llm_requests`. Async. Fire-and-forget queue (the call returns; logging happens in background) to avoid adding latency.
- `app/llm/embedder.py` — `Embedder` Protocol + a stub implementation. Real bge-large wiring is in M6 (this milestone defines the interface).
- `app/llm/reranker_model.py` — same pattern: Protocol + stub, real bge-reranker-base in M6.

### Configuration

- `config/router.yaml` — exactly the shape from `03-components/llm-router.md`. Include all four providers configured; tiers prioritize local first.
- `app/llm/config.py` — Pydantic loader for `router.yaml` (`RouterConfig`, `ProviderConfig`, `TierConfig`).

### Wiring

- `app/api/deps.py` — `get_llm_router()` dependency that constructs the router lazily on first request and caches. Reads `config/router.yaml` from path in settings.
- `app/api/routes/admin.py` — `GET /admin/llm-stats` returns: last-hour aggregates from `llm_log.llm_requests` (by tier, by provider; calls, p50/p95 latency, total cost, cache hit rate), plus current budget remaining.

### Tests

- `tests/unit/test_cache_key.py` — NN-7: identical inputs → identical key; reordering a `dict` in messages doesn't change key (because of `sort_keys`); changing one character of system prompt changes key
- `tests/unit/test_budget.py` — sliding window expiration; `check()` blocks at the right threshold
- `tests/unit/test_router_failover.py` — primary `MockProvider` raises `ProviderUnavailable` → router falls through to secondary `MockProvider` → request succeeds; tier exhausted → `LLMUnavailableError`
- `tests/unit/test_router_cache.py` — first call hits provider, second call hits cache (verify by inspecting `MockProvider.call_count`)
- `tests/unit/test_router_budget.py` — set `LLM_HOURLY_BUDGET_USD=0.01`; make a call estimated above budget; assert `BudgetExceededError`; assert tier `validation` (cheap local) still goes through if configured to bypass budget
- `tests/integration/test_llm_log.py` — after a router call, `llm_log.llm_requests` has a row with the right `trace_id`, `tier`, `tokens_*`, `cost_usd`

## Acceptance criteria

- [ ] All four real provider classes import without error (SDK dependency wiring works); they are tested with mocked HTTP only (no live calls in CI)
- [ ] Mock-driven tests cover failover, cache hit, schema violation retry, budget block
- [ ] Cache key is content-addressed; changing `appended_rules` in messages produces a new key
- [ ] `/admin/llm-stats` returns a real JSON shape (with zeros if no calls yet)
- [ ] One smoke test with a real local Anthropic key behind a `pytest --live` flag (skipped in CI) — proves the Anthropic provider class actually works end-to-end
- [ ] `LLM_HOURLY_BUDGET_USD=0` blocks all hosted calls but lets local-tier calls proceed (because they cost $0)

## Out of scope

- Real embedder / reranker model loading — M6
- Streaming responses — v1.1
- Token counting before send (provider-specific tokenizer wiring) — use server-returned counts for now
- The vLLM container itself — added to compose in M0/M1; this milestone is the client

## Definition of done

A test using `MockProvider` exercises every routing path. A `pytest --live` test against Anthropic produces a real response with token counts and cost. `/admin/llm-stats` shows the test runs. `M2-DONE.md` written, including a one-paragraph note on which provider SDK versions are pinned.

## Sub-agent delegation

**Yes, recommended.** After `app/llm/types.py`, `app/llm/providers/base.py`, `app/llm/router.py`, and `app/llm/cache.py` are in place:

- Sub-agent A: `app/llm/providers/vllm.py` + tests
- Sub-agent B: `app/llm/providers/anthropic.py` + tests
- Sub-agent C: `app/llm/providers/openai.py` + tests
- Sub-agent D: `app/llm/providers/gemini.py` + tests

Each provider follows the same shape — they should produce nearly-identical code structure. Merge them as they come in; the router stays untouched.
