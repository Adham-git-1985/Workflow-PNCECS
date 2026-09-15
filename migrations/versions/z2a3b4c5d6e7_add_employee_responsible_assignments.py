"""add explicit employee responsible assignments

Revision ID: z2a3b4c5d6e7
Revises: m5n6o7p8q9r0
Create Date: 2026-09-16
"""

from alembic import op
import sqlalchemy as sa


revision = "z2a3b4c5d6e7"
down_revision = "m5n6o7p8q9r0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("employee_followup_reports"):
        followup_columns = {
            column["name"]
            for column in inspector.get_columns("employee_followup_reports")
        }
        if "manager_user_ids" not in followup_columns:
            with op.batch_alter_table("employee_followup_reports") as batch_op:
                batch_op.add_column(sa.Column("manager_user_ids", sa.Text(), nullable=True))

    # Movement requests keep a frozen copy of all selected employee
    # responsibles, while the legacy manager_user_id remains the first choice
    # for backward-compatible screens.
    if inspector.has_table("transport_permit"):
        transport_columns = {
            column["name"]
            for column in inspector.get_columns("transport_permit")
        }
        if "manager_user_ids" not in transport_columns:
            with op.batch_alter_table("transport_permit") as batch_op:
                batch_op.add_column(sa.Column("manager_user_ids", sa.Text(), nullable=True))

    if not inspector.has_table("employee_responsible_assignment"):
        op.create_table(
            "employee_responsible_assignment",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("employee_user_id", sa.Integer(), nullable=False),
            sa.Column("responsible_user_id", sa.Integer(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("created_by_id", sa.Integer(), nullable=True),
            sa.Column("updated_by_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["employee_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["responsible_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "employee_user_id",
                "responsible_user_id",
                name="uq_employee_responsible_assignment_pair",
            ),
            sa.CheckConstraint(
                "employee_user_id <> responsible_user_id",
                name="ck_employee_responsible_not_self",
            ),
        )

    index_names = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("employee_responsible_assignment")
    }
    for name, columns in (
        (
            "ix_employee_responsible_assignment_employee_user_id",
            ["employee_user_id"],
        ),
        (
            "ix_employee_responsible_assignment_responsible_user_id",
            ["responsible_user_id"],
        ),
        ("ix_employee_responsible_assignment_is_active", ["is_active"]),
        (
            "ix_employee_responsible_active_employee",
            ["employee_user_id", "is_active", "id"],
        ),
    ):
        if name not in index_names:
            op.create_index(
                name,
                "employee_responsible_assignment",
                columns,
                unique=False,
            )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("employee_responsible_assignment"):
        index_names = {
            index["name"]
            for index in inspector.get_indexes("employee_responsible_assignment")
        }
        for name in (
            "ix_employee_responsible_active_employee",
            "ix_employee_responsible_assignment_is_active",
            "ix_employee_responsible_assignment_responsible_user_id",
            "ix_employee_responsible_assignment_employee_user_id",
        ):
            if name in index_names:
                op.drop_index(name, table_name="employee_responsible_assignment")
        op.drop_table("employee_responsible_assignment")

    if inspector.has_table("transport_permit"):
        columns = {column["name"] for column in inspector.get_columns("transport_permit")}
        if "manager_user_ids" in columns:
            with op.batch_alter_table("transport_permit") as batch_op:
                batch_op.drop_column("manager_user_ids")
    if inspector.has_table("employee_followup_reports"):
        columns = {
            column["name"]
            for column in inspector.get_columns("employee_followup_reports")
        }
        if "manager_user_ids" in columns:
            with op.batch_alter_table("employee_followup_reports") as batch_op:
                batch_op.drop_column("manager_user_ids")
