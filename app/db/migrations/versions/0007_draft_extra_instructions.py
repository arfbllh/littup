"""Add app.drafts.extra_instructions for WS-F pre-flight composer.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-15
"""

from alembic import op


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.drafts
        ADD COLUMN IF NOT EXISTS extra_instructions TEXT NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.drafts
        DROP COLUMN IF EXISTS extra_instructions
        """
    )
