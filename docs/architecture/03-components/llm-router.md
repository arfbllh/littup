# LLM Router

**Purpose:** Without this, swapping models means a refactor. With this, swapping models means a config change. The router is also the single chokepoint where caching, retries, cost tracking, and fallback policy are enforced — so every LLM call benefits from them.

**Inputs:** A `LLMRequest` (messages, schema, task tier, max tokens, temperature, optional model override).
**Outputs:** A `LLMResponse` (text or structured output, model used, tokens in/out, cost, latency, finish reason).
**Owns:** Provider connection pool, request log, response cache.
**Depends on:** Provider SDKs (`anthropic`, `openai`, `google-genai`); vLLM HTTP endpoint (OpenAI-compatible API) when present.
**Failure mode:** Primary provider error → automatic failover to next configured provider in the tier. All providers fail → typed `LLMUnavailableError` surfaced to caller. Never silently substitutes a worse model without logging.

---

## Why this exists, justified

Without a router:
- Every call site hardcodes a provider SDK. Swapping providers = grep and replace, plus re-testing every call.
- Cost tracking lives in five places, badly. Caching, the same.
- Local vs hosted becomes an if-statement scattered through the codebase.

With a router:
- Business logic calls `router.generate(messages, task="extraction")`. The router picks the model.
- Adding `Mistral`, `Cohere`, or a fine-tuned local model = new provider class, no caller changes.
- Cost, latency, cache, retries, and fallback are uniform.

Boring abstraction, real payoff. Earns its place.

---

## Capability tiers, not vendor tiers

The router routes by **task tier** — what the call needs — not by which API key looks shiny today.

| Task tier | Used for | Default local model | Default hosted model |
|---|---|---|---|
| `extraction` | Structured field extraction, JSON output, single-shot classification | Qwen 2.5 14B Instruct (4-bit) | `claude-haiku-4-5` or `gpt-4.1-mini` |
| `generation` | Section drafting, summarization, prose with citations | Qwen 2.5 32B Instruct (4-bit) | `claude-sonnet-4-5` or `gpt-4.1` |
| `validation` | Claim ↔ source support check (3-way classification) | Qwen 2.5 7B Instruct | `claude-haiku-4-5` |
| `vision` | Last-resort OCR / handwritten pages | (none locally for v1) | `claude-sonnet-4-5` vision or `gpt-4.1` vision |
| `analysis` | Offline edit-pattern rule extraction | Qwen 2.5 32B Instruct | `claude-sonnet-4-5` |

A `task` tier ties to a list of providers in priority order. Within a tier, providers fail over in order. The user (or eval harness) can override the model for any single call.

Model names above are illustrative defaults checked at run-time against an availability probe; the config is the source of truth.

---

## Provider interface

```python
class LLMProvider(Protocol):
    name: str
    capabilities: set[Literal["text", "json", "tools", "vision", "streaming"]]

    async def generate(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse: ...

    async def health(self) -> bool: ...

    def cost_estimate(self, tokens_in: int, tokens_out: int) -> float: ...
```

Implementations:
- `VLLMProvider` — talks to local vLLM over its OpenAI-compatible HTTP endpoint
- `AnthropicProvider` — Claude family
- `OpenAIProvider` — GPT family
- `GeminiProvider` — Gemini family
- `MockProvider` — used in tests and eval; returns canned responses

Each is a single file in `app/llm/providers/`. Adding one is ~50 lines of code.

---

## Routing logic

```python
class LLMRouter:
    def __init__(self, config: RouterConfig, providers: dict[str, LLMProvider]):
        self.tiers = config.tiers  # tier_name -> [provider_name, ...]
        self.providers = providers
        self.cache = ResponseCache(...)
        self.metrics = MetricsCollector(...)

    async def generate(
        self,
        messages: list[Message],
        *,
        task: TaskTier,
        schema: type[BaseModel] | None = None,
        model_override: str | None = None,
        cache: bool = True,
        **kwargs,
    ) -> LLMResponse:
        if cache:
            hit = self.cache.lookup(messages, schema, task, model_override, kwargs)
            if hit: return hit

        provider_names = (
            [model_override] if model_override
            else self.tiers[task]
        )

        last_error = None
        for provider_name in provider_names:
            provider = self.providers[provider_name]
            try:
                with self.metrics.track(provider=provider_name, task=task):
                    response = await provider.generate(messages, schema=schema, **kwargs)
                self.cache.put(messages, schema, task, model_override, kwargs, response)
                return response
            except (ProviderUnavailable, RateLimited) as e:
                last_error = e
                continue
            except SchemaViolation as e:
                # Retry once within same provider with stricter reminder, then escalate
                ...

        raise LLMUnavailableError(f"All providers failed for tier={task}", cause=last_error)
```

---

## Configuration

A single YAML file (`config/router.yaml`) decides everything:

