from pydantic_settings import BaseSettings, SettingsConfigDict


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
    VLLM_BASE_URL: str = "http://vllm:8001/v1"
    LLM_HOURLY_BUDGET_USD: float = 5.00
    MAX_VLM_PAGES_PER_DOC: int = 20
    ROUTER_CONFIG_PATH: str = "config/router.yaml"

    # Embeddings
    EMBEDDING_MODEL: str = "BAAI/bge-large-en-v1.5"
    EMBEDDING_BATCH_SIZE: int = 32
    RERANKER_MODEL: str = "BAAI/bge-reranker-base"
    EMBEDDER_PROVIDER: str = "bge"   # "bge" | "openai"

    # Ingestion (M3)
    MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024  # 50 MB
    UPLOAD_DIR: str = "data/uploads"
    PAGE_IMAGE_DIR: str = "data/page_images"
    SSE_KEEPALIVE_SECONDS: float = 15.0
    SSE_POLL_INTERVAL_SECONDS: float = 1.0
    SSE_MAX_STREAM_SECONDS: float = 600.0

    # Retrieval (M6)
    HNSW_EF_SEARCH: int = 100
    RETRIEVAL_WORK_MEM: str = "64MB"
    RETRIEVAL_STATEMENT_TIMEOUT: str = "5s"
    MAX_CHUNKS_PER_DOC_PER_QUERY: int = 3
    TRIGRAM_THRESHOLD: float = 0.15
    RETRIEVER_ALWAYS_TRIGRAM: bool = False

    # OCR (M4)
    OCR_CONFIG_PATH: str = "config/ocr.yaml"
    OCR_USE_GPU: bool = False
    OCR_PAGE_WORKERS: int = 4       # thread-pool size for per-page OCR
    OCR_RASTER_DPI: int = 300       # dpi used when rasterising PDF pages for OCR

    # Draft Engine (M7)
    TEMPLATES_DIR: str = "config/templates"
    DRAFT_EXTRACTION_TOP_K: int = 5
    DRAFT_SECTION_TOP_K: int = 8
    DRAFT_SECTION_MAX_TOKENS: int = 1200
    DRAFT_REGENERATE_TIMEOUT_S: int = 30
    WORKER_DRAFT_CONCURRENCY: int = 2

    # Edit Capture + Few-shot Store (M9)
    WORKER_CONCURRENCY_FEW_SHOT: int = 4
    FEW_SHOT_TOP_K: int = 3
    FEW_SHOT_INDEX_MAX_ATTEMPTS: int = 5
    EDIT_METRICS_DEFAULT_DAYS: int = 30


settings = Settings()
