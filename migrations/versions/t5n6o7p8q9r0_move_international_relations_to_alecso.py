"""move international relations section from ICESCO to ALECSO

Revision ID: t5n6o7p8q9r0
Revises: s4m5n6o7p8q9
Create Date: 2026-08-27 20:45:00
"""

from alembic import op
import sqlalchemy as sa


revision = "t5n6o7p8q9r0"
down_revision = "s4m5n6o7p8q9"
branch_labels = None
depends_on = None


def _move_to(parent_code: str, version: str) -> None:
    connection = op.get_bind()
    target_parent_id = connection.execute(
        sa.text(
            "SELECT id FROM org_nodes "
            "WHERE code = :code AND is_active = 1 ORDER BY id LIMIT 1"
        ),
        {"code": parent_code},
    ).scalar()
    if target_parent_id is None:
        return

    connection.execute(
        sa.text(
            "UPDATE org_nodes SET parent_id = :parent_id, updated_at = CURRENT_TIMESTAMP "
            "WHERE code = 'SEC_ICESCO_REL' AND is_active = 1"
        ),
        {"parent_id": int(target_parent_id)},
    )
    connection.execute(
        sa.text(
            "UPDATE system_setting SET value = :version "
            "WHERE key = 'ORG_APPROVED_STRUCTURE_VERSION'"
        ),
        {"version": version},
    )


def upgrade():
    _move_to("DEP_ALECSO", "2023-05-08:v2")


def downgrade():
    _move_to("DEP_ICESCO", "2023-05-08:v1")
