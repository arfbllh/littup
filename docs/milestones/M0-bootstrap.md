# M0 — Project Bootstrap

**Estimated time:** 1 hour
**Dependencies:** none
**Rubric impact:** Code Quality foundation; no user-visible features yet

## Goal

A repo that boots: `docker compose up` brings up an empty FastAPI service with `/healthz` and `/readyz`, structured JSON logs to stdout, a request-ID middleware, and a typed error hierarchy. Postgres container is present but no schema yet. The skeleton compiles and tests run (even if there are only smoke tests).

## Context Claude Code must read

1. `docs/architecture/00-summary.md`
2. `docs/architecture/10-fixes-and-non-negotiables.md` — focus on **NN-12**
3. `docs/architecture/project-skeleton.md`
4. `docs/architecture/02-architecture.md` (deployment shape section)

## Non-Negotiables that apply

- **NN-12** — observability built in from the start

## Files to create

- `pyproject.toml` — Python 3.11+, dependencies: `fastapi`, `uvicorn[standard]`, `pydantic-settings`, `structlog`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `httpx`, `pytest`, `pytest-asyncio`, `pytest-cov`. Use `uv` for env management.
- `uv.lock`
- `.env.example` — every env var documented with a one-line comment
- `.gitignore` — Python, Node, IDE, `.env`, `eval/reports/`, `__pycache__`
- `Makefile` — `up`, `down`, `logs`, `test`, `lint`, `fmt`, `migrate`, `seed`, `eval`
- `docker-compose.yml` — services: `api`, `postgres` (just postgres for now, pgbouncer added in M1), no worker or vllm yet
- `Dockerfile` — multi-stage Python image; non-root user; copies `app/`, `config/`; entrypoint `uvicorn app.main:app`
- `app/__init__.py`
- `app/main.py` — FastAPI app, includes health router, mounts middleware
- `app/settings.py` — Pydantic `BaseSettings` reading from env; fields: `DATABASE_URL`, `LOG_LEVEL`, `ENV`, plus stubs for LLM keys
- `app/core/__init__.py`
- `app/core/logging.py` — `structlog` setup; JSON renderer in prod, console renderer in dev; bound context includes `request_id`
- `app/core/errors.py` — `AppError` base class with `code: str`, `message: str`, `status_code: int = 400`; subclasses `NotFoundError`, `ValidationError`, `ConflictError`, `BudgetExceededError`, `LLMUnavailableError`, `IngestError`
- `app/core/middleware.py` — `request_id_middleware` (reads `X-Request-ID`, generates uuid7 if absent, binds to structlog context, sets response header); `error_handler` (translates `AppError` to typed JSON, logs unexpected exceptions with `request_id`)
- `app/core/ids.py` — `new_uuid7()`, `sha256_hex(bytes)`, `short_id()`
- `app/api/__init__.py`
- `app/api/routes/__init__.py`
- `app/api/routes/health.py` — `GET /healthz` returns `{"status":"ok"}`; `GET /readyz` checks DB ping (returns 503 if down)
- `tests/__init__.py`
- `tests/conftest.py` — minimal: an `httpx.AsyncClient` fixture against the FastAPI app
- `tests/unit/test_health.py` — asserts `/healthz` returns 200 and the response has `X-Request-ID`
- `tests/unit/test_errors.py` — asserts `AppError` subclass raised in a test route is rendered as the documented JSON envelope with `code`, `message`, `request_id`

## Acceptance criteria

- [ ] `docker compose up` brings up `api` and `postgres`; `curl localhost:8000/healthz` returns 200
- [ ] `curl -H 'X-Request-ID: test-123' localhost:8000/healthz` echoes `test-123` in the response header
- [ ] Every log line is valid JSON, includes `request_id`, `event`, `ts`, `level`
- [ ] `make test` runs and passes
- [ ] `make lint` runs and passes (use `ruff`)
- [ ] `.env.example` has every variable the code references and nothing extra
- [ ] Raising `NotFoundError("doc not found", code="DOCUMENT_NOT_FOUND")` from a test route produces a 404 with `{"error":{"code":"DOCUMENT_NOT_FOUND","message":"doc not found","request_id":"..."}}`

## Out of scope (Claude Code: do NOT)

- DB models or migrations (that's M1)
- LLM router or providers (M2)
- Any ingest, retrieval, or draft logic
- Authentication (none in v1)
- Streamlit/Next.js UI (M11)
- Worker process (M1)

## Test plan

- `tests/unit/test_health.py` — health endpoints
- `tests/unit/test_errors.py` — error class hierarchy and HTTP rendering
- `tests/unit/test_logging.py` — capture logs in pytest and assert JSON shape

## Definition of done

You can cold-clone the repo, run `make up`, hit `/healthz`, and see structured JSON logs streaming with request IDs. Tests pass. Lint clean. `M0-DONE.md` written.

## Sub-agent delegation

None at this size. Single sequential build.
