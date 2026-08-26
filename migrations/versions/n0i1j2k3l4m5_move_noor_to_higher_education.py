"""move Noor Barghouti to the Higher Education section

Revision ID: n0i1j2k3l4m5
Revises: m9h0i1j2k3l4
Create Date: 2026-08-26 17:00:00

The application database is not stored in Git, so this data migration carries
the approved personnel change to deployed environments.  Stable employee and
organization codes are used instead of environment-specific row IDs.
"""

from alembic import op
import sqlalchemy as sa


revision = "n0i1j2k3l4m5"
down_revision = "m9h0i1j2k3l4"
branch_labels = None
depends_on = None


EMPLOYEE_NO = "187465"
EMPLOYEE_EMAIL = "noor.pncecs@gmail.com"
EMPLOYEE_NAME = "نور نبيل فهمي برغوثي"
SOURCE_NODE_CODE = "SEC_PRE_UNI"
TARGET_NODE_CODE = "SEC_HIGH_ED"
SOURCE_SECTION_CODE = "SEC-PNC-38"
TARGET_SECTION_CODE = "SEC-PNC-39"
ASSIGNMENT_TITLE = "رئيس القسم"


def _scalar(bind, sql, **params):
    return bind.execute(sa.text(sql), params).scalar()


def _employee_user_id(bind):
    inspector = sa.inspect(bind)
    user_id = None

    if inspector.has_table("employee_file"):
        user_id = _scalar(
            bind,
            "SELECT user_id FROM employee_file "
            "WHERE employee_no = :employee_no ORDER BY user_id LIMIT 1",
            employee_no=EMPLOYEE_NO,
        )

    if user_id is None and inspector.has_table("users"):
        user_id = _scalar(
            bind,
            "SELECT id FROM users WHERE email = :email ORDER BY id LIMIT 1",
            email=EMPLOYEE_EMAIL,
        )
    if user_id is None and inspector.has_table("users"):
        user_id = _scalar(
            bind,
            "SELECT id FROM users WHERE name = :name ORDER BY id LIMIT 1",
            name=EMPLOYEE_NAME,
        )

    return user_id


def _node_id(bind, code):
    return _scalar(
        bind,
        "SELECT id FROM org_nodes WHERE code = :code AND is_active = 1 "
        "ORDER BY id LIMIT 1",
        code=code,
    )


def _section_id(bind, code):
    if not sa.inspect(bind).has_table("sections"):
        return None
    return _scalar(
        bind,
        "SELECT id FROM sections WHERE code = :code ORDER BY id LIMIT 1",
        code=code,
    )


