"""seed employee card and work-decision attachment types

Revision ID: zu0a1b2c3d4e
Revises: zt9d0e1f2a3b
"""

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "zu0a1b2c3d4e"
down_revision = "zt9d0e1f2a3b"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _ensure_lookup(bind, *, code, name_ar, name_en, sort_order, classification_id):
    row = bind.execute(
        sa.text(
            "SELECT id, attachment_classification_lookup_id "
            "FROM hr_lookup_item WHERE category = 'ATTACH_TYPE' AND code = :code"
        ),
        {"code": code},
    ).first()
    now = datetime.utcnow()
    if row:
        if row.attachment_classification_lookup_id is None and classification_id:
            bind.execute(
                sa.text(
                    "UPDATE hr_lookup_item "
                    "SET attachment_classification_lookup_id = :classification_id, updated_at = :updated_at "
                    "WHERE id = :id"
                ),
                {
                    "classification_id": classification_id,
                    "updated_at": now,
                    "id": row.id,
                },
            )
        return row.id

    bind.execute(
        sa.text(
            "INSERT INTO hr_lookup_item "
            "(category, code, name_ar, name_en, sort_order, is_active, "
            "created_at, updated_at, attachment_classification_lookup_id) "
            "VALUES ('ATTACH_TYPE', :code, :name_ar, :name_en, :sort_order, 1, "
            ":created_at, :updated_at, :classification_id)"
        ),
        {
            "code": code,
            "name_ar": name_ar,
            "name_en": name_en,
            "sort_order": sort_order,
            "created_at": now,
            "updated_at": now,
            "classification_id": classification_id,
        },
    )
    return bind.execute(
        sa.text("SELECT id FROM hr_lookup_item WHERE category = 'ATTACH_TYPE' AND code = :code"),
        {"code": code},
    ).scalar_one()


def upgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())
    if "hr_lookup_item" not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns("hr_lookup_item")}
    if "attachment_classification_lookup_id" not in columns:
        op.add_column(
            "hr_lookup_item",
            sa.Column("attachment_classification_lookup_id", sa.Integer(), nullable=True),
        )

    bind = op.get_bind()
    classification_id = bind.execute(
        sa.text(
            "SELECT id FROM hr_lookup_item "
            "WHERE category = 'ATTACH_CLASSIFICATION' AND code = 'PERSONAL_DATA'"
        )
    ).scalar()
    work_classification_id = bind.execute(
        sa.text(
            "SELECT id FROM hr_lookup_item "
            "WHERE category = 'ATTACH_CLASSIFICATION' AND code = 'WORK_DATA'"
        )
    ).scalar()

    for code, name_ar, name_en, sort_order, default_classification_id in (
        (
            "EMPLOYEE_CARD_FRONT",
            "بطاقة الموظف - الوجه الأمامي",
            "Employee Card - Front",
            5,
            classification_id,
        ),
        (
            "EMPLOYEE_CARD_BACK",
            "بطاقة الموظف - الوجه الخلفي",
            "Employee Card - Back",
            6,
            classification_id,
        ),
        (
            "ASSIGNMENT",
            "قرار/تكليف عمل",
            "Work Decision/Assignment",
            45,
            work_classification_id,
        ),
    ):
        _ensure_lookup(
            bind,
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            sort_order=sort_order,
            classification_id=default_classification_id,
        )


def downgrade():
    bind = op.get_bind()
    if "hr_lookup_item" not in set(_inspector().get_table_names()):
        return
    bind.execute(
        sa.text(
            "DELETE FROM hr_lookup_item "
            "WHERE category = 'ATTACH_TYPE' "
            "AND code IN ('EMPLOYEE_CARD_FRONT', 'EMPLOYEE_CARD_BACK', 'ASSIGNMENT')"
        )
    )
