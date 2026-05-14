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
	docker compose exec postgres psql -U littup -c "DROP DATABASE IF EXISTS littup;" || true
	docker compose exec postgres psql -U littup -c "CREATE DATABASE littup;" || true
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
