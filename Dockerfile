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

# GPU build: replace the CPU paddlepaddle wheel with the matching GPU wheel.
# Enable by building with: docker build --build-arg PADDLE_GPU=1 ...
ARG PADDLE_GPU=0
RUN if [ "$PADDLE_GPU" = "1" ]; then \
        uv pip uninstall --system paddlepaddle || true && \
        uv pip install --system --no-cache paddlepaddle-gpu; \
    fi

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

# Copy installed packages from builder. These layers only bust when
# requirements.txt changes, so the model bake below stays cached across
# normal code edits.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Persist HF / sentence-transformers / paddleocr caches so they survive
# container restarts. All three point under /app/hf-cache (mounted as a named
# volume in docker-compose) so a single volume catches every downloaded blob
# regardless of which library wrote it.
ENV HF_HOME=/app/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/app/hf-cache/sentence-transformers \
    PADDLE_OCR_BASE_DIR=/app/hf-cache/paddleocr \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create writable dirs as root, then drop to appuser so the bake step (and
# everything that follows) runs unprivileged. Both /app/data and /app/hf-cache
# are created here so neither needs a chown after the COPY steps below.
RUN mkdir -p \
        /app/data/uploads \
        /app/data/page_images \
        /app/hf-cache/sentence-transformers \
        /app/hf-cache/paddleocr \
    && chown -R appuser:appuser /app

USER appuser

# Pre-bake the embedder + reranker into the image so a fresh container starts
# in seconds instead of pulling ~2.4 GB of weights on first boot. The models
# land under /app/hf-cache; the named volume mount in compose preserves them
# across restarts. Models are baked into the image layer, so even if the
# volume is wiped (`docker volume rm`), the next container start still finds
# the weights via the image's content.
#
# IMPORTANT: this RUN sits *before* COPY app/ and COPY config/ so that editing
# Python source or YAML templates does NOT invalidate the bake cache. It only
# busts when requirements.txt (and therefore site-packages) changes.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
    SentenceTransformer('BAAI/bge-large-en-v1.5'); \
    CrossEncoder('BAAI/bge-reranker-base')"

# Application code last so edits don't invalidate the (expensive) bake layer
# above. --chown ensures appuser owns the files since we've already USER-switched.
COPY --chown=appuser:appuser app/ app/
COPY --chown=appuser:appuser config/ config/
COPY --chown=appuser:appuser alembic.ini ./

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
