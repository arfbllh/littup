"""Initial schema: extensions, three schemas, all tables, indexes, legal_en FTS config,
partitioned llm_requests.

Revision ID: 0001
Revises:
Create Date: 2026-05-14
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Extensions ────────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── Schemas ───────────────────────────────────────────────────────────────
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("CREATE SCHEMA IF NOT EXISTS jobs")
    op.execute("CREATE SCHEMA IF NOT EXISTS llm_log")

    # ── legal_en text search configuration ───────────────────────────────────
    # Starts as a copy of 'english'. The asciiword/word/numword mappings
    # will be overridden to use the 'simple' dictionary for tokens that look
    # like proper nouns, acronyms, or statute citations.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_ts_config WHERE cfgname = 'legal_en'
            ) THEN
                CREATE TEXT SEARCH CONFIGURATION legal_en (COPY = english);
            END IF;
        END
        $$
    """)

    # SQL function that validates the legal_en config (used in integration tests)
    op.execute("""
        CREATE OR REPLACE FUNCTION legal_en_test()
        RETURNS tsvector
        LANGUAGE sql IMMUTABLE AS
        $$
            SELECT to_tsvector('legal_en',
                'plaintiff Pearson Specter Litt filed 42 U.S.C. § 1983')
        $$
    """)

    # ── app.documents ─────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.documents (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            sha256           TEXT NOT NULL UNIQUE,
            filename         TEXT NOT NULL,
            mime_type        TEXT,
            size_bytes       BIGINT,
            page_count       INT,
            doc_type         TEXT,
            status           TEXT NOT NULL DEFAULT 'uploaded',
            embedded_at      TIMESTAMPTZ,
            last_event_seq   BIGINT NOT NULL DEFAULT 0,
            error_code       TEXT,
            error_message    TEXT,
            vlm_pages_used   INT NOT NULL DEFAULT 0,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_accessed_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_documents_status ON app.documents (status)")

    # ── app.pages ─────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.pages (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id UUID NOT NULL REFERENCES app.documents(id) ON DELETE CASCADE,
            page_number INT NOT NULL,
            width       FLOAT,
            height      FLOAT,
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_pages_document_id ON app.pages (document_id)")

    # ── app.blocks ────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.blocks (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id     UUID NOT NULL REFERENCES app.documents(id) ON DELETE CASCADE,
            parent_block_id UUID REFERENCES app.blocks(id) ON DELETE SET NULL,
            block_type      TEXT NOT NULL,
            page_start      INT,
            page_end        INT,
            bbox_x0         FLOAT,
            bbox_y0         FLOAT,
            bbox_x1         FLOAT,
            bbox_y1         FLOAT,
            text            TEXT,
            metadata        JSONB,
            reading_order   INT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_blocks_document_id ON app.blocks (document_id)")

    # ── app.spans ─────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.spans (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            page_id    UUID NOT NULL REFERENCES app.pages(id) ON DELETE CASCADE,
            block_id   UUID REFERENCES app.blocks(id) ON DELETE SET NULL,
            text       TEXT NOT NULL,
            bbox_x0    FLOAT,
            bbox_y0    FLOAT,
            bbox_x1    FLOAT,
            bbox_y1    FLOAT,
            confidence FLOAT,
            source     TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_spans_page_id ON app.spans (page_id)")

    # ── app.chunks ────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.chunks (
            id                  UUID PRIMARY KEY,
            document_id         UUID NOT NULL REFERENCES app.documents(id) ON DELETE CASCADE,
            text                TEXT NOT NULL,
            text_tsv            TSVECTOR,
            embedding           VECTOR(1024),
            section_path        TEXT[],
            chunk_type          TEXT,
            page_start          INT,
            page_end            INT,
            char_start          INT,
            char_end            INT,
            token_count         INT,
            block_ids           TEXT[],
            entities            TEXT[],
            prompt_fingerprint  TEXT,
            metadata            JSONB,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_chunks_document_id ON app.chunks (document_id)")

    # HNSW index — explicit params required
    op.execute("""
        CREATE INDEX chunks_embedding_hnsw_idx ON app.chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
    """)
    # GIN on tsvector for BM25 search
    op.execute("CREATE INDEX chunks_tsv_gin_idx ON app.chunks USING GIN (text_tsv)")
    # trigram GIN on raw text for proper-noun fuzzy search
    op.execute("CREATE INDEX chunks_text_trgm_idx ON app.chunks USING GIN (text gin_trgm_ops)")
    # GIN on entities array for exact entity match
    op.execute("CREATE INDEX chunks_entities_gin_idx ON app.chunks USING GIN (entities)")

    # Trigger to auto-populate text_tsv using legal_en config whenever text changes
    op.execute("""
        CREATE OR REPLACE FUNCTION app.chunks_tsv_update()
        RETURNS trigger LANGUAGE plpgsql AS
        $$
        BEGIN
            NEW.text_tsv := to_tsvector('legal_en', COALESCE(NEW.text, ''));
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER chunks_tsv_trigger
        BEFORE INSERT OR UPDATE OF text ON app.chunks
        FOR EACH ROW EXECUTE FUNCTION app.chunks_tsv_update()
    """)

    # ── app.drafts ────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.drafts (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            template_id         TEXT NOT NULL,
            template_version    INT  NOT NULL,
            prompt_fingerprint  TEXT NOT NULL,
            document_ids        UUID[],
            status              TEXT NOT NULL DEFAULT 'generating',
            ai_output           JSONB,
            final_output        JSONB,
            model_used          TEXT,
            tokens_in           INT,
            tokens_out          INT,
            cost_usd            NUMERIC(12,6),
            generated_at        TIMESTAMPTZ,
            edited_at           TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── app.sections ──────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.sections (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            draft_id          UUID NOT NULL REFERENCES app.drafts(id) ON DELETE CASCADE,
            name              TEXT NOT NULL,
            ai_text           TEXT,
            final_text        TEXT,
            target_length_min INT,
            target_length_max INT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_sections_draft_id ON app.sections (draft_id)")

    # ── app.citations ─────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.citations (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            section_id        UUID NOT NULL REFERENCES app.sections(id) ON DELETE CASCADE,
            chunk_id          UUID NOT NULL REFERENCES app.chunks(id)   ON DELETE CASCADE,
            claim_span_start  INT,
            claim_span_end    INT,
            validation_status TEXT NOT NULL DEFAULT 'unchecked',
            validation_reason TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_citations_section_id ON app.citations (section_id)")
    op.execute("CREATE INDEX ix_citations_chunk_id   ON app.citations (chunk_id)")

    # ── app.edits ─────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.edits (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            draft_id               UUID NOT NULL REFERENCES app.drafts(id) ON DELETE CASCADE,
            template_id            TEXT NOT NULL,
            template_version       INT  NOT NULL,
            prompt_fingerprint     TEXT NOT NULL,
            field_or_section_name  TEXT NOT NULL,
            field_type             TEXT NOT NULL,
            ai_value               JSONB,
            user_value             JSONB,
            diff                   JSONB,
            context                JSONB,
            embedding              VECTOR(1024),
            few_shot_indexed_at    TIMESTAMPTZ,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_edits_draft_id           ON app.edits (draft_id)")
    op.execute("CREATE INDEX ix_edits_prompt_fingerprint ON app.edits (prompt_fingerprint)")
    op.execute("CREATE INDEX ix_edits_few_shot_unindexed ON app.edits (created_at) WHERE few_shot_indexed_at IS NULL")

    # ── app.templates ─────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE app.templates (
            template_id        TEXT NOT NULL,
            version            INT  NOT NULL,
            yaml_body          TEXT NOT NULL,
            system_prompt      TEXT,
            appended_rules     JSONB NOT NULL DEFAULT '[]',
            prompt_fingerprint TEXT NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (template_id, version)
        )
    """)

    # ── jobs.jobs ─────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE jobs.jobs (
            id           UUID PRIMARY KEY,
            kind         TEXT NOT NULL,
            payload      JSONB NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            attempts     INT  NOT NULL DEFAULT 0,
            max_attempts INT  NOT NULL DEFAULT 3,
            picked_up_at TIMESTAMPTZ,
            heartbeat_at TIMESTAMPTZ,
            worker_id    TEXT,
            result       JSONB,
            error        TEXT,
            dedup_key    TEXT UNIQUE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_jobs_status ON jobs.jobs (status)")
    op.execute("CREATE INDEX ix_jobs_kind   ON jobs.jobs (kind)")
    # Partial index for the claim_one query (only pending rows)
    op.execute("CREATE INDEX ix_jobs_pending ON jobs.jobs (kind, created_at) WHERE status = 'pending'")

    # ── jobs.job_history ──────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE jobs.job_history (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id     UUID NOT NULL,
            kind       TEXT NOT NULL,
            status     TEXT NOT NULL,
            worker_id  TEXT,
            error      TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_job_history_job_id ON jobs.job_history (job_id)")

    # ── llm_log.llm_requests (partitioned by month on created_at) ────────────
    op.execute("""
        CREATE TABLE llm_log.llm_requests (
            id                UUID          NOT NULL,
            trace_id          TEXT,
            tier              TEXT,
            provider          TEXT,
            model             TEXT,
            prompt_fingerprint TEXT,
            cache_key         TEXT,
            tokens_in         INT,
            tokens_out        INT,
            cost_usd          NUMERIC(10,6),
            latency_ms        INT,
            status            TEXT,
            error_code        TEXT,
            cache_hit         BOOLEAN,
            created_at        TIMESTAMPTZ   NOT NULL,
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        -- Future months: add partitions manually or via pg_partman.
        -- Example: CREATE TABLE llm_log.llm_requests_2026_06
        --   PARTITION OF llm_log.llm_requests
        --   FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
    """)

    # First month partition (current: 2026-05)
    op.execute("""
        CREATE TABLE llm_log.llm_requests_2026_05
            PARTITION OF llm_log.llm_requests
            FOR VALUES FROM ('2026-05-01') TO ('2026-06-01')
    """)

    # ── llm_log.llm_cache ─────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE llm_log.llm_cache (
            cache_key  TEXT PRIMARY KEY,
            response   JSONB,
            model      TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX ix_llm_cache_expires_at ON llm_log.llm_cache (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_log.llm_cache CASCADE")
    op.execute("DROP TABLE IF EXISTS llm_log.llm_requests CASCADE")
    op.execute("DROP TABLE IF EXISTS jobs.job_history CASCADE")
    op.execute("DROP TABLE IF EXISTS jobs.jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS app.templates CASCADE")
    op.execute("DROP TABLE IF EXISTS app.edits CASCADE")
    op.execute("DROP TABLE IF EXISTS app.citations CASCADE")
    op.execute("DROP TABLE IF EXISTS app.sections CASCADE")
    op.execute("DROP TABLE IF EXISTS app.drafts CASCADE")
    op.execute("DROP TABLE IF EXISTS app.chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS app.spans CASCADE")
    op.execute("DROP TABLE IF EXISTS app.blocks CASCADE")
    op.execute("DROP TABLE IF EXISTS app.pages CASCADE")
    op.execute("DROP TABLE IF EXISTS app.documents CASCADE")
    op.execute("DROP FUNCTION IF EXISTS app.chunks_tsv_update() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS legal_en_test() CASCADE")
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS legal_en CASCADE")
    op.execute("DROP SCHEMA IF EXISTS llm_log CASCADE")
    op.execute("DROP SCHEMA IF EXISTS jobs CASCADE")
    op.execute("DROP SCHEMA IF EXISTS app CASCADE")
