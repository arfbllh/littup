"""app.document_events table for SSE replay.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-14
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.document_events (
            document_id  UUID        NOT NULL
                         REFERENCES app.documents(id) ON DELETE CASCADE,
            seq          BIGINT      NOT NULL,
            type         TEXT        NOT NULL,
            payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
            ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (document_id, seq)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.document_events")
