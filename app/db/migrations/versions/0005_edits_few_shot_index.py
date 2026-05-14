"""Add HNSW partial index on app.edits.embedding for few-shot retrieval.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-14
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS edits_embedding_hnsw_idx "
        "ON app.edits USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) "
        "WHERE few_shot_indexed_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.edits_embedding_hnsw_idx")
