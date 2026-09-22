"""add permanent attendance-report exclusions

Revision ID: z8a9b0c1d2e3
Revises: z7a8b9c0d1e2
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa


revision = "z8a9b0c1d2e3"
down_revision = "z7a8b9c0d1e2"
branch_labels = None
depends_on = None


_TABLE = "hr_attendance_exemption"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_hr_attendance_exemption_user"),
    )
    op.create_index("ix_hr_attendance_exemption_user_id", _TABLE, ["user_id"])
    op.create_index("ix_hr_attendance_exemption_created_by_id", _TABLE, ["created_by_id"])
    op.create_index("ix_hr_attendance_exemption_created_at", _TABLE, ["created_at"])


def downgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        op.drop_table(_TABLE)
