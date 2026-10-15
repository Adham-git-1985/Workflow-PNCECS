"""finalize approved fixed-asset cycles and add inventory list indexes

Revision ID: zn3a4b5c6d7e
Revises: zm2a3b4c5d6e
"""

from alembic import op
import sqlalchemy as sa


revision = "zn3a4b5c6d7e"
down_revision = "zm2a3b4c5d6e"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _tables():
    return set(_inspector().get_table_names())


def _columns(table):
    return {column["name"] for column in _inspector().get_columns(table)} if table in _tables() else set()


def _indexes(table):
    return {index["name"] for index in _inspector().get_indexes(table)} if table in _tables() else set()


def _add_column(table, column, column_type):
    if table in _tables() and column not in _columns(table):
        op.add_column(table, sa.Column(column, column_type, nullable=True))


def _add_index(table, name, columns):
    if table in _tables() and name not in _indexes(table):
        op.create_index(name, table, columns)


def upgrade():
    for name, column_type in (
        ("finalized_at", sa.DateTime()),
        ("finalized_by_id", sa.Integer()),
        ("successor_cycle_id", sa.Integer()),
    ):
        _add_column("inv_fixed_asset_cycle", name, column_type)

    _add_index(
        "inv_fixed_asset_cycle",
        "ix_inv_fixed_asset_cycle_finalized_at",
        ["finalized_at"],
    )
    _add_index(
        "inv_fixed_asset_cycle",
        "ix_inv_fixed_asset_cycle_finalized_by_id",
        ["finalized_by_id"],
    )
    _add_index(
        "inv_fixed_asset_cycle",
        "ix_inv_fixed_asset_cycle_successor_cycle_id",
        ["successor_cycle_id"],
    )
    _add_index(
        "inv_fixed_asset_cycle",
        "ix_inv_fixed_asset_cycle_previous_successor",
        ["previous_cycle_id", "successor_cycle_id"],
    )
    _add_index(
        "inv_employee_request",
        "ix_inv_employee_request_status_stage_created",
        ["status", "approval_stage", "created_at"],
    )


def downgrade():
    for table, name in (
        ("inv_employee_request", "ix_inv_employee_request_status_stage_created"),
        ("inv_fixed_asset_cycle", "ix_inv_fixed_asset_cycle_previous_successor"),
        ("inv_fixed_asset_cycle", "ix_inv_fixed_asset_cycle_successor_cycle_id"),
        ("inv_fixed_asset_cycle", "ix_inv_fixed_asset_cycle_finalized_by_id"),
        ("inv_fixed_asset_cycle", "ix_inv_fixed_asset_cycle_finalized_at"),
    ):
        if name in _indexes(table):
            op.drop_index(name, table_name=table)

    if "inv_fixed_asset_cycle" in _tables():
        existing = _columns("inv_fixed_asset_cycle")
        for column in ("successor_cycle_id", "finalized_by_id", "finalized_at"):
            if column in existing:
                with op.batch_alter_table("inv_fixed_asset_cycle") as batch:
                    batch.drop_column(column)
