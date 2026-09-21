"""add planned annual leave rollover decisions

Revision ID: z5a6b7c8d9e0
Revises: z4a5b6c7d8e9
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa


revision = "z5a6b7c8d9e0"
down_revision = "z4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("hr_leave_rollover_decision"):
        return

    op.create_table(
        "hr_leave_rollover_decision",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("leave_type_id", sa.Integer(), nullable=False),
        sa.Column("source_year", sa.Integer(), nullable=False),
        sa.Column("target_year", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("transfer_days", sa.Float(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("applied_source_days", sa.Float(), nullable=True),
        sa.Column("applied_transfer_days", sa.Float(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["leave_type_id"], ["hr_leave_type.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "leave_type_id",
            "source_year",
            "target_year",
            name="uq_hr_leave_rollover_decision_scope",
        ),
    )
    op.create_index(
        "ix_hr_leave_rollover_decision_user_id",
        "hr_leave_rollover_decision",
        ["user_id"],
    )
    op.create_index(
        "ix_hr_leave_rollover_decision_leave_type_id",
        "hr_leave_rollover_decision",
        ["leave_type_id"],
    )
    op.create_index(
        "ix_hr_leave_rollover_decision_source_year",
        "hr_leave_rollover_decision",
        ["source_year"],
    )
    op.create_index(
        "ix_hr_leave_rollover_decision_target_year",
        "hr_leave_rollover_decision",
        ["target_year"],
    )
    op.create_index(
        "ix_hr_leave_rollover_decision_applied_at",
        "hr_leave_rollover_decision",
        ["applied_at"],
    )
    op.create_index(
        "ix_hr_leave_rollover_due",
        "hr_leave_rollover_decision",
        ["target_year", "applied_at"],
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("hr_leave_rollover_decision"):
        op.drop_table("hr_leave_rollover_decision")
