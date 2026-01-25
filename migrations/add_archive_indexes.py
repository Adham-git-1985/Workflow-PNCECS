from alembic import op
import sqlalchemy as sa

revision = "add_archive_indexes"
down_revision = "<آخر revision عندك>"
branch_labels = None
depends_on = None


def upgrade():
    # ArchiveFile
    op.create_index(
        "ix_archive_file_department",
        "archive_file",
        ["department_id"]
    )
    op.create_index(
        "ix_archive_file_owner",
        "archive_file",
        ["owner_id"]
    )
    op.create_index(
        "ix_archive_file_visibility",
        "archive_file",
        ["visibility"]
    )
    op.create_index(
        "ix_archive_file_created_at",
        "archive_file",
        ["created_at"]
    )

    # AuditLog (archive related)
    op.create_index(
        "ix_audit_target",
        "audit_log",
        ["target_type", "target_id"]
    )
    op.create_index(
        "ix_audit_created_at",
        "audit_log",
        ["created_at"]
    )


def downgrade():
    op.drop_index("ix_archive_file_department", table_name="archive_file")
    op.drop_index("ix_archive_file_owner", table_name="archive_file")
    op.drop_index("ix_archive_file_visibility", table_name="archive_file")
    op.drop_index("ix_archive_file_created_at", table_name="archive_file")
    op.drop_index("ix_audit_target", table_name="audit_log")
    op.drop_index("ix_audit_created_at", table_name="audit_log")
