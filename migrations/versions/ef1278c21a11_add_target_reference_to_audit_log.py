"""add target reference to audit log

Revision ID: ef1278c21a11
Revises: de837bcb98e3
Create Date: 2026-01-19 20:26:04.740535

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ef1278c21a11'
down_revision = 'de837bcb98e3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('target_type', sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column('target_id', sa.Integer(), nullable=True)
        )


    # ### end Alembic commands ###

    def downgrade():
        with op.batch_alter_table('audit_log', schema=None) as batch_op:
            batch_op.drop_column('target_id')
            batch_op.drop_column('target_type')

    # ### end Alembic commands ###
