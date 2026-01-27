"""add request types and workflow routing rules

Revision ID: f1a2b3c4d5e6
Revises: ef1278c21a11
Create Date: 2026-01-23 12:48:49

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'ef1278c21a11'
branch_labels = None
depends_on = None


def upgrade():
    # request_types
    op.create_table(
        'request_types',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(length=50), nullable=False),
        sa.Column('name_ar', sa.String(length=200), nullable=False),
        sa.Column('name_en', sa.String(length=200), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.UniqueConstraint('code', name='uq_request_types_code'),
    )
    op.create_index('ix_request_types_code', 'request_types', ['code'], unique=True)

    # workflow_routing_rules
    op.create_table(
        'workflow_routing_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('request_type_id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=True),
        sa.Column('directorate_id', sa.Integer(), nullable=True),
        sa.Column('department_id', sa.Integer(), nullable=True),
        sa.Column('template_id', sa.Integer(), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False, server_default=sa.text('100')),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['request_type_id'], ['request_types.id']),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['directorate_id'], ['directorates.id']),
        sa.ForeignKeyConstraint(['department_id'], ['departments.id']),
        sa.ForeignKeyConstraint(['template_id'], ['workflow_templates.id']),
    )
    op.create_index('ix_workflow_routing_rules_request_type', 'workflow_routing_rules', ['request_type_id'], unique=False)
    op.create_index('ix_workflow_routing_rules_org', 'workflow_routing_rules', ['organization_id'], unique=False)
    op.create_index('ix_workflow_routing_rules_dir', 'workflow_routing_rules', ['directorate_id'], unique=False)
    op.create_index('ix_workflow_routing_rules_dept', 'workflow_routing_rules', ['department_id'], unique=False)
    op.create_index('ix_workflow_routing_rules_template', 'workflow_routing_rules', ['template_id'], unique=False)
    op.create_index(
        'ix_routing_rule_match',
        'workflow_routing_rules',
        ['request_type_id', 'organization_id', 'directorate_id', 'department_id', 'is_active'],
        unique=False
    )

    # add request_type_id to workflow_request
    with op.batch_alter_table('workflow_request', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_type_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_workflow_request_request_type', ['request_type_id'], unique=False)
        batch_op.create_foreign_key('fk_workflow_request_request_type', 'request_types', ['request_type_id'], ['id'])


def downgrade():
    with op.batch_alter_table('workflow_request', schema=None) as batch_op:
        batch_op.drop_constraint('fk_workflow_request_request_type', type_='foreignkey')
        batch_op.drop_index('ix_workflow_request_request_type')
        batch_op.drop_column('request_type_id')

    op.drop_index('ix_routing_rule_match', table_name='workflow_routing_rules')
    op.drop_index('ix_workflow_routing_rules_template', table_name='workflow_routing_rules')
    op.drop_index('ix_workflow_routing_rules_dept', table_name='workflow_routing_rules')
    op.drop_index('ix_workflow_routing_rules_dir', table_name='workflow_routing_rules')
    op.drop_index('ix_workflow_routing_rules_org', table_name='workflow_routing_rules')
    op.drop_index('ix_workflow_routing_rules_request_type', table_name='workflow_routing_rules')
    op.drop_table('workflow_routing_rules')

    op.drop_index('ix_request_types_code', table_name='request_types')
    op.drop_table('request_types')
