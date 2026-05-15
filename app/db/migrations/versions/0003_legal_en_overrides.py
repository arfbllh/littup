"""Add legal_en dict overrides for proper nouns / statute cites.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-14
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TEXT SEARCH CONFIGURATION legal_en
          ALTER MAPPING FOR asciiword, word, numword
          WITH simple
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TEXT SEARCH CONFIGURATION legal_en
          ALTER MAPPING FOR asciiword, word, numword
          WITH english_stem
        """
    )
