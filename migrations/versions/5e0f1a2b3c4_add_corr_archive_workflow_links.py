"""add correspondence archive and workflow links

Revision ID: 5e0f1a2b3c4
Revises: 4d9e0f1a2b34
Create Date: 2026-06-24 11:40:00

"""
from alembic import op
import sqlalchemy as sa


revision = "5e0f1a2b3c4"
down_revision = "4d9e0f1a2b34"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("corr_attachment", schema=None) as batch_op:
        batch_op.add_column(sa.Column("archive_file_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("workflow_request_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_corr_attachment_archive_file", ["archive_file_id"], unique=False)
        batch_op.create_index("ix_corr_attachment_workflow_request", ["workflow_request_id"], unique=False)
        try:
            batch_op.create_foreign_key(
                "fk_corr_attachment_archive_file",
                "archived_file",
                ["archive_file_id"],
                ["id"],
            )
            batch_op.create_foreign_key(
                "fk_corr_attachment_workflow_request",
                "workflow_request",
                ["workflow_request_id"],
                ["id"],
            )
        except Exception:
            pass


def downgrade():
    with op.batch_alter_table("corr_attachment", schema=None) as batch_op:
        batch_op.drop_index("ix_corr_attachment_workflow_request")
        batch_op.drop_index("ix_corr_attachment_archive_file")
        batch_op.drop_column("workflow_request_id")
        batch_op.drop_column("archive_file_id")
