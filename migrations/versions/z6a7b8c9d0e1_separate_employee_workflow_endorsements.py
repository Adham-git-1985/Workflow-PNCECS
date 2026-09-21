"""separate employee workflow endorsements from Secretary-General endorsements

Revision ID: z6a7b8c9d0e1
Revises: z5a6b7c8d9e0
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa


revision = "z6a7b8c9d0e1"
down_revision = "z5a6b7c8d9e0"
branch_labels = None
depends_on = None


_TABLE = "workflow_quick_endorsements"
_AUDIENCE_COLUMN = "audience"
_SCOPED_UNIQUE = "uq_workflow_quick_endorsements_audience_text"
_SCOPED_INDEX = "ix_workflow_quick_endorsements_audience_active_order"


def _unique_column_sets(inspector):
    return {
        tuple(sorted(constraint.get("column_names") or ()))
        for constraint in inspector.get_unique_constraints(_TABLE)
    }


def _sqlite_requires_rebuild(bind, inspector):
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _AUDIENCE_COLUMN not in columns:
        return True

    if tuple(sorted((_AUDIENCE_COLUMN, "text"))) not in _unique_column_sets(inspector):
        return True

    # The previous model used ``text`` as a global unique field.  It must be
    # removed so the same wording can independently exist in both lists.
    for index in inspector.get_indexes(_TABLE):
        if index.get("unique") and tuple(index.get("column_names") or ()) == ("text",):
            return True
    for constraint in inspector.get_unique_constraints(_TABLE):
        if tuple(constraint.get("column_names") or ()) == ("text",):
            return True
    return False


def _rebuild_sqlite_table(bind, inspector):
    """Rebuild SQLite table to replace its old anonymous UNIQUE(text)."""
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    audience_value = (
        "COALESCE(NULLIF(TRIM(audience), ''), 'SECRETARY')"
        if _AUDIENCE_COLUMN in columns
        else "'SECRETARY'"
    )
    temporary_table = "workflow_quick_endorsements__audience_upgrade"

    op.execute(sa.text(f"DROP TABLE IF EXISTS {temporary_table}"))
    op.execute(sa.text(f"""
        CREATE TABLE {temporary_table} (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            audience VARCHAR(20) NOT NULL DEFAULT 'SECRETARY',
            text VARCHAR(160) NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            created_by_id INTEGER,
            created_at DATETIME NOT NULL,
            CONSTRAINT {_SCOPED_UNIQUE} UNIQUE (audience, text),
            FOREIGN KEY(created_by_id) REFERENCES users (id)
        )
    """))
    op.execute(sa.text(f"""
        INSERT INTO {temporary_table}
            (id, audience, text, sort_order, is_active, created_by_id, created_at)
        SELECT id,
               {audience_value},
               text,
               COALESCE(sort_order, 0),
               COALESCE(is_active, 1),
               created_by_id,
               COALESCE(created_at, CURRENT_TIMESTAMP)
          FROM {_TABLE}
    """))
    op.execute(sa.text(f"DROP TABLE {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {temporary_table} RENAME TO {_TABLE}"))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_workflow_quick_endorsements_is_active "
        f"ON {_TABLE} (is_active)"
    ))
    op.execute(sa.text(
        f"CREATE INDEX IF NOT EXISTS {_SCOPED_INDEX} "
        f"ON {_TABLE} (audience, is_active, sort_order)"
    ))


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return

    if bind.dialect.name == "sqlite":
        if _sqlite_requires_rebuild(bind, inspector):
            _rebuild_sqlite_table(bind, inspector)
        else:
            op.execute(sa.text(
                f"UPDATE {_TABLE} SET audience = 'SECRETARY' "
                "WHERE audience IS NULL OR TRIM(audience) = ''"
            ))
            op.execute(sa.text(
                f"CREATE INDEX IF NOT EXISTS {_SCOPED_INDEX} "
                f"ON {_TABLE} (audience, is_active, sort_order)"
            ))
        return

    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _AUDIENCE_COLUMN not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                _AUDIENCE_COLUMN,
                sa.String(length=20),
                nullable=False,
                server_default="SECRETARY",
            ),
        )
    op.execute(sa.text(
        f"UPDATE {_TABLE} SET audience = 'SECRETARY' "
        "WHERE audience IS NULL OR TRIM(audience) = ''"
    ))

    fresh_inspector = sa.inspect(bind)
    for constraint in fresh_inspector.get_unique_constraints(_TABLE):
        if tuple(constraint.get("column_names") or ()) == ("text",) and constraint.get("name"):
            op.drop_constraint(constraint["name"], _TABLE, type_="unique")

    fresh_inspector = sa.inspect(bind)
    if tuple(sorted((_AUDIENCE_COLUMN, "text"))) not in _unique_column_sets(fresh_inspector):
        op.create_unique_constraint(
            _SCOPED_UNIQUE,
            _TABLE,
            [_AUDIENCE_COLUMN, "text"],
        )
    if _SCOPED_INDEX not in {index["name"] for index in sa.inspect(bind).get_indexes(_TABLE)}:
        op.create_index(
            _SCOPED_INDEX,
            _TABLE,
            [_AUDIENCE_COLUMN, "is_active", "sort_order"],
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return

    # Reintroducing the old global unique constraint would silently discard
    # data if the same text now exists in both independent lists.
    duplicate = bind.execute(sa.text(f"""
        SELECT text
          FROM {_TABLE}
         GROUP BY text
        HAVING COUNT(*) > 1
         LIMIT 1
    """)).first()
    if duplicate:
        raise RuntimeError(
            "Cannot downgrade employee endorsement separation while the same "
            "endorsement text exists in both audiences."
        )

    if bind.dialect.name == "sqlite":
        temporary_table = "workflow_quick_endorsements__legacy_downgrade"
        op.execute(sa.text(f"DROP TABLE IF EXISTS {temporary_table}"))
        op.execute(sa.text(f"""
            CREATE TABLE {temporary_table} (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                text VARCHAR(160) NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_by_id INTEGER,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(created_by_id) REFERENCES users (id)
            )
        """))
        op.execute(sa.text(f"""
            INSERT INTO {temporary_table}
                (id, text, sort_order, is_active, created_by_id, created_at)
            SELECT id, text, sort_order, is_active, created_by_id, created_at
              FROM {_TABLE}
        """))
        op.execute(sa.text(f"DROP TABLE {_TABLE}"))
        op.execute(sa.text(f"ALTER TABLE {temporary_table} RENAME TO {_TABLE}"))
        op.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_workflow_quick_endorsements_is_active "
            f"ON {_TABLE} (is_active)"
        ))
        return

    if _SCOPED_INDEX in {index["name"] for index in inspector.get_indexes(_TABLE)}:
        op.drop_index(_SCOPED_INDEX, table_name=_TABLE)
    if tuple(sorted((_AUDIENCE_COLUMN, "text"))) in _unique_column_sets(inspector):
        op.drop_constraint(_SCOPED_UNIQUE, _TABLE, type_="unique")
    op.create_unique_constraint("uq_workflow_quick_endorsements_text", _TABLE, ["text"])
    op.drop_column(_TABLE, _AUDIENCE_COLUMN)
