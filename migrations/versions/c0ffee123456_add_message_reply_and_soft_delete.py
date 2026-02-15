"""add message reply + soft delete

Revision ID: c0ffee123456
Revises: ab12cd34ef56
Create Date: 2026-01-24

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c0ffee123456'
down_revision = 'ab12cd34ef56'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reply_to_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('sender_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('sender_deleted_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('message_recipients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('message_recipients', schema=None) as batch_op:
        batch_op.drop_column('deleted_at')
        batch_op.drop_column('is_deleted')

    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.drop_column('sender_deleted_at')
        batch_op.drop_column('sender_deleted')
        batch_op.drop_column('reply_to_id')
