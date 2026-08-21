"""add user dynamic workflow presets

Revision ID: k7f8g9h0i1j2
Revises: j6e7f8g9h0i1
Create Date: 2026-08-21
"""

from alembic import op
import sqlalchemy as sa


revision = "k7f8g9h0i1j2"
down_revision = "j6e7f8g9h0i1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_dynamic_workflow_presets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("target_refs_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_user_dynamic_workflow_preset_name"),
    )
    op.create_index(
        "ix_user_dynamic_workflow_presets_user_id",
        "user_dynamic_workflow_presets",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_dynamic_workflow_preset_updated",
        "user_dynamic_workflow_presets",
        ["user_id", "updated_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_user_dynamic_workflow_preset_updated",
        table_name="user_dynamic_workflow_presets",
    )
    op.drop_index(
        "ix_user_dynamic_workflow_presets_user_id",
        table_name="user_dynamic_workflow_presets",
    )
    op.drop_table("user_dynamic_workflow_presets")
