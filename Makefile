.PHONY: up down logs test lint fmt migrate reset-db seed eval

# ── Docker ───────────────────────────────────────────────────────────────────
up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f

# ── Development ──────────────────────────────────────────────────────────────
dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker:
	python -m app.jobs.worker

# ── Tests ────────────────────────────────────────────────────────────────────
test:
	pytest tests/unit/ -v --tb=short

test-integration:
	pytest tests/integration/ -v --tb=short

test-all:
	pytest tests/ -v --tb=short --cov=app --cov-report=term-missing

# ── Code quality ─────────────────────────────────────────────────────────────
lint:
	ruff check app/ tests/

fmt:
	ruff format app/ tests/
	ruff check --fix app/ tests/

# ── Database ─────────────────────────────────────────────────────────────────
migrate:
	alembic upgrade head

migrate-down:
	alembic downgrade -1

reset-db:
	docker compose exec -T postgres psql -U littup -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'littup' AND pid <> pg_backend_pid();" || true
	docker compose exec -T postgres psql -U littup -d postgres -c "DROP DATABASE IF EXISTS littup;"
	docker compose exec -T postgres psql -U littup -d postgres -c "CREATE DATABASE littup;"
	alembic upgrade head

# ── Data ─────────────────────────────────────────────────────────────────────
seed:
	python scripts/seed.py

# ── OCR fixtures & bench ─────────────────────────────────────────────────────
fixtures:
	python scripts/generate_fixtures.py

bench-ocr:
	python scripts/bench_ocr.py

# ── Eval ─────────────────────────────────────────────────────────────────────
eval:
	python eval/run_all.py
