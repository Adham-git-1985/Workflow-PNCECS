"""add correspondence competence fields

Revision ID: 3c8d9e0f1a23
Revises: 2b7c8d9e0f12
Create Date: 2026-06-23 14:20:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3c8d9e0f1a23'
down_revision = '2b7c8d9e0f12'
branch_labels = None
depends_on = None


def _add_competence_columns(table_name: str):
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.add_column(sa.Column('competence_kind', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('competence_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('competence_label', sa.String(length=255), nullable=True))
        batch_op.create_index(f'ix_{table_name}_competence_kind', ['competence_kind'], unique=False)
        batch_op.create_index(f'ix_{table_name}_competence_id', ['competence_id'], unique=False)
        batch_op.create_index(f'ix_{table_name}_competence_label', ['competence_label'], unique=False)


def _drop_competence_columns(table_name: str):
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.drop_index(f'ix_{table_name}_competence_label')
        batch_op.drop_index(f'ix_{table_name}_competence_id')
        batch_op.drop_index(f'ix_{table_name}_competence_kind')
        batch_op.drop_column('competence_label')
        batch_op.drop_column('competence_id')
        batch_op.drop_column('competence_kind')


def upgrade():
    _add_competence_columns('corr_inbound')
    _add_competence_columns('corr_outbound')


def downgrade():
    _drop_competence_columns('corr_outbound')
    _drop_competence_columns('corr_inbound')
