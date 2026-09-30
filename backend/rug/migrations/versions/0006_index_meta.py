"""index_meta: which embedding model produced the stored vectors

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "index_meta",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("index_meta")
