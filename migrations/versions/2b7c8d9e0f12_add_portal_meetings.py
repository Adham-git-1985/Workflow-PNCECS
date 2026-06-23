"""add portal meetings

Revision ID: 2b7c8d9e0f12
Revises: 1d2e3f4a5b6c, b7d8e9a0f123, c0ffee123456
Create Date: 2026-06-23 13:46:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2b7c8d9e0f12'
down_revision = ('1d2e3f4a5b6c', 'b7d8e9a0f123', 'c0ffee123456')
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'portal_meetings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('location', sa.String(length=200), nullable=True),
        sa.Column('start_at', sa.DateTime(), nullable=False),
        sa.Column('end_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='SCHEDULED'),
        sa.Column('reminder_minutes_before', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('reminder_sent_at', sa.DateTime(), nullable=True),
        sa.Column('minutes_text', sa.Text(), nullable=True),
        sa.Column('decisions_text', sa.Text(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
    )
    op.create_index('ix_portal_meetings_start', 'portal_meetings', ['start_at'], unique=False)
    op.create_index('ix_portal_meetings_status_start', 'portal_meetings', ['status', 'start_at'], unique=False)
    op.create_index('ix_portal_meetings_created_by_user_id', 'portal_meetings', ['created_by_user_id'], unique=False)

    op.create_table(
        'portal_meeting_participants',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=30), nullable=False, server_default='ATTENDEE'),
        sa.Column('attendance_status', sa.String(length=20), nullable=False, server_default='INVITED'),
        sa.Column('note', sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['portal_meetings.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.UniqueConstraint('meeting_id', 'user_id', name='uq_portal_meeting_participant'),
    )
    op.create_index('ix_portal_meeting_participants_meeting_id', 'portal_meeting_participants', ['meeting_id'], unique=False)
    op.create_index('ix_portal_meeting_participants_user', 'portal_meeting_participants', ['user_id'], unique=False)

    op.create_table(
        'portal_meeting_agenda_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('owner_user_id', sa.Integer(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('is_done', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.ForeignKeyConstraint(['meeting_id'], ['portal_meetings.id']),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id']),
    )
    op.create_index('ix_portal_meeting_agenda_items_meeting_id', 'portal_meeting_agenda_items', ['meeting_id'], unique=False)
    op.create_index('ix_portal_meeting_agenda_items_owner_user_id', 'portal_meeting_agenda_items', ['owner_user_id'], unique=False)
    op.create_index('ix_portal_meeting_agenda_meeting_order', 'portal_meeting_agenda_items', ['meeting_id', 'sort_order'], unique=False)

    op.create_table(
        'portal_meeting_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('assignee_user_id', sa.Integer(), nullable=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='OPEN'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['portal_meetings.id']),
        sa.ForeignKeyConstraint(['assignee_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
    )
    op.create_index('ix_portal_meeting_tasks_meeting_id', 'portal_meeting_tasks', ['meeting_id'], unique=False)
    op.create_index('ix_portal_meeting_tasks_assignee_user_id', 'portal_meeting_tasks', ['assignee_user_id'], unique=False)
    op.create_index('ix_portal_meeting_tasks_created_by_user_id', 'portal_meeting_tasks', ['created_by_user_id'], unique=False)
    op.create_index('ix_portal_meeting_tasks_status_due', 'portal_meeting_tasks', ['status', 'due_date'], unique=False)
    op.create_index('ix_portal_meeting_tasks_assignee_status', 'portal_meeting_tasks', ['assignee_user_id', 'status'], unique=False)


def downgrade():
    op.drop_index('ix_portal_meeting_tasks_assignee_status', table_name='portal_meeting_tasks')
    op.drop_index('ix_portal_meeting_tasks_status_due', table_name='portal_meeting_tasks')
    op.drop_index('ix_portal_meeting_tasks_created_by_user_id', table_name='portal_meeting_tasks')
    op.drop_index('ix_portal_meeting_tasks_assignee_user_id', table_name='portal_meeting_tasks')
    op.drop_index('ix_portal_meeting_tasks_meeting_id', table_name='portal_meeting_tasks')
    op.drop_table('portal_meeting_tasks')

    op.drop_index('ix_portal_meeting_agenda_meeting_order', table_name='portal_meeting_agenda_items')
    op.drop_index('ix_portal_meeting_agenda_items_owner_user_id', table_name='portal_meeting_agenda_items')
    op.drop_index('ix_portal_meeting_agenda_items_meeting_id', table_name='portal_meeting_agenda_items')
    op.drop_table('portal_meeting_agenda_items')

    op.drop_index('ix_portal_meeting_participants_user', table_name='portal_meeting_participants')
    op.drop_index('ix_portal_meeting_participants_meeting_id', table_name='portal_meeting_participants')
    op.drop_table('portal_meeting_participants')

    op.drop_index('ix_portal_meetings_created_by_user_id', table_name='portal_meetings')
    op.drop_index('ix_portal_meetings_status_start', table_name='portal_meetings')
    op.drop_index('ix_portal_meetings_start', table_name='portal_meetings')
    op.drop_table('portal_meetings')
