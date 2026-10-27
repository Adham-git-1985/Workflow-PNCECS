"""link employee attachment types to their default classifications

Revision ID: zs8c9d0e1f2
Revises: zr7b8c9d0e1f
"""

from alembic import op
import sqlalchemy as sa


revision = "zs8c9d0e1f2"
down_revision = "zr7b8c9d0e1f"
branch_labels = None
depends_on = None


TABLE = "hr_lookup_item"
COLUMN = "attachment_classification_lookup_id"
INDEX = "ix_hr_lookup_item_attachment_classification_lookup_id"


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())
    if TABLE not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    if COLUMN not in columns:
        op.add_column(
            TABLE,
            sa.Column(COLUMN, sa.Integer(), nullable=True),
        )

    indexes = {index["name"] for index in inspector.get_indexes(TABLE)}
    if INDEX not in indexes:
        op.create_index(INDEX, TABLE, [COLUMN], unique=False)


def downgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())
    if TABLE not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    indexes = {index["name"] for index in inspector.get_indexes(TABLE)}
    if INDEX in indexes:
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in columns:
        op.drop_column(TABLE, COLUMN)
