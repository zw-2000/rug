"""users, server-side sessions, folder permissions and an append-only audit log

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("username", sa.Text, primary_key=True),  # lower-case sAMAccountName
        sa.Column("display_name", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "first_login", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_login", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Administrator-set block, independent of the directory (fast offboarding).
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Text, primary_key=True),  # sha256 of the random cookie token
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False, server_default=""),
        sa.Column("groups", postgresql.ARRAY(sa.Text), nullable=False),  # normalised DNs at login
        sa.Column("is_admin", sa.Boolean, nullable=False),
        sa.Column("csrf_token", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ip", sa.Text),
    )
    op.create_index("ix_sessions_username", "sessions", ["username"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    # AD group (normalised DN, or "*" for every signed-in user) -> top-level NAS folder.
    op.create_table(
        "group_folders",
        sa.Column("group_dn", sa.Text, nullable=False),
        sa.Column("folder", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("group_dn", "folder"),
    )

    # Per-user exceptions to the group mapping. A deny always wins.
    op.create_table(
        "user_overrides",
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("folder", sa.Text, nullable=False),
        sa.Column("effect", sa.String(8), nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("username", "folder"),
        sa.CheckConstraint("effect IN ('allow', 'deny')", name="ck_user_overrides_effect"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "detail", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("ip", sa.Text),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_action_at", "audit_log", ["action", "at"])
    op.create_index("ix_audit_log_actor", "audit_log", ["actor"])

    # Append-only: history cannot be edited or removed through the application role.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
    op.drop_table("audit_log")
    op.drop_table("user_overrides")
    op.drop_table("group_folders")
    op.drop_table("sessions")
    op.drop_table("users")
