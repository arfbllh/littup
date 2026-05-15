"""Add app.reextract_sessions and app.reextract_page_results for the
operator preview/accept-reject re-OCR workflow.

A session is created when the operator picks pages + an engine; OCR runs
into the staging table; the operator inspects the diff per page and
accepts/discards. Accepted pages overwrite app.spans for that page; the
doc-level pipeline (layout → chunking → embedding) re-runs once.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-15
"""

from alembic import op


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.reextract_sessions (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id  UUID NOT NULL REFERENCES app.documents(id) ON DELETE CASCADE,
            provider     TEXT NOT NULL CHECK (provider IN ('pdfplumber', 'paddleocr')),
            page_numbers INTEGER[] NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'running', 'ready', 'accepted', 'rejected', 'failed')),
            error_message TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS reextract_sessions_doc_idx
            ON app.reextract_sessions (document_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.reextract_page_results (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id  UUID NOT NULL REFERENCES app.reextract_sessions(id) ON DELETE CASCADE,
            page_id     UUID NOT NULL REFERENCES app.pages(id) ON DELETE CASCADE,
            page_number INTEGER NOT NULL,
            provider    TEXT NOT NULL,
            old_text    TEXT NOT NULL,
            new_text    TEXT NOT NULL,
            new_spans   JSONB NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'ok', 'failed')),
            error_message TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (session_id, page_number)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.reextract_page_results")
    op.execute("DROP TABLE IF EXISTS app.reextract_sessions")
