# M0 — Bootstrap: DONE

**Date shipped:** 2026-05-14
**NN rules verified:** NN-12

## What shipped

- `pyproject.toml` — Python 3.11+, all deps, `uv` toolchain, `ruff` config
- `.env.example` — all env vars documented
- `.gitignore` — Python/Node/IDE/eval reports
- `Makefile` — `up`, `down`, `test`, `test-integration`, `lint`, `fmt`, `migrate`, `seed`, `eval`
- `Dockerfile` — multi-stage, non-root `appuser`
- `docker-compose.yml` — `api`, `worker`, `postgres` (pgvector/pgvector:pg16), `pgbouncer` (bitnami)
- `app/settings.py` — `BaseSettings` with all env vars
- `app/core/ids.py` — `new_uuid7()`, `sha256_hex()`, `short_id()` (uses `uuid_extensions` module)
- `app/core/logging.py` — structlog JSON (prod) / console (dev), `request_id` context var
- `app/core/errors.py` — `AppError` + `NotFoundError`, `ValidationError`, `ConflictError`, `BudgetExceededError`, `LLMUnavailableError`, `IngestError`, `RateLimitError`
- `app/core/middleware.py` — `RequestIDMiddleware` (reads/generates X-Request-ID, binds to structlog), `app_error_handler`, `unhandled_error_handler`
- `app/main.py` — FastAPI app factory, mounts middleware + health router
- `app/api/routes/health.py` — `GET /healthz` → 200; `GET /readyz` → 503 if DB down

## Tests

- `tests/unit/test_health.py` — 3 tests (200, echo, auto-generate)
- `tests/unit/test_errors.py` — 4 tests (JSON envelope, status codes, request_id in header)
- `tests/unit/test_logging.py` — 2 tests (JSON shape, request_id bound)

All 9 M0 unit tests pass. `ruff check` clean.

## Deviations

- `uuid7` package installs as `uuid_extensions` module (not `uuid7`). Import fixed in `ids.py`.
- docker-compose.yml already includes `worker` and `pgbouncer` (written for the M1-final state to avoid a second rewrite).

## Follow-ups

- `GET /readyz` DB check will silently fail until M1 migration is applied (returns 503 gracefully — correct behaviour).
