"""add annual leave rollover extension duration

Revision ID: z7a8b9c0d1e2
Revises: z6a7b8c9d0e1
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa


revision = "z7a8b9c0d1e2"
down_revision = "z6a7b8c9d0e1"
branch_labels = None
depends_on = None


_TABLE = "hr_leave_rollover_decision"
_COLUMN = "extension_days"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
