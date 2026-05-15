"""Add template_extractor_state table for rule extractor.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-14
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.template_extractor_state (
            template_id TEXT PRIMARY KEY,
            last_run_at TIMESTAMPTZ,
            last_edit_id TEXT,
            edits_processed INT NOT NULL DEFAULT 0,
            rules_added INT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.template_extractor_state")
