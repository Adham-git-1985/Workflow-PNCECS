"""preserve correspondence secrecy in workflow requests

Revision ID: 8b3c4d5e6f70
Revises: 7a2b3c4d5e6f
Create Date: 2026-08-05 00:30:00

"""
from alembic import op
import sqlalchemy as sa


revision = "8b3c4d5e6f70"
down_revision = "7a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_request", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "confidentiality",
            sa.String(length=20),
            nullable=False,
            server_default="NORMAL",
        ))
        batch_op.add_column(sa.Column("source_corr_kind", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("source_corr_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_workflow_request_confidentiality",
            ["confidentiality"],
            unique=False,
        )
        batch_op.create_index(
            "ix_workflow_request_source_corr_kind",
            ["source_corr_kind"],
            unique=False,
        )
        batch_op.create_index(
            "ix_workflow_request_source_corr_id",
            ["source_corr_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_workflow_request_source_corr",
            ["source_corr_kind", "source_corr_id"],
            unique=False,
        )

    # Backfill routes created before these columns were introduced.  The
    # correspondence attachment retains both its source id and workflow id.
    connection = op.get_bind()
    connection.execute(sa.text(
        "UPDATE workflow_request SET source_corr_kind='IN', "
        "source_corr_id=(SELECT ca.inbound_id FROM corr_attachment ca "
        "WHERE ca.workflow_request_id=workflow_request.id "
        "AND ca.inbound_id IS NOT NULL LIMIT 1) "
        "WHERE source_corr_id IS NULL AND EXISTS (SELECT 1 FROM corr_attachment ca "
        "WHERE ca.workflow_request_id=workflow_request.id AND ca.inbound_id IS NOT NULL)"
    ))
    connection.execute(sa.text(
        "UPDATE workflow_request SET source_corr_kind='OUT', "
        "source_corr_id=(SELECT ca.outbound_id FROM corr_attachment ca "
        "WHERE ca.workflow_request_id=workflow_request.id "
        "AND ca.outbound_id IS NOT NULL LIMIT 1) "
        "WHERE source_corr_id IS NULL AND EXISTS (SELECT 1 FROM corr_attachment ca "
        "WHERE ca.workflow_request_id=workflow_request.id AND ca.outbound_id IS NOT NULL)"
    ))
    connection.execute(sa.text(
        "UPDATE workflow_request SET confidentiality=COALESCE((SELECT ci.confidentiality "
        "FROM corr_inbound ci WHERE source_corr_kind='IN' AND ci.id=source_corr_id), 'NORMAL') "
        "WHERE source_corr_kind='IN'"
    ))
    connection.execute(sa.text(
        "UPDATE workflow_request SET confidentiality=COALESCE((SELECT co.confidentiality "
        "FROM corr_outbound co WHERE source_corr_kind='OUT' AND co.id=source_corr_id), 'NORMAL') "
        "WHERE source_corr_kind='OUT'"
    ))


def downgrade() -> None:
    with op.batch_alter_table("workflow_request", schema=None) as batch_op:
        batch_op.drop_index("ix_workflow_request_source_corr")
        batch_op.drop_index("ix_workflow_request_source_corr_id")
        batch_op.drop_index("ix_workflow_request_source_corr_kind")
        batch_op.drop_index("ix_workflow_request_confidentiality")
        batch_op.drop_column("source_corr_id")
        batch_op.drop_column("source_corr_kind")
        batch_op.drop_column("confidentiality")
