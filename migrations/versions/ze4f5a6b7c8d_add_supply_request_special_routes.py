"""add special materials-request routes

Revision ID: ze4f5a6b7c8d
Revises: zd3e4f5a6b7
Create Date: 2026-10-04 22:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "ze4f5a6b7c8d"
down_revision = "zd3e4f5a6b7"
branch_labels = None
depends_on = None


def _column_names(table_name):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name):
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade():
    if "request_route" not in _column_names("inv_item_category"):
        op.add_column(
            "inv_item_category",
            sa.Column(
                "request_route",
                sa.String(length=30),
                nullable=False,
                server_default=sa.text("'AUTO'"),
            ),
        )
    if "route_type" not in _column_names("inv_employee_request"):
        op.add_column(
            "inv_employee_request",
            sa.Column(
                "route_type",
                sa.String(length=30),
                nullable=False,
                server_default=sa.text("'NORMAL'"),
            ),
        )
    if "ix_inv_item_category_request_route" not in _index_names("inv_item_category"):
        op.create_index(
            "ix_inv_item_category_request_route",
            "inv_item_category",
            ["request_route"],
            unique=False,
        )
    if "ix_inv_employee_request_route_type" not in _index_names("inv_employee_request"):
        op.create_index(
            "ix_inv_employee_request_route_type",
            "inv_employee_request",
            ["route_type"],
            unique=False,
        )


def downgrade():
    if "ix_inv_employee_request_route_type" in _index_names("inv_employee_request"):
        op.drop_index("ix_inv_employee_request_route_type", table_name="inv_employee_request")
    if "ix_inv_item_category_request_route" in _index_names("inv_item_category"):
        op.drop_index("ix_inv_item_category_request_route", table_name="inv_item_category")
    if "route_type" in _column_names("inv_employee_request"):
        op.drop_column("inv_employee_request", "route_type")
    if "request_route" in _column_names("inv_item_category"):
        op.drop_column("inv_item_category", "request_route")
