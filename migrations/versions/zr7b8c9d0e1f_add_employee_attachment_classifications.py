"""add independent classifications for employee attachments

Revision ID: zr7b8c9d0e1f
Revises: zq6a7b8c9d0e
"""

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "zr7b8c9d0e1f"
down_revision = "zq6a7b8c9d0e"
branch_labels = None
depends_on = None


TABLE = "employee_attachment"
COLUMN = "attachment_classification_lookup_id"
INDEX = "ix_employee_attachment_classification_lookup_id"


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    bind = op.get_bind()
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    if TABLE in tables:
        columns = {column["name"] for column in inspector.get_columns(TABLE)}
        if COLUMN not in columns:
            op.add_column(
                TABLE,
                sa.Column(COLUMN, sa.Integer(), nullable=True),
            )

        indexes = {index["name"] for index in inspector.get_indexes(TABLE)}
        if INDEX not in indexes:
            op.create_index(INDEX, TABLE, [COLUMN], unique=False)

    # Seed useful starting groups once.  They remain ordinary HR lookup rows,
    # so administrators can rename, disable, or add groups from the UI.
    if "hr_lookup_item" not in tables:
        return

    lookup = sa.table(
        "hr_lookup_item",
        sa.column("category", sa.String(80)),
        sa.column("code", sa.String(80)),
        sa.column("name_ar", sa.String(255)),
        sa.column("name_en", sa.String(255)),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    now = datetime.utcnow()
    for code, name_ar, name_en, sort_order in (
        ("PERSONAL_DATA", "بيانات شخصية", "Personal Data", 10),
        ("WORK_DATA", "بيانات العمل", "Work Data", 20),
    ):
        exists = bind.execute(
            sa.select(lookup.c.code).where(
                lookup.c.category == "ATTACH_CLASSIFICATION",
                lookup.c.code == code,
            )
        ).first()
        if not exists:
            bind.execute(
                lookup.insert().values(
                    category="ATTACH_CLASSIFICATION",
                    code=code,
                    name_ar=name_ar,
                    name_en=name_en,
                    sort_order=sort_order,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )


def downgrade():
    bind = op.get_bind()
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