```yaml
default_locale: local            # or "hosted" or "mixed"

tiers:
  extraction:
    - vllm_qwen25_14b            # local first
    - anthropic_haiku            # fallback
  generation:
    - vllm_qwen25_32b
    - anthropic_sonnet
    - openai_gpt41
  validation:
    - vllm_qwen25_7b
    - anthropic_haiku
  vision:
    - anthropic_sonnet_vision    # no good local default in v1
    - openai_gpt41_vision

providers:
  vllm_qwen25_14b:
    type: vllm
    base_url: http://vllm:8000/v1
    model: Qwen/Qwen2.5-14B-Instruct
    timeout_s: 60
  vllm_qwen25_32b:
    type: vllm
    base_url: http://vllm:8000/v1
    model: Qwen/Qwen2.5-32B-Instruct-AWQ
    timeout_s: 90
  anthropic_haiku:
    type: anthropic
    model: claude-haiku-4-5
    api_key_env: ANTHROPIC_API_KEY
  anthropic_sonnet:
    type: anthropic
    model: claude-sonnet-4-5
    api_key_env: ANTHROPIC_API_KEY
  openai_gpt41:
    type: openai
    model: gpt-4.1
    api_key_env: OPENAI_API_KEY
  gemini_pro:
    type: gemini
    model: gemini-2.5-pro
    api_key_env: GEMINI_API_KEY

cache:
  enabled: true
  ttl_hours: 24
  backend: postgres
```

Hosted-first deployment: reorder the tier lists, restart, done. No code changes.

---

## Caching

Cache key: `sha256((model_id, prompt_version, messages, schema, sampling_params))`. Same input → same output for the same model + prompt. Backed by Postgres (simple table, TTL eviction). The cache speeds demos, reruns, and the eval harness; it does not change correctness because the key is content-addressed.

Caching matters because re-running an eval suite or re-generating a draft after fixing a UI bug shouldn't burn API credits.

---

## Cost & latency tracking

Every call logs:
```
{request_id, tier, provider, model, tokens_in, tokens_out, cost_estimate_usd,
 latency_ms, status, cached_hit, error_code}
```

Aggregations exposed at `/admin/llm-stats` for the demo: cost per draft, p50/p95 latency per tier, error rate per provider. A future Langfuse / Helicone integration would replace this — but for an MVP, a Postgres table and a tiny page is enough.

---

## vLLM specifics

- Start vLLM in the Compose stack with `--enable-prefix-caching` (huge speedup for system-prompt-heavy calls) and `--max-num-seqs` tuned to GPU memory.
- Use the **OpenAI-compatible API** vLLM exposes — that means the provider class is almost a copy of the OpenAI provider class with a different `base_url`.
- vLLM supports **guided decoding** for JSON schemas natively; the provider passes the schema as `response_format`.
- If GPU is absent, the operator runs without the `vllm` service — the router's health probe marks `vllm_*` providers `unavailable` and tiers cascade to hosted. Same code path, zero changes.

---

## Edge cases

| Case | Strategy |
|---|---|
| Provider rate-limits | Exponential backoff (3 tries), then failover within tier |
| Provider returns 500 | Failover immediately, log |
| Schema violation (model returns invalid JSON) | One retry on same provider with stricter instruction; then escalate to next tier |
| Timeout | Failover; original request marked `timeout` for offline analysis |
| Cache poisoning concern | Cache key includes model + prompt versions; bumping either invalidates |
| Hosted provider deprecated a model name | Health probe catches at startup; loud config error rather than silent failover |
| User wants a head-to-head A/B (Qwen vs Claude on the same input) | Eval harness calls `router.generate(..., model_override="...")` for both, no business-logic change |
| Token budget exceeded | Pre-flight estimate; if over context window, return `ContextOverflowError` upstream (Draft Engine decides whether to truncate or split) |
| Hosted API key missing | Provider marked `unavailable` at startup; tier cascades or fails loudly if no fallback configured |

## Interface

```python
class LLMRouter:
    async def generate(
        self,
        messages: list[Message],
        *,
        task: Literal["extraction", "generation", "validation", "vision", "analysis"],
        schema: type[BaseModel] | None = None,
        model_override: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        cache: bool = True,
    ) -> LLMResponse: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...    # via embedder provider

    async def rerank(self, query: str, docs: list[str]) -> list[int]: ... # via reranker provider

    def stats(self, window: str = "1h") -> RouterStats: ...
```

Embeddings and reranking go through the same router pattern with their own provider interfaces. The router is the only thing in the system that talks to any AI model.

## Open questions

- Routing on **observed model quality** (route low-confidence retries to a stronger tier automatically) — interesting; v1.1.
- A **shadow mode** where every local call also runs against the hosted equivalent and diffs the outputs, for evaluation — useful, but not in build window. Designed-for.
- Streaming responses through the router — uniform streaming interface is doable; deferred to v1.1.
