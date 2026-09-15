"""backfill automatic inventory identifiers

Revision ID: l4m5n6o7p8q9
Revises: k3c4d5e6f7g8
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa


revision = "l4m5n6o7p8q9"
down_revision = "k3c4d5e6f7g8"
branch_labels = None
depends_on = None


def _fill_master_codes(connection, table_name, prefix):
    rows = connection.execute(
        sa.text(f"SELECT id, code FROM {table_name}")
    ).mappings().all()
    for row in rows:
        if str(row["code"] or "").strip():
            continue
        connection.execute(
            sa.text(f"UPDATE {table_name} SET code = :code WHERE id = :id"),
            {"code": f"{prefix}-{int(row['id']):04d}", "id": int(row["id"])},
        )


def _fill_voucher_numbers(connection, table_name, prefix):
    columns = "id, voucher_date, voucher_no"
    if table_name == "inv_issue_voucher":
        columns += ", issue_kind"
    rows = connection.execute(
        sa.text(f"SELECT {columns} FROM {table_name}")
    ).mappings().all()
    for row in rows:
        if str(row["voucher_no"] or "").strip():
            continue
        row_prefix = prefix
        if table_name == "inv_issue_voucher":
            kind = str(row.get("issue_kind") or "").upper()
            row_prefix = "TRF" if kind == "WAREHOUSE" else ("EMP" if kind == "EMPLOYEE" else "ISS")
        year = str(row["voucher_date"] or "")[:4]
        if not year.isdigit() or len(year) != 4:
            year = "0000"
        connection.execute(
            sa.text(f"UPDATE {table_name} SET voucher_no = :voucher_no WHERE id = :id"),
            {
                "voucher_no": f"{row_prefix}-{year}-{int(row['id']):06d}",
                "id": int(row["id"]),
            },
        )


def upgrade():
    connection = op.get_bind()
    _fill_master_codes(connection, "inv_warehouse", "WH")
    _fill_master_codes(connection, "inv_room", "RM")

    for table_name, prefix in (
        ("inv_issue_voucher", "ISS"),
        ("inv_inbound_voucher", "IN"),
        ("inv_scrap_voucher", "SCR"),
        ("inv_return_voucher", "RET"),
        ("inv_stocktake_voucher", "STK"),
        ("inv_custody_voucher", "CUS"),
    ):
        _fill_voucher_numbers(connection, table_name, prefix)


def downgrade():
    # Identifier backfills are intentionally retained on downgrade so that
    # existing inventory records do not lose their references.
    pass
