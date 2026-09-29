"""catalog, chunks (tsvector + pgvector), index runs

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from rug.db.models import EMBED_DIM, TSV_EXPR

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("path", sa.Text, nullable=False, unique=True),
        sa.Column("folder", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("ext", sa.String(16), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("title_source", sa.String(16), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("mtime", sa.Float, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("version_key", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column(
            "indexed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_documents_folder", "documents", ["folder"])
    op.create_index("ix_documents_sha256", "documents", ["sha256"])
    op.create_index("ix_documents_folder_version", "documents", ["folder", "version_key"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ord", sa.Integer, nullable=False),
        sa.Column("heading_path", sa.Text, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
        sa.Column("tsv", postgresql.TSVECTOR, sa.Computed(TSV_EXPR, persisted=True)),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "index_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("total", sa.Integer, nullable=False),
        sa.Column("processed", sa.Integer, nullable=False),
        sa.Column("counts", postgresql.JSONB, nullable=False),
        sa.Column("errors", postgresql.JSONB, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("index_runs")
    op.drop_table("chunks")
    op.drop_table("documents")
