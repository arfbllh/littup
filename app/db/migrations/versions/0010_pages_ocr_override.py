"""Add app.pages.ocr_provider_override for per-page re-OCR with a chosen engine.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-15
"""

from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.pages
        ADD COLUMN IF NOT EXISTS ocr_provider_override TEXT NULL
            CHECK (ocr_provider_override IN ('pdfplumber', 'paddleocr'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.pages
        DROP COLUMN IF EXISTS ocr_provider_override
        """
    )
