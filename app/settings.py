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

    # Ingestion (M3)
    MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024  # 50 MB
    UPLOAD_DIR: str = "data/uploads"
    PAGE_IMAGE_DIR: str = "data/page_images"
    SSE_KEEPALIVE_SECONDS: float = 15.0
    SSE_POLL_INTERVAL_SECONDS: float = 1.0
    SSE_MAX_STREAM_SECONDS: float = 600.0


settings = Settings()
