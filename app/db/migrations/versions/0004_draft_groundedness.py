"""Add groundedness_score to app.drafts.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "drafts",
        sa.Column("groundedness_score", sa.Numeric(4, 3), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("drafts", "groundedness_score", schema="app")
