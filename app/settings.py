from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Push .env into os.environ so libraries that read env vars directly (HuggingFace
# Hub looks for HF_TOKEN / HUGGING_FACE_HUB_TOKEN, OpenAI for OPENAI_API_KEY,
# etc.) pick them up. pydantic-settings reads .env into the Settings object but
# does not export it; this fills that gap. `override=False` keeps real shell
# vars authoritative over .env.
load_dotenv(override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://littup:littup@localhost:5432/littup"
    DATABASE_URL_DIRECT: str = "postgresql+asyncpg://littup:littup@localhost:5432/littup"
    TEST_DATABASE_URL: str = "postgresql+asyncpg://littup:littup@localhost:5432/littup_test"

    # Worker
    WORKER_CONCURRENCY_OCR: int = 2
    WORKER_CONCURRENCY_EMBEDDING: int = 4
    WORKER_CONCURRENCY_DEFAULT: int = 2
    WORKER_HEARTBEAT_INTERVAL: int = 10
    JOB_STALE_TIMEOUT: int = 120
    JOB_QUEUE_MAX_PENDING: int = 100

    # LLM
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    # Ollama daemon root (no /v1 suffix). When this env var is absent from the
    # environment, ollama-typed providers are skipped entirely and the router
    # falls straight to the next provider in each tier (typically Anthropic).
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    LLM_HOURLY_BUDGET_USD: float = 5.00
    MAX_VLM_PAGES_PER_DOC: int = 20
    ROUTER_CONFIG_PATH: str = "config/router.yaml"
    # Debug: when True, structlog-dump every outbound LLM request (messages,
    # schema) and the response text. Off by default — payloads can be large.
    LLM_LOG_PROMPTS: bool = False
    LLM_LOG_PROMPTS_MAX_CHARS: int = 4000

    # Embeddings
    EMBEDDING_MODEL: str = "BAAI/bge-large-en-v1.5"
    EMBEDDING_BATCH_SIZE: int = 32
    RERANKER_MODEL: str = "BAAI/bge-reranker-base"
    EMBEDDER_PROVIDER: str = "bge"   # "bge" | "openai"
    HF_TOKEN: str = ""               # picked up by huggingface_hub via os.environ
    TORCH_DEVICE: str = "cpu"        # "cpu" | "mps" | "cuda" — MPS on Apple Silicon hits a 9GB pool cap

    # Ingestion 
    MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024  # 50 MB
    UPLOAD_DIR: str = "data/uploads"
    PAGE_IMAGE_DIR: str = "data/page_images"
    SSE_KEEPALIVE_SECONDS: float = 15.0
    SSE_POLL_INTERVAL_SECONDS: float = 1.0
    SSE_MAX_STREAM_SECONDS: float = 600.0

    # Retrieval 
    HNSW_EF_SEARCH: int = 100
    RETRIEVAL_WORK_MEM: str = "64MB"
    RETRIEVAL_STATEMENT_TIMEOUT: str = "5s"
    MAX_CHUNKS_PER_DOC_PER_QUERY: int = 3
    TRIGRAM_THRESHOLD: float = 0.15
    RETRIEVER_ALWAYS_TRIGRAM: bool = False

    # OCR 
    OCR_CONFIG_PATH: str = "config/ocr.yaml"
    OCR_USE_GPU: bool = False
    OCR_PAGE_WORKERS: int = 4       # thread-pool size for per-page OCR
    OCR_RASTER_DPI: int = 300       # dpi used when rasterising PDF pages for OCR
    OCR_MAX_IMAGE_SIDE: int = 4000  # cap longest side before passing to PaddleOCR (saves RAM, matches det max_side_limit)
    OCR_FORCE_RASTER: bool = False  # if True, ignore native text layer and rasterise+OCR every PDF
    OCR_PREPROCESS_ALL: bool = False  # if True, run deskew+denoise+binarize on every scan page, not just classifier-flagged degraded ones

    # Draft Engine
    TEMPLATES_DIR: str = "config/templates"
    DRAFT_EXTRACTION_TOP_K: int = 5
    DRAFT_SECTION_TOP_K: int = 8
    DRAFT_SECTION_MAX_TOKENS: int = 1200
    DRAFT_REGENERATE_TIMEOUT_S: int = 30
    WORKER_DRAFT_CONCURRENCY: int = 2

    # Edit Capture + Few-shot Store
    WORKER_CONCURRENCY_FEW_SHOT: int = 4
    FEW_SHOT_TOP_K: int = 3
    FEW_SHOT_INDEX_MAX_ATTEMPTS: int = 5
    EDIT_METRICS_DEFAULT_DAYS: int = 30

    # Rule Extractor
    RULE_EXTRACTOR_INTERVAL_HOURS: int = 6
    RULE_EXTRACTOR_MIN_EDITS: int = 3
    RULE_EXTRACTOR_SIMILARITY_THRESHOLD: float = 0.88
    RULE_EXTRACTOR_MAX_EDITS_PER_GROUP: int = 12
    RULE_EXTRACTOR_FIRST_RUN_LOOKBACK_DAYS: int = 30
    RULE_EXTRACTOR_VERSION_LOOKBACK: int = 2
    RULE_EXTRACTOR_ADMIN_MAX_DURATION_S: int = 180
    RULE_EXTRACTOR_LOCK_ID: int = 9_010_001


settings = Settings()
