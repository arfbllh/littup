# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Build deps for any wheels that fall back to source (paddleocr, opencv, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Runtime system deps:
#   poppler-utils    → pdf2image (pdftoppm/pdfinfo) for page rendering
#   libgomp1         → OpenMP runtime for torch / paddlepaddle
#   libglib2.0-0     → required by opencv-python-headless at import time
#   libgl1           → libGL.so.1 used by some paddleocr/opencv code paths
#   curl, ca-certs   → healthchecks + outbound HTTPS to model hubs
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        libgomp1 \
        libglib2.0-0 \
        libgl1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with a real home directory (HuggingFace cache defaults to ~)
RUN groupadd -r appuser && useradd -r -m -g appuser appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY app/ app/
COPY config/ config/

# Persist HF / sentence-transformers / paddleocr caches on the data volume so
# they survive container restarts instead of re-downloading on every boot.
ENV HF_HOME=/app/data/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/app/data/hf-cache/sentence-transformers \
    PADDLE_OCR_BASE_DIR=/app/data/paddleocr-cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p \
        /app/data/uploads \
        /app/data/page_images \
        /app/data/hf-cache \
        /app/data/paddleocr-cache \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
