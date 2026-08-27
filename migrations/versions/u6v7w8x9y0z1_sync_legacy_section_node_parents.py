"""sync legacy section OrgNode parents after master-data edits

Revision ID: u6v7w8x9y0z1
Revises: t5n6o7p8q9r0
Create Date: 2026-08-27 22:10:00
"""

from alembic import op
import sqlalchemy as sa


revision = "u6v7w8x9y0z1"
down_revision = "t5n6o7p8q9r0"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    sections = connection.execute(
        sa.text(
            "SELECT id, department_id, directorate_id, unit_id "
            "FROM sections"
        )
    ).mappings().all()

    for section in sections:
        if section["department_id"] is not None:
            parent_type = "DEPARTMENT"
            parent_legacy_id = int(section["department_id"])
        elif section["unit_id"] is not None:
            parent_type = "UNIT"
            parent_legacy_id = int(section["unit_id"])
        elif section["directorate_id"] is not None:
            parent_type = "DIRECTORATE"
            parent_legacy_id = int(section["directorate_id"])
        else:
            continue

        parent_node_id = connection.execute(
            sa.text(
                "SELECT id FROM org_nodes "
                "WHERE legacy_type = :legacy_type AND legacy_id = :legacy_id "
                "AND is_active = :is_active ORDER BY id ASC LIMIT 1"
            ),
            {
                "legacy_type": parent_type,
                "legacy_id": parent_legacy_id,
                "is_active": True,
            },
        ).scalar()
        if parent_node_id is None:
            continue

        connection.execute(
            sa.text(
                "UPDATE org_nodes "
                "SET parent_id = :parent_id, updated_at = CURRENT_TIMESTAMP "
                "WHERE legacy_type = 'SECTION' AND legacy_id = :section_id "
                "AND is_active = :is_active"
            ),
            {
                "parent_id": int(parent_node_id),
                "section_id": int(section["id"]),
                "is_active": True,
            },
        )


def downgrade():
    # Previous parent links are not recoverable from the current master data.
    pass
