"""question/answer log with per-answer feedback (admin-only; purged after a retention period)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qa_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("mode", sa.Text, nullable=False, server_default=""),
        sa.Column("resolved_doc", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "chunk_ids", postgresql.ARRAY(sa.BigInteger), nullable=False, server_default="{}"
        ),
        sa.Column("answer", sa.Text, nullable=False, server_default=""),
        sa.Column("ungrounded", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("model", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("feedback", sa.SmallInteger, nullable=True),
        sa.Column("comment", sa.Text, nullable=True),
        sa.CheckConstraint("feedback IN (-1, 1)", name="ck_qa_log_feedback"),
    )
    op.create_index("ix_qa_log_created_at", "qa_log", ["created_at"])
    op.create_index("ix_qa_log_username_id", "qa_log", ["username", "id"])


def downgrade() -> None:
    op.drop_table("qa_log")
