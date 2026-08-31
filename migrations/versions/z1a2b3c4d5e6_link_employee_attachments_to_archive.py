"""link employee attachments to their private archive files

Revision ID: z1a2b3c4d5e6
Revises: y0z1a2b3c4d
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa


revision = "z1a2b3c4d5e6"
down_revision = "y0z1a2b3c4d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "employee_attachment",
        sa.Column("archived_file_id", sa.Integer(), nullable=True),
    )
    try:
        op.create_foreign_key(
            "fk_employee_attachment_archived_file",
            "employee_attachment",
            "archived_file",
            ["archived_file_id"],
            ["id"],
        )
    except Exception:
        # SQLite cannot add a foreign-key constraint to an existing table.
        pass
    op.create_index(
        "ix_employee_attachment_archived_file_id",
        "employee_attachment",
        ["archived_file_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_employee_attachment_archived_file_id", table_name="employee_attachment")
    try:
        op.drop_constraint(
            "fk_employee_attachment_archived_file",
            "employee_attachment",
            type_="foreignkey",
        )
    except Exception:
        pass
    op.drop_column("employee_attachment", "archived_file_id")