def _set_placement(bind, user_id, node_id, section_id):
    inspector = sa.inspect(bind)

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "org_node_id" in user_columns:
        bind.execute(
            sa.text("UPDATE users SET org_node_id = :node_id WHERE id = :user_id"),
            {"node_id": node_id, "user_id": user_id},
        )
    if section_id is not None and "section_id" in user_columns:
        bind.execute(
            sa.text("UPDATE users SET section_id = :section_id WHERE id = :user_id"),
            {"section_id": section_id, "user_id": user_id},
        )

    if inspector.has_table("employee_file"):
        employee_columns = {
            column["name"] for column in inspector.get_columns("employee_file")
        }
        if section_id is not None and "section_id" in employee_columns:
            updated_at = ", updated_at = CURRENT_TIMESTAMP" if "updated_at" in employee_columns else ""
            bind.execute(
                sa.text(
                    "UPDATE employee_file SET section_id = :section_id"
                    f"{updated_at} WHERE user_id = :user_id"
                ),
                {"section_id": section_id, "user_id": user_id},
            )


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required_tables = {
        "users",
        "org_nodes",
        "org_node_assignments",
        "org_node_managers",
    }
    if any(not inspector.has_table(table) for table in required_tables):
        return

    user_id = _employee_user_id(bind)
    if user_id is None:
        # Fresh installations can run migrations before employee import.
        return

    source_node_id = _node_id(bind, SOURCE_NODE_CODE)
    target_node_id = _node_id(bind, TARGET_NODE_CODE)
    if target_node_id is None:
        raise RuntimeError("Active SEC_HIGH_ED organization node was not found")

    source_assignment_id = None
    if source_node_id is not None:
        source_assignment_id = _scalar(
            bind,
            "SELECT id FROM org_node_assignments "
            "WHERE user_id = :user_id AND node_id = :node_id ORDER BY id LIMIT 1",
            user_id=user_id,
            node_id=source_node_id,
        )
    target_assignment_id = _scalar(
        bind,
        "SELECT id FROM org_node_assignments "
        "WHERE user_id = :user_id AND node_id = :node_id ORDER BY id LIMIT 1",
        user_id=user_id,
        node_id=target_node_id,
    )

    if target_assignment_id is not None:
        assignment_id = target_assignment_id
        if source_assignment_id is not None and source_assignment_id != target_assignment_id:
            bind.execute(
                sa.text("DELETE FROM org_node_assignments WHERE id = :assignment_id"),
                {"assignment_id": source_assignment_id},
            )
    elif source_assignment_id is not None:
        assignment_id = source_assignment_id
        bind.execute(
            sa.text(
                "UPDATE org_node_assignments "
                "SET node_id = :node_id WHERE id = :assignment_id"
            ),
            {"node_id": target_node_id, "assignment_id": assignment_id},
        )
    else:
        bind.execute(
            sa.text(
                "INSERT INTO org_node_assignments "
                "(user_id, node_id, title, is_primary, created_at, created_by_id) "
                "VALUES (:user_id, :node_id, :title, 1, CURRENT_TIMESTAMP, NULL)"
            ),
            {"user_id": user_id, "node_id": target_node_id, "title": ASSIGNMENT_TITLE},
        )
        assignment_id = _scalar(
            bind,
            "SELECT id FROM org_node_assignments "
            "WHERE user_id = :user_id AND node_id = :node_id ORDER BY id LIMIT 1",
            user_id=user_id,
            node_id=target_node_id,
        )

    bind.execute(
        sa.text(
            "UPDATE org_node_assignments SET is_primary = 0 "
            "WHERE user_id = :user_id AND id <> :assignment_id"
        ),
        {"user_id": user_id, "assignment_id": assignment_id},
    )
    bind.execute(
        sa.text(
            "UPDATE org_node_assignments "
            "SET title = :title, is_primary = 1 WHERE id = :assignment_id"
        ),
        {"title": ASSIGNMENT_TITLE, "assignment_id": assignment_id},
    )

    manager_row_id = _scalar(
        bind,
        "SELECT id FROM org_node_managers WHERE node_id = :node_id",
        node_id=target_node_id,
    )
    if manager_row_id is None:
        bind.execute(
            sa.text(
                "INSERT INTO org_node_managers "
                "(node_id, manager_user_id, deputy_user_id, updated_at, updated_by_id) "
                "VALUES (:node_id, :user_id, NULL, CURRENT_TIMESTAMP, NULL)"
            ),
            {"node_id": target_node_id, "user_id": user_id},
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE org_node_managers "
                "SET manager_user_id = :user_id, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = :manager_row_id"
            ),
            {"user_id": user_id, "manager_row_id": manager_row_id},
        )

    _set_placement(bind, user_id, target_node_id, _section_id(bind, TARGET_SECTION_CODE))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required_tables = {
        "users",
        "org_nodes",
        "org_node_assignments",
        "org_node_managers",
    }
    if any(not inspector.has_table(table) for table in required_tables):
        return

    user_id = _employee_user_id(bind)
    source_node_id = _node_id(bind, SOURCE_NODE_CODE)
    target_node_id = _node_id(bind, TARGET_NODE_CODE)
    if user_id is None or source_node_id is None or target_node_id is None:
        return

    target_assignment_id = _scalar(
        bind,
        "SELECT id FROM org_node_assignments "
        "WHERE user_id = :user_id AND node_id = :node_id ORDER BY id LIMIT 1",
        user_id=user_id,
        node_id=target_node_id,
    )
    source_assignment_id = _scalar(
        bind,
        "SELECT id FROM org_node_assignments "
        "WHERE user_id = :user_id AND node_id = :node_id ORDER BY id LIMIT 1",
        user_id=user_id,
        node_id=source_node_id,
    )
    if target_assignment_id is not None and source_assignment_id is None:
        bind.execute(
            sa.text(
                "UPDATE org_node_assignments "
                "SET node_id = :node_id, title = NULL, is_primary = 1 "
                "WHERE id = :assignment_id"
            ),
            {"node_id": source_node_id, "assignment_id": target_assignment_id},
        )

    bind.execute(
        sa.text(
            "UPDATE org_node_managers "
            "SET manager_user_id = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE node_id = :node_id AND manager_user_id = :user_id"
        ),
        {"node_id": target_node_id, "user_id": user_id},
    )
    _set_placement(bind, user_id, source_node_id, _section_id(bind, SOURCE_SECTION_CODE))
