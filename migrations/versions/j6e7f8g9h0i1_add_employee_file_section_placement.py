"""add employee file section placement

Revision ID: j6e7f8g9h0i1
Revises: i5d6e7f8g9h0
Create Date: 2026-08-21
"""

from alembic import op
import sqlalchemy as sa


revision = "j6e7f8g9h0i1"
down_revision = "i5d6e7f8g9h0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("employee_file") as batch_op:
        batch_op.add_column(sa.Column("section_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_employee_file_section_id_sections",
            "sections",
            ["section_id"],
            ["id"],
        )
        batch_op.create_index("ix_employee_file_section_id", ["section_id"], unique=False)


def downgrade():
    with op.batch_alter_table("employee_file") as batch_op:
        batch_op.drop_index("ix_employee_file_section_id")
        batch_op.drop_constraint("fk_employee_file_section_id_sections", type_="foreignkey")
        batch_op.drop_column("section_id")
