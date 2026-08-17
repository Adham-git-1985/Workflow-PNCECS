"""add audience targeting to portal circulars

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-17 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("portal_circulars", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "target_scope",
            sa.String(length=20),
            nullable=False,
            server_default="ALL",
        ))
        batch_op.add_column(sa.Column("target_directorate_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("target_department_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_portal_circulars_target_directorate_id",
            "directorates",
            ["target_directorate_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_portal_circulars_target_department_id",
            "departments",
            ["target_department_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_portal_circulars_target_scope",
            ["target_scope"],
            unique=False,
        )
        batch_op.create_index(
            "ix_portal_circulars_target_directorate_id",
            ["target_directorate_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_portal_circulars_target_department_id",
            ["target_department_id"],
            unique=False,
        )
        batch_op.create_check_constraint(
            "ck_portal_circulars_target_scope",
            "target_scope IN ('ALL','DIRECTORATE','DEPARTMENT')",
        )
        batch_op.create_check_constraint(
            "ck_portal_circulars_target_fields",
            "(target_scope='ALL' AND target_directorate_id IS NULL AND target_department_id IS NULL) OR "
            "(target_scope='DIRECTORATE' AND target_directorate_id IS NOT NULL AND target_department_id IS NULL) OR "
            "(target_scope='DEPARTMENT' AND target_department_id IS NOT NULL AND target_directorate_id IS NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("portal_circulars", schema=None) as batch_op:
        batch_op.drop_constraint("ck_portal_circulars_target_fields", type_="check")
        batch_op.drop_constraint("ck_portal_circulars_target_scope", type_="check")
        batch_op.drop_index("ix_portal_circulars_target_department_id")
        batch_op.drop_index("ix_portal_circulars_target_directorate_id")
        batch_op.drop_index("ix_portal_circulars_target_scope")
        batch_op.drop_constraint(
            "fk_portal_circulars_target_department_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_portal_circulars_target_directorate_id",
            type_="foreignkey",
        )
        batch_op.drop_column("target_department_id")
        batch_op.drop_column("target_directorate_id")
        batch_op.drop_column("target_scope")
