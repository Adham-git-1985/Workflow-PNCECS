"""add independent approval workflow for manual attendance edits

Revision ID: aa1b2c3d4e5f
Revises: z1a2b3c4d5e6
Create Date: 2026-09-01
"""

from alembic import op
import sqlalchemy as sa


revision = "aa1b2c3d4e5f"
down_revision = "z1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("hr_att_special_case") as batch:
        batch.add_column(
            sa.Column(
                "approval_status",
                sa.String(length=20),
                nullable=False,
                server_default="APPROVED",
            )
        )
        batch.add_column(sa.Column("approved_by_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("approved_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("approval_note", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_hr_att_special_case_approved_by",
            "users",
            ["approved_by_id"],
            ["id"],
        )
        batch.create_index("ix_hr_att_special_case_approval_status", ["approval_status"])
        batch.create_index("ix_hr_att_special_case_approved_by_id", ["approved_by_id"])
        batch.create_index("ix_hr_att_special_case_approved_at", ["approved_at"])

    # Initial policy: the Secretary General and Super Admin receive both
    # independent permissions. Administrators can still change either grant
    # later from the Portal permissions screen.
    bind = op.get_bind()
    role_codes = bind.execute(
        sa.text(
            "SELECT code FROM roles "
            "WHERE upper(code) IN ('GENERAL_SECRETARY', 'SUPER_ADMIN')"
        )
    ).scalars().all()
    for role_code in role_codes:
        for permission in ("HR_ATTENDANCE_EDIT", "HR_ATTENDANCE_EDIT_APPROVE"):
            bind.execute(
                sa.text(
                    "INSERT INTO role_permission (role, permission) "
                    "SELECT :role, :permission "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM role_permission "
                    "  WHERE lower(role) = lower(:role) "
                    "    AND permission = :permission"
                    ")"
                ),
                {"role": role_code, "permission": permission},
            )


def downgrade():
    with op.batch_alter_table("hr_att_special_case") as batch:
        batch.drop_index("ix_hr_att_special_case_approved_at")
        batch.drop_index("ix_hr_att_special_case_approved_by_id")
        batch.drop_index("ix_hr_att_special_case_approval_status")
        batch.drop_constraint("fk_hr_att_special_case_approved_by", type_="foreignkey")
        batch.drop_column("approval_note")
        batch.drop_column("approved_at")
        batch.drop_column("approved_by_id")
        batch.drop_column("approval_status")
