"""add independent team memberships

Revision ID: i5d6e7f8g9h0
Revises: h4c5d6e7f8g9
Create Date: 2026-08-21
"""

from alembic import op
import sqlalchemy as sa


revision = "i5d6e7f8g9h0"
down_revision = "h4c5d6e7f8g9"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "team_memberships" not in inspector.get_table_names():
        op.create_table(
            "team_memberships",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("team_id", "user_id", name="uq_team_membership_team_user"),
        )
        op.create_index("ix_team_memberships_team_id", "team_memberships", ["team_id"])
        op.create_index("ix_team_memberships_user_id", "team_memberships", ["user_id"])
        op.create_index("ix_team_memberships_is_active", "team_memberships", ["is_active"])

    bind = op.get_bind()
    if "org_unit_assignment" in inspector.get_table_names():
        bind.execute(sa.text(
            "INSERT OR IGNORE INTO team_memberships "
            "(team_id, user_id, title, is_active, created_at, created_by_id) "
            "SELECT unit_id, user_id, title, 1, COALESCE(created_at, CURRENT_TIMESTAMP), created_by_id "
            "FROM org_unit_assignment WHERE UPPER(unit_type)='TEAM'"
        ))


def downgrade():
    op.drop_index("ix_team_memberships_is_active", table_name="team_memberships")
    op.drop_index("ix_team_memberships_user_id", table_name="team_memberships")
    op.drop_index("ix_team_memberships_team_id", table_name="team_memberships")
    op.drop_table("team_memberships")
