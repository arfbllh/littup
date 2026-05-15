"""Add jobs.jobs.cancel_requested for cooperative cancellation.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15
"""

from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE jobs.jobs
        ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE jobs.jobs
        DROP COLUMN IF EXISTS cancel_requested
        """
    )
