"""add independent usernames for account login

Revision ID: f7e8d9c0b1a2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa


revision = "f7e8d9c0b1a2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def _unique_username(candidate: str, used: set[str]) -> str:
    base = candidate or "user"
    value = base
    suffix = 1
    while value.casefold() in used:
        suffix += 1
        value = f"{base}-{suffix}"
    return value


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "username" not in columns:
        op.add_column("users", sa.Column("username", sa.String(length=255), nullable=True))

    rows = bind.execute(
        sa.text("SELECT id, username, email FROM users ORDER BY id ASC")
    ).mappings().all()
    used: set[str] = set()
    for row in rows:
        username = str(row["username"] or "").strip()
        if not username or username.casefold() in used:
            fallback = str(row["email"] or "").strip() or f"user-{int(row['id'])}"
            username = _unique_username(fallback, used)
            bind.execute(
                sa.text("UPDATE users SET username = :username WHERE id = :id"),
                {"username": username, "id": int(row["id"])},
            )
        used.add(username.casefold())

    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_username" not in indexes:
        op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_username" in indexes:
        op.drop_index("ix_users_username", table_name="users")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("users")}
    if "username" in columns:
        op.drop_column("users", "username")
