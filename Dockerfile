# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN pip install uv

COPY pyproject.toml .
RUN uv pip install --system --no-cache -e ".[dev]" 2>/dev/null || \
    uv pip install --system --no-cache \
        fastapi uvicorn[standard] pydantic pydantic-settings \
        python-multipart httpx sqlalchemy[asyncio] asyncpg alembic pgvector \
        structlog uuid7 python-dotenv sse-starlette anthropic openai \
        pdfplumber Pillow

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY app/ app/
COPY config/ config/

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
