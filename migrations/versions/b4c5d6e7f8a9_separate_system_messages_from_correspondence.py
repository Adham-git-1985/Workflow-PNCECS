"""separate system-generated entries from internal correspondence

Revision ID: b4c5d6e7f8a9
Revises: a2b3c4d5e6f, a3b4c5d6e7f8
Create Date: 2026-09-01
"""

from alembic import op
import sqlalchemy as sa


revision = "b4c5d6e7f8a9"
down_revision = ("a2b3c4d5e6f", "a3b4c5d6e7f8")
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(
            sa.Column("is_system_generated", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.create_index("ix_messages_is_system_generated", ["is_system_generated"], unique=False)

    # Keep historical correspondence visible while removing known automated
    # alerts that were previously written into the same inbox.
    op.execute(
        sa.text(
            """
            UPDATE messages
            SET is_system_generated = 1
            WHERE subject LIKE 'تعميم إداري:%'
               OR subject LIKE 'تعميم إداري مستعجل:%'
               OR subject LIKE 'دعوة اجتماع:%'
               OR subject LIKE 'تذكير اجتماع:%'
               OR subject LIKE 'رد على دعوة اجتماع:%'
               OR subject LIKE 'إلغاء اجتماع:%'
               OR subject LIKE 'محضر اجتماع:%'
               OR subject LIKE 'قسيمة راتب %'
               OR subject LIKE 'تمت إضافتك إلى مسار الطلب #%'
               OR subject LIKE 'تنبيه تصعيد:%'
               OR subject LIKE 'طلب حركة #% بانتظار الإجراء'
               OR subject LIKE 'طلب مواد #%'
            """
        )
    )


def downgrade():
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_index("ix_messages_is_system_generated")
        batch_op.drop_column("is_system_generated")
