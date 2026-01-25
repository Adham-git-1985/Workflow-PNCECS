"""add shared_by to file_permission

Revision ID: b7d8e9a0f123
Revises: f1a2b3c4d5e6
Create Date: 2026-01-24

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7d8e9a0f123'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('file_permission', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shared_by', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_file_permission_shared_by_users', 'users', ['shared_by'], ['id'])


def downgrade():
    with op.batch_alter_table('file_permission', schema=None) as batch_op:
        batch_op.drop_constraint('fk_file_permission_shared_by_users', type_='foreignkey')
        batch_op.drop_column('shared_by')
