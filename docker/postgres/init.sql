-- Run once on cold-start before Alembic. Alembic also creates these,
-- but doing it here makes the first `alembic upgrade head` faster.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
