"""add fixed-asset committee custody workflows and official reports

Revision ID: zm2a3b4c5d6e
Revises: zl1f2a3b4c5d
"""

from alembic import op
import sqlalchemy as sa


revision = "zm2a3b4c5d6e"
down_revision = "zl1f2a3b4c5d"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _tables():
    return set(_inspector().get_table_names())


def _columns(table):
    return {column["name"] for column in _inspector().get_columns(table)} if table in _tables() else set()


def _add_column(table, column, column_type):
    if table in _tables() and column not in _columns(table):
        op.add_column(table, sa.Column(column, column_type, nullable=True))


def upgrade():
    _add_column("inv_fixed_asset_cycle", "committee_id", sa.Integer())
    for name, column_type in (
        ("employee_response", sa.String(30)),
        ("employee_response_note", sa.Text()),
        ("response_at", sa.DateTime()),
        ("response_by_id", sa.Integer()),
        ("revision_number", sa.Integer()),
        ("committee_revision_note", sa.Text()),
        ("committee_revised_at", sa.DateTime()),
        ("committee_revised_by_id", sa.Integer()),
        ("rejection_escalated_at", sa.DateTime()),
        ("rejection_escalated_by_id", sa.Integer()),
        ("rejection_escalation_note", sa.Text()),
    ):
        _add_column("inv_asset_document", name, column_type)

    tables = _tables()
    if "inv_asset_document_event" not in tables:
        op.create_table(
            "inv_asset_document_event",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("document_id", sa.Integer(), sa.ForeignKey("inv_asset_document.id"), nullable=False),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_inv_asset_document_event_document_id", "inv_asset_document_event", ["document_id"])
        op.create_index("ix_inv_asset_document_event_action", "inv_asset_document_event", ["action"])
        op.create_index("ix_inv_asset_document_event_actor_id", "inv_asset_document_event", ["actor_id"])
        op.create_index("ix_inv_asset_document_event_created_at", "inv_asset_document_event", ["created_at"])

    if "inv_fixed_asset_committee_report" not in tables:
        op.create_table(
            "inv_fixed_asset_committee_report",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("inv_fixed_asset_cycle.id"), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="DRAFT"),
            sa.Column("introduction", sa.Text(), nullable=True),
            sa.Column("points", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("header_filename", sa.String(255), nullable=True),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("submitted_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("secretary_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("submitted_at", sa.DateTime(), nullable=True),
            sa.Column("decided_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("cycle_id", name="uq_inv_fixed_asset_committee_report_cycle"),
        )
        op.create_index("ix_inv_fixed_asset_committee_report_cycle_id", "inv_fixed_asset_committee_report", ["cycle_id"], unique=True)
        op.create_index("ix_inv_fixed_asset_committee_report_status", "inv_fixed_asset_committee_report", ["status"])
        op.create_index("ix_inv_fixed_asset_committee_report_submitted_at", "inv_fixed_asset_committee_report", ["submitted_at"])

    if "inv_fixed_asset_previous_line" not in tables:
        op.create_table(
            "inv_fixed_asset_previous_line",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("inv_fixed_asset_cycle.id"), nullable=False),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("inv_fixed_asset.id"), nullable=True),
            sa.Column("asset_tag", sa.String(255), nullable=False),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.Column("source", sa.String(20), nullable=False, server_default="MANUAL"),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("cycle_id", "employee_id", "asset_tag", name="uq_inv_fixed_asset_previous_line"),
        )
        op.create_index("ix_inv_fixed_asset_previous_line_cycle_id", "inv_fixed_asset_previous_line", ["cycle_id"])
        op.create_index("ix_inv_fixed_asset_previous_line_employee_id", "inv_fixed_asset_previous_line", ["employee_id"])
        op.create_index("ix_inv_fixed_asset_previous_line_asset_id", "inv_fixed_asset_previous_line", ["asset_id"])
        op.create_index("ix_inv_fixed_asset_previous_line_asset_tag", "inv_fixed_asset_previous_line", ["asset_tag"])
        op.create_index("ix_inv_fixed_asset_previous_employee_cycle", "inv_fixed_asset_previous_line", ["employee_id", "cycle_id"])


def downgrade():
    tables = _tables()
    if "inv_fixed_asset_previous_line" in tables:
        op.drop_table("inv_fixed_asset_previous_line")
    if "inv_fixed_asset_committee_report" in tables:
        op.drop_table("inv_fixed_asset_committee_report")
    if "inv_asset_document_event" in tables:
        op.drop_table("inv_asset_document_event")

    inspector = _inspector()
    if "inv_asset_document" in inspector.get_table_names():
        existing = _columns("inv_asset_document")
        for column in (
            "rejection_escalation_note", "rejection_escalated_by_id", "rejection_escalated_at",
            "committee_revised_by_id", "committee_revised_at", "committee_revision_note",
            "revision_number", "response_by_id", "response_at", "employee_response_note", "employee_response",
        ):
            if column in existing:
                with op.batch_alter_table("inv_asset_document") as batch:
                    batch.drop_column(column)
    if "inv_fixed_asset_cycle" in inspector.get_table_names() and "committee_id" in _columns("inv_fixed_asset_cycle"):
        with op.batch_alter_table("inv_fixed_asset_cycle") as batch:
            batch.drop_column("committee_id")
