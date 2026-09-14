"""link daily attendance, automatic leave, and final manual approval

Revision ID: j3c4d5e6f7g8
Revises: i2b3c4d5e6f7
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa


revision = "j3c4d5e6f7g8"
down_revision = "i2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("hr_leave_request") as batch:
        batch.add_column(sa.Column("source", sa.String(length=30), nullable=True))
        batch.add_column(sa.Column("source_attendance_day", sa.String(length=10), nullable=True))
        batch.add_column(sa.Column("replaced_by_leave_request_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("replaced_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("replacement_reason", sa.String(length=80), nullable=True))
        batch.create_foreign_key(
            "fk_hr_leave_request_replaced_by",
            "hr_leave_request",
            ["replaced_by_leave_request_id"],
            ["id"],
        )
        batch.create_index("ix_hr_leave_request_source", ["source"])
        batch.create_index("ix_hr_leave_request_source_attendance_day", ["source_attendance_day"])
        batch.create_index("ix_hr_leave_request_replaced_by_leave_request_id", ["replaced_by_leave_request_id"])
        batch.create_index("ix_hr_leave_request_replaced_at", ["replaced_at"])
        batch.create_unique_constraint(
            "uq_hr_leave_attendance_auto_day",
            ["user_id", "source", "source_attendance_day"],
        )

    with op.batch_alter_table("hr_att_special_case") as batch:
        batch.add_column(sa.Column("final_approved_by_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("final_approved_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("final_approval_note", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_hr_att_special_case_final_approved_by",
            "users",
            ["final_approved_by_id"],
            ["id"],
        )
        batch.create_index("ix_hr_att_special_case_final_approved_by_id", ["final_approved_by_id"])
        batch.create_index("ix_hr_att_special_case_final_approved_at", ["final_approved_at"])

    # Keep the two approval gates usable after deployment even where these
    # roles were created after the original manual-attendance migration.
    bind = op.get_bind()
    role_codes = bind.execute(
        sa.text(
            "SELECT code FROM roles "
            "WHERE upper(replace(replace(code, '-', '_'), ' ', '_')) IN ("
            "'HR', 'HR_ADMIN', 'HR_MANAGER', "
            "'GENERAL_SECRETARY', 'SECRETARY_GENERAL'"
            ")"
        )
    ).scalars().all()
    for role_code in role_codes:
        normalized = str(role_code or "").strip().upper().replace("-", "_").replace(" ", "_")
        permissions = ["HR_ATTENDANCE_EDIT_APPROVE", "HR_REPORTS_VIEW", "HR_REPORTS_EXPORT"]
        if normalized in {"HR", "HR_ADMIN", "HR_MANAGER"}:
            permissions.append("HR_ATTENDANCE_EDIT")
        for permission in permissions:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permission (role, permission) "
                    "SELECT :role, :permission "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM role_permission "
                    "  WHERE lower(role) = lower(:role) AND permission = :permission"
                    ")"
                ),
                {"role": role_code, "permission": permission},
            )

    # Grant the first-stage permission to the manager currently assigned to
    # the Administrative Affairs directorate/department. Runtime resolution
    # remains dynamic for future manager changes; this grant also makes the
    # assignment visible in the ordinary permissions administration screen.
    manager_ids = set(bind.execute(sa.text(
        "SELECT DISTINCT m.manager_user_id "
        "FROM org_node_managers m "
        "JOIN org_nodes n ON n.id = m.node_id "
        "WHERE m.manager_user_id IS NOT NULL AND ("
        "  n.name_ar LIKE '%شؤون%إداري%' "
        "  OR n.name_ar LIKE '%شؤون%اداري%' "
        "  OR lower(coalesce(n.name_en, '')) LIKE '%administrative%affairs%'"
        ")"
    )).scalars().all())
    manager_ids.update(bind.execute(sa.text(
        "SELECT DISTINCT m.manager_user_id "
        "FROM org_unit_manager m "
        "JOIN directorates d ON m.unit_type = 'DIRECTORATE' AND d.id = m.unit_id "
        "WHERE m.manager_user_id IS NOT NULL AND ("
        "  d.name_ar LIKE '%شؤون%إداري%' "
        "  OR d.name_ar LIKE '%شؤون%اداري%' "
        "  OR lower(coalesce(d.name_en, '')) LIKE '%administrative%affairs%'"
        ") "
        "UNION "
        "SELECT DISTINCT m.manager_user_id "
        "FROM org_unit_manager m "
        "JOIN departments d ON m.unit_type = 'DEPARTMENT' AND d.id = m.unit_id "
        "WHERE m.manager_user_id IS NOT NULL AND ("
        "  d.name_ar LIKE '%شؤون%إداري%' "
        "  OR d.name_ar LIKE '%شؤون%اداري%' "
        "  OR lower(coalesce(d.name_en, '')) LIKE '%administrative%affairs%'"
        ")"
    )).scalars().all())
    for manager_id in (int(value) for value in manager_ids if value):
        for permission in (
            "HR_ATTENDANCE_EDIT_APPROVE",
            "HR_REPORTS_VIEW",
            "HR_REPORTS_EXPORT",
        ):
            bind.execute(
                sa.text(
                    "UPDATE user_permission SET is_allowed = 1 "
                    "WHERE user_id = :user_id AND key = :permission"
                ),
                {"user_id": manager_id, "permission": permission},
            )
            bind.execute(
                sa.text(
                    "INSERT INTO user_permission (user_id, key, is_allowed, created_at) "
                    "SELECT :user_id, :permission, 1, CURRENT_TIMESTAMP "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM user_permission "
                    "  WHERE user_id = :user_id AND key = :permission"
                    ")"
                ),
                {"user_id": manager_id, "permission": permission},
            )


def downgrade():
    with op.batch_alter_table("hr_att_special_case") as batch:
        batch.drop_index("ix_hr_att_special_case_final_approved_at")
        batch.drop_index("ix_hr_att_special_case_final_approved_by_id")
        batch.drop_constraint("fk_hr_att_special_case_final_approved_by", type_="foreignkey")
        batch.drop_column("final_approval_note")
        batch.drop_column("final_approved_at")
        batch.drop_column("final_approved_by_id")

    with op.batch_alter_table("hr_leave_request") as batch:
        batch.drop_constraint("uq_hr_leave_attendance_auto_day", type_="unique")
        batch.drop_index("ix_hr_leave_request_replaced_at")
        batch.drop_index("ix_hr_leave_request_replaced_by_leave_request_id")
        batch.drop_index("ix_hr_leave_request_source_attendance_day")
        batch.drop_index("ix_hr_leave_request_source")
        batch.drop_constraint("fk_hr_leave_request_replaced_by", type_="foreignkey")
        batch.drop_column("replacement_reason")
        batch.drop_column("replaced_at")
        batch.drop_column("replaced_by_leave_request_id")
        batch.drop_column("source_attendance_day")
        batch.drop_column("source")
