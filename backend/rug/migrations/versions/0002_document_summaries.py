"""document summaries (content-hash keyed) with a document-level embedding

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

EMBED_DIM = 768  # frozen copy; see 0001


def upgrade() -> None:
    op.create_table(
        "document_summaries",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # No vector index: one row per distinct document, scored exactly.


def downgrade() -> None:
    op.drop_table("document_summaries")
