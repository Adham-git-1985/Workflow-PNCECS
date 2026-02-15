"""add internal messages

Revision ID: ab12cd34ef56
Revises: f1a2b3c4d5e6
Create Date: 2026-01-23

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ab12cd34ef56'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('sender_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('subject', sa.String(length=200), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('target_kind', sa.String(length=20), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
    )
    op.create_index('ix_messages_created', 'messages', ['created_at'], unique=False)
    op.create_index('ix_messages_sender', 'messages', ['sender_id'], unique=False)

    op.create_table(
        'message_recipients',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('message_id', sa.Integer(), sa.ForeignKey('messages.id'), nullable=False),
        sa.Column('recipient_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('is_read', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('read_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_msgrec_user_read', 'message_recipients', ['recipient_user_id', 'is_read'], unique=False)
    op.create_index('ix_msgrec_message', 'message_recipients', ['message_id'], unique=False)


def downgrade():
    op.drop_index('ix_msgrec_message', table_name='message_recipients')
    op.drop_index('ix_msgrec_user_read', table_name='message_recipients')
    op.drop_table('message_recipients')

    op.drop_index('ix_messages_sender', table_name='messages')
    op.drop_index('ix_messages_created', table_name='messages')
    op.drop_table('messages')
