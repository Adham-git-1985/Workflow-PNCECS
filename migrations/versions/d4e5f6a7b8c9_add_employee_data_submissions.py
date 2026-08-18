"""add staged employee data submissions

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-18

"""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "employee_data_submission",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_user_id", sa.Integer(), nullable=False),
        sa.Column("submitted_by_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="ONLINE"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("schema_version", sa.String(length=50), nullable=False, server_default="EMP-DATA-FORM/V1.1"),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("apply_summary_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["employee_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["submitted_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"]),
        sa.UniqueConstraint(
            "employee_user_id",
            "payload_sha256",
            name="uq_employee_data_submission_employee_hash",
        ),
        sa.CheckConstraint(
            "source IN ('ONLINE','OFFLINE')",
            name="ck_employee_data_submission_source",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','APPLIED','REJECTED')",
            name="ck_employee_data_submission_status",
        ),
    )
    op.create_index("ix_employee_data_submission_employee_user_id", "employee_data_submission", ["employee_user_id"])
    op.create_index("ix_employee_data_submission_submitted_by_id", "employee_data_submission", ["submitted_by_id"])
    op.create_index("ix_employee_data_submission_source", "employee_data_submission", ["source"])
    op.create_index("ix_employee_data_submission_status", "employee_data_submission", ["status"])
    op.create_index("ix_employee_data_submission_payload_sha256", "employee_data_submission", ["payload_sha256"])
    op.create_index("ix_employee_data_submission_submitted_at", "employee_data_submission", ["submitted_at"])
    op.create_index("ix_employee_data_submission_reviewed_by_id", "employee_data_submission", ["reviewed_by_id"])


def downgrade():
    op.drop_index("ix_employee_data_submission_reviewed_by_id", table_name="employee_data_submission")
    op.drop_index("ix_employee_data_submission_submitted_at", table_name="employee_data_submission")
    op.drop_index("ix_employee_data_submission_payload_sha256", table_name="employee_data_submission")
    op.drop_index("ix_employee_data_submission_status", table_name="employee_data_submission")
    op.drop_index("ix_employee_data_submission_source", table_name="employee_data_submission")
    op.drop_index("ix_employee_data_submission_submitted_by_id", table_name="employee_data_submission")
    op.drop_index("ix_employee_data_submission_employee_user_id", table_name="employee_data_submission")
    op.drop_table("employee_data_submission")
