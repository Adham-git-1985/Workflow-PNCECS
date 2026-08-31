"""Keep employee-file attachments available in the employee's private archive."""

from __future__ import annotations

import mimetypes
import os
import shutil
from datetime import datetime
from pathlib import Path

from flask import current_app

from extensions import db
from models import ArchivedFile, EmployeeAttachment
from utils.file_uploads import clean_original_filename, random_storage_name


def _employee_attachment_path(attachment: EmployeeAttachment) -> Path:
    return (
        Path(current_app.instance_path)
        / "uploads"
        / "employees"
        / str(attachment.user_id)
        / (attachment.stored_name or "")
    )


def _archive_storage_dir() -> Path:
    """Use the normal archive directory, with a test-friendly config override."""
    configured = current_app.config.get("ARCHIVE_STORAGE_DIR")
    return Path(configured) if configured else Path(os.getcwd()) / "storage" / "archive"


def _archive_description(attachment: EmployeeAttachment) -> str:
    if (attachment.attachment_type or "").upper() == "PAYSLIP":
        period = attachment.payslip_period_label
        return f"Employee payslip ({period})" if period != "-" else "Employee payslip"
    return "Employee file attachment"


def sync_employee_attachment_to_archive(
    attachment: EmployeeAttachment,
    *,
    source_path: Path | None = None,
) -> ArchivedFile:
    """Create or refresh the attachment's private archive copy.

    The archive file is owned by ``attachment.user_id`` rather than by the HR
    user who uploaded it.  Therefore normal archive permissions make it visible
    to the employee and no extra sharing record is needed.
    """
    source = Path(source_path) if source_path is not None else _employee_attachment_path(attachment)
    if not source.is_file():
        raise FileNotFoundError(f"Employee attachment source does not exist: {source}")

    original_name = clean_original_filename(attachment.original_name)
    if not original_name:
        raise ValueError("Employee attachment must have a valid filename")

    archived = None
    if attachment.archived_file_id:
        archived = db.session.get(ArchivedFile, attachment.archived_file_id)

    archive_dir = _archive_storage_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)

    if archived is None:
        # Preserve random storage names across the archive, including when two
        # employees upload files with the same visible name.
        from uuid import uuid4

        stored_name = random_storage_name(f"employee_{uuid4().hex}", original_name)
        archive_path = archive_dir / stored_name
        archived = ArchivedFile(
            original_name=original_name,
            stored_name=stored_name,
            description=_archive_description(attachment),
            file_path=str(archive_path),
            owner_id=attachment.user_id,
            visibility="owner",
        )
        db.session.add(archived)
        db.session.flush()
        attachment.archived_file_id = archived.id
    else:
        archive_path = Path(archived.file_path or (archive_dir / archived.stored_name))
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archived.original_name = original_name
        archived.description = _archive_description(attachment)
        archived.owner_id = attachment.user_id
        archived.visibility = "owner"
        archived.is_deleted = False
        archived.deleted_at = None
        archived.deleted_by = None
        archived.is_final_deleted = False
        archived.final_deleted_at = None
        archived.final_deleted_by = None
        archived.upload_date = datetime.utcnow()
        archived.file_path = str(archive_path)

    shutil.copy2(source, archive_path)
    archived.mime_type = mimetypes.guess_type(original_name)[0]
    archived.file_size = archive_path.stat().st_size
    return archived


def archive_employee_attachment_deletion(attachment: EmployeeAttachment, *, deleted_by_id: int | None) -> None:
    """Hide the corresponding archive record when HR deletes the attachment."""
    if not attachment.archived_file_id:
        return

    archived = db.session.get(ArchivedFile, attachment.archived_file_id)
    if not archived:
        return

    archived.is_deleted = True
    archived.deleted_at = datetime.utcnow()
    archived.deleted_by = deleted_by_id


def sync_pending_employee_attachments_for_user(user_id: int) -> int:
    """Backfill legacy attachments for one employee when they open Archive.

    Missing source files are intentionally skipped: no empty or broken archive
    entries are created.  The work is idempotent because each successful row is
    linked through ``EmployeeAttachment.archived_file_id``.
    """
    attachments = (
        EmployeeAttachment.query
        .filter(
            EmployeeAttachment.user_id == user_id,
            EmployeeAttachment.archived_file_id.is_(None),
        )
        .all()
    )

    synced = 0
    for attachment in attachments:
        try:
            sync_employee_attachment_to_archive(attachment)
            synced += 1
        except (FileNotFoundError, OSError, ValueError):
            continue

    if synced:
        db.session.commit()
    return synced
