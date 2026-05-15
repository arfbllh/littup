"""Add app.documents.ocr_provider_override for operator-driven OCR engine choice.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-15
"""

from alembic import op


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.documents
        ADD COLUMN IF NOT EXISTS ocr_provider_override TEXT NULL
            CHECK (ocr_provider_override IN ('pdfplumber', 'paddleocr'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.documents
        DROP COLUMN IF EXISTS ocr_provider_override
        """
    )
