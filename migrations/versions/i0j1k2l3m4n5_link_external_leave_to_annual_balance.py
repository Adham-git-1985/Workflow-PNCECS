"""link request types to the balance they consume

Revision ID: i0j1k2l3m4n5
Revises: h9i0j1k2l3m4
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa


revision = "i0j1k2l3m4n5"
down_revision = "h9i0j1k2l3m4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("hr_leave_type")}
    foreign_keys = {foreign_key.get("name") for foreign_key in inspector.get_foreign_keys("hr_leave_type")}
    indexes = {index.get("name") for index in inspector.get_indexes("hr_leave_type")}

    with op.batch_alter_table("hr_leave_type") as batch_op:
        if "balance_source_leave_type_id" not in columns:
            batch_op.add_column(sa.Column("balance_source_leave_type_id", sa.Integer(), nullable=True))
        if "fk_hr_leave_type_balance_source" not in foreign_keys:
            batch_op.create_foreign_key(
                "fk_hr_leave_type_balance_source",
                "hr_leave_type",
                ["balance_source_leave_type_id"],
                ["id"],
            )
        if "ix_hr_leave_type_balance_source_leave_type_id" not in indexes:
            batch_op.create_index(
                "ix_hr_leave_type_balance_source_leave_type_id",
                ["balance_source_leave_type_id"],
                unique=False,
            )

    op.execute(sa.text(
        "UPDATE hr_leave_type "
        "SET balance_source_leave_type_id = ("
        "SELECT annual.id FROM hr_leave_type AS annual "
        "WHERE UPPER(annual.code) IN ('A', 'ANNUAL', 'ANNUAL_LEAVE', 'PERSONAL') "
        "ORDER BY CASE UPPER(annual.code) "
        "WHEN 'A' THEN 1 WHEN 'ANNUAL' THEN 2 WHEN 'ANNUAL_LEAVE' THEN 3 ELSE 4 END "
        "LIMIT 1"
        "), deduct_from_balance = 1 "
        "WHERE balance_source_leave_type_id IS NULL "
        "AND UPPER(code) IN ('O', 'EXTERNAL', 'EXTERNAL_LEAVE', 'OUTSIDE') "
        "AND EXISTS ("
        "SELECT 1 FROM hr_leave_type AS annual "
        "WHERE UPPER(annual.code) IN ('A', 'ANNUAL', 'ANNUAL_LEAVE', 'PERSONAL')"
        ")"
    ))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("hr_leave_type")}
    if "balance_source_leave_type_id" not in columns:
        return
    foreign_keys = {foreign_key.get("name") for foreign_key in inspector.get_foreign_keys("hr_leave_type")}
    indexes = {index.get("name") for index in inspector.get_indexes("hr_leave_type")}

    with op.batch_alter_table("hr_leave_type") as batch_op:
        if "ix_hr_leave_type_balance_source_leave_type_id" in indexes:
            batch_op.drop_index("ix_hr_leave_type_balance_source_leave_type_id")
        if "fk_hr_leave_type_balance_source" in foreign_keys:
            batch_op.drop_constraint("fk_hr_leave_type_balance_source", type_="foreignkey")
        batch_op.drop_column("balance_source_leave_type_id")
