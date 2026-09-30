"""editable document-type vocabulary (SOW, CR, MSA, NDA, ...) used by the resolver

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Frozen copy of the defaults at the time of this migration. An empty table makes the
# resolver fall back to its built-in defaults, so deleting every row is a "reset".
_SEED = {
    "SOW": ["sow", "statement of work", "statements of work"],
    "CR": ["cr", "change request", "change requests"],
    "MSA": ["msa", "master services agreement", "master service agreement"],
    "NDA": ["nda", "non-disclosure agreement", "non disclosure agreement", "nondisclosure"],
}


def upgrade() -> None:
    table = op.create_table(
        "doc_types",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("phrases", postgresql.ARRAY(sa.Text), nullable=False),
    )
    op.bulk_insert(table, [{"name": k, "phrases": v} for k, v in _SEED.items()])


def downgrade() -> None:
    op.drop_table("doc_types")
