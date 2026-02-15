"""add publish fields to employee_attachment payslips

Revision ID: 1d2e3f4a5b6c
Revises: f1a2b3c4d5e6
Create Date: 2026-02-06

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '1d2e3f4a5b6c'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    # Draft/Publish controls for payslips
    op.add_column('employee_attachment', sa.Column('is_published', sa.Boolean(), nullable=False, server_default=sa.text('1')))
    op.add_column('employee_attachment', sa.Column('published_at', sa.DateTime(), nullable=True))
    op.add_column('employee_attachment', sa.Column('published_by_id', sa.Integer(), nullable=True))

    # Optional FK to users
    try:
        op.create_foreign_key(
            'fk_employee_attachment_published_by',
            'employee_attachment', 'users',
            ['published_by_id'], ['id'],
        )
    except Exception:
        # SQLite might not support adding FK in-place depending on settings.
        pass

    # Helpful index for filtering
    try:
        op.create_index(
            'ix_employee_attachment_payslip_pub',
            'employee_attachment',
            ['attachment_type', 'payslip_year', 'payslip_month', 'is_published'],
            unique=False,
        )
    except Exception:
        pass


def downgrade():
    try:
        op.drop_index('ix_employee_attachment_payslip_pub', table_name='employee_attachment')
    except Exception:
        pass

    try:
        op.drop_constraint('fk_employee_attachment_published_by', 'employee_attachment', type_='foreignkey')
    except Exception:
        pass

    op.drop_column('employee_attachment', 'published_by_id')
    op.drop_column('employee_attachment', 'published_at')
    op.drop_column('employee_attachment', 'is_published')
