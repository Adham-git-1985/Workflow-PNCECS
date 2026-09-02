"""seed maternity leave type for the regular HR leave workflow

Revision ID: d5e6f7a8b9c
Revises: c4d5e6f7a8b
Create Date: 2026-09-02
"""

from alembic import op
import sqlalchemy as sa


revision = "d5e6f7a8b9c"
down_revision = "c4d5e6f7a8b"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().execute(
        sa.text(
            "INSERT INTO hr_leave_type ("
            "code, name_ar, name_en, requires_approval, max_days, "
            "default_balance_days, exception_max_days, exception_requires_hr, "
            "exception_requires_note, requires_documents, is_external, is_active, created_at"
            ") "
            "SELECT 'M', 'إجازة أمومة', 'Maternity leave', 1, NULL, "
            "NULL, NULL, 1, 0, 0, 0, 1, CURRENT_TIMESTAMP "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM hr_leave_type WHERE upper(code) = 'M'"
            ")"
        )
    )


def downgrade():
    op.get_bind().execute(
        sa.text(
            "DELETE FROM hr_leave_type "
            "WHERE upper(code) = 'M' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM hr_leave_request "
            "WHERE hr_leave_request.leave_type_id = hr_leave_type.id"
            ")"
        )
    )
