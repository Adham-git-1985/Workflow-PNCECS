"""Employee inventory reviews and fixed asset custody documents."""
from alembic import op
import sqlalchemy as sa

revision = "k2l3m4n5o6p7"
down_revision = "j1k2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "inv_asset_document" not in tables:
        _create_documents()
    if "inv_asset_document_line" not in tables:
        _create_lines()
    if "inv_asset_document_reservation" not in tables:
        op.create_table("inv_asset_document_reservation",
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("inv_fixed_asset.id"), primary_key=True),
            sa.Column("document_id", sa.Integer(), sa.ForeignKey("inv_asset_document.id"), nullable=False))
        op.create_index("ix_inv_asset_document_reservation_document_id", "inv_asset_document_reservation", ["document_id"])


def _create_documents():
    op.create_table("inv_asset_document",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("inv_fixed_asset_cycle.id")),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime()), sa.Column("issued_at", sa.DateTime()),
        sa.Column("issued_by_id", sa.Integer(), sa.ForeignKey("users.id")), sa.Column("reason", sa.Text()))
    op.create_index("ix_inv_asset_document_cycle_id", "inv_asset_document", ["cycle_id"])
    op.create_index("ix_inv_asset_document_employee_id", "inv_asset_document", ["employee_id"])


def _create_lines():
    op.create_table("inv_asset_document_line",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("inv_asset_document.id"), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("inv_fixed_asset.id"), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False), sa.Column("employee_note", sa.Text()),
        sa.UniqueConstraint("document_id", "asset_id", name="uq_asset_document_line"))
    op.create_index("ix_inv_asset_document_line_document_id", "inv_asset_document_line", ["document_id"])
    op.create_index("ix_inv_asset_document_line_asset_id", "inv_asset_document_line", ["asset_id"])


def downgrade():
    op.drop_table("inv_asset_document_reservation")
    op.drop_table("inv_asset_document_line")
    op.drop_table("inv_asset_document")
