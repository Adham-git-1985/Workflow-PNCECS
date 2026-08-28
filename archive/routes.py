# archive/routes.py

import os
import mimetypes
import re
import uuid
import zipfile
from datetime import datetime, timedelta
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from flask import (
    render_template, request, redirect,
    url_for, flash, send_file, abort
)
from flask_login import login_required, current_user
from sqlalchemy import or_, func
from sqlalchemy.orm import joinedload

from extensions import db
from permissions import roles_required

from archive.permissions import (
    can_view_archive_file,
    can_edit_archive_file,
    can_manage_archive_file
)

from archive.cache import get_cached_file, set_cached_file
from archive.queries import archive_access_query
from utils.events import emit_event
from utils.file_uploads import (
    clean_original_filename,
    is_allowed_attachment,
    is_safe_inline_mimetype,
    random_storage_name,
)

from models import (
    ArchivedFile,
    FilePermission,
    User,
    AuditLog,
    WorkflowRequest,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowStepTask,
    RequestAttachment,
    WorkflowTemplate,
    SystemSetting,
    CorrAttachment,
)

from workflow.engine import (
    resolve_template_participant_user_ids,
    start_workflow_for_request,
)
from services.workflow_confidentiality import (
    can_user_access_archived_file_confidentiality,
    can_user_pass_confidential_workflow_gate,
    filter_confidential_workflow_user_ids,
)

from archive import archive_bp


# =========================
# Storage
# =========================
BASE_STORAGE = os.path.join(os.getcwd(), "storage", "archive")

IMAGE_PREVIEW_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff",
}

TEXT_PREVIEW_EXTENSIONS = {
    "txt", "csv", "tsv", "json", "xml", "html", "htm", "css", "js",
    "py", "java", "php", "sql", "md", "log", "ini", "conf", "yml",
    "yaml", "rtf",
}

OFFICE_TEXT_PREVIEW_EXTENSIONS = {
    "docx", "xlsx", "pptx", "odt", "ods", "odp",
}

TEXT_PREVIEW_MAX_BYTES = 1024 * 1024
TEXT_PREVIEW_MAX_CHARS = 200_000


# =========================
# Helpers
# =========================
def _is_super_admin(user) -> bool:
    try:
        return user.has_role("SUPER_ADMIN") or user.has_role("SUPERADMIN")
    except Exception:
        return False


def _archive_extension(file) -> str:
    name = file.original_name or file.stored_name or file.file_path or ""
    return os.path.splitext(name)[1].lower().lstrip(".")


def _archive_mimetype(file) -> str:
    guessed, _ = mimetypes.guess_type(file.original_name or file.file_path or "")
    return file.mime_type or guessed or "application/octet-stream"


def _archive_disk_path(file) -> str | None:
    candidates = []
    if file.file_path:
        candidates.append(file.file_path)
        candidates.append(os.path.join(BASE_STORAGE, os.path.basename(file.file_path)))
    if file.stored_name:
        candidates.append(os.path.join(BASE_STORAGE, file.stored_name))

    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.exists(path):
            return path
    return None


def _limit_preview_text(text: str) -> tuple[str, bool]:
    text = (text or "").replace("\x00", "")
    if len(text) > TEXT_PREVIEW_MAX_CHARS:
        return text[:TEXT_PREVIEW_MAX_CHARS], True
    return text, False


def _decode_text_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "windows-1256", "cp1256", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_basic_rtf(text: str) -> str:
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\'[0-9a-fA-F]{2}", "", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _read_text_preview(path: str, ext: str) -> tuple[str, bool]:
    with open(path, "rb") as handle:
        raw = handle.read(TEXT_PREVIEW_MAX_BYTES + 1)
    truncated = len(raw) > TEXT_PREVIEW_MAX_BYTES
    text = _decode_text_bytes(raw[:TEXT_PREVIEW_MAX_BYTES])
    if ext == "rtf":
        text = _strip_basic_rtf(text)
    text, char_truncated = _limit_preview_text(text)
    return text, truncated or char_truncated


def _zip_xml_root(zf: zipfile.ZipFile, name: str):
    return ET.fromstring(zf.read(name))


def _docx_text(path: str) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as zf:
        root = _zip_xml_root(zf, "word/document.xml")
    lines = []
    for paragraph in root.iter(f"{ns}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{ns}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{ns}tab":
                parts.append("\t")
            elif node.tag == f"{ns}br":
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    root = _zip_xml_root(zf, "xl/sharedStrings.xml")
    return [
        "".join(node.text or "" for node in item.iter(f"{ns}t"))
        for item in root.iter(f"{ns}si")
    ]


def _natural_archive_name(value: str):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def _xlsx_text(path: str) -> tuple[str, bool]:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    lines = []
    truncated = False
    with zipfile.ZipFile(path) as zf:
        shared_strings = _xlsx_shared_strings(zf)
        sheets = sorted(
            [
                name for name in zf.namelist()
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            ],
            key=_natural_archive_name,
        )
        for index, sheet in enumerate(sheets, 1):
            root = _zip_xml_root(zf, sheet)
            lines.append(f"ورقة {index}")
            for row_index, row in enumerate(root.iter(f"{ns}row")):
                if row_index >= 200:
                    truncated = True
                    lines.append("...")
                    break
                values = []
                for cell in row.findall(f"{ns}c"):
                    cell_type = cell.attrib.get("t")
                    value = ""
                    if cell_type == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter(f"{ns}t"))
                    else:
                        raw_value = cell.find(f"{ns}v")
                        if raw_value is not None and raw_value.text is not None:
                            value = raw_value.text
                            if cell_type == "s":
                                try:
                                    value = shared_strings[int(value)]
                                except (ValueError, IndexError):
                                    pass
                    values.append(value)
                if any(values):
                    lines.append("\t".join(values))
            lines.append("")
    return "\n".join(lines).strip(), truncated


def _pptx_text(path: str) -> str:
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    lines = []
    with zipfile.ZipFile(path) as zf:
        slides = sorted(
            [
                name for name in zf.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ],
            key=_natural_archive_name,
        )
        for index, slide in enumerate(slides, 1):
            root = _zip_xml_root(zf, slide)
            texts = [node.text for node in root.iter(f"{ns}t") if node.text]
            if texts:
                lines.append(f"شريحة {index}")
                lines.extend(texts)
                lines.append("")
    return "\n".join(lines).strip()


def _open_document_text(path: str) -> str:
    with zipfile.ZipFile(path) as zf:
        root = _zip_xml_root(zf, "content.xml")
    texts = []
    for node in root.iter():
        if node.text and node.text.strip():
            texts.append(node.text.strip())
    return "\n".join(texts)


def _office_text_preview(path: str, ext: str) -> tuple[str, bool]:
    if ext == "docx":
        text = _docx_text(path)
        return _limit_preview_text(text)
    if ext == "xlsx":
        text, rows_truncated = _xlsx_text(path)
        text, chars_truncated = _limit_preview_text(text)
        return text, rows_truncated or chars_truncated
    if ext == "pptx":
        text = _pptx_text(path)
        return _limit_preview_text(text)
    if ext in {"odt", "ods", "odp"}:
        text = _open_document_text(path)
        return _limit_preview_text(text)
    return "", False


def _render_archive_preview(file, download_endpoint: str):
    disk_path = _archive_disk_path(file)
    if not disk_path:
        abort(404)

    ext = _archive_extension(file)
    mime_type = _archive_mimetype(file)
    mime_compare = mime_type.lower().split(";", 1)[0].strip()

    if ext == "pdf" or mime_compare == "application/pdf":
        response = send_file(
            disk_path,
            mimetype="application/pdf",
            as_attachment=False,
            download_name=file.original_name,
            conditional=True,
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    if ext in IMAGE_PREVIEW_EXTENSIONS or (
        mime_compare.startswith("image/") and is_safe_inline_mimetype(mime_compare)
    ):
        guessed_image_mime = mimetypes.guess_type(f"file.{ext}")[0] if ext else None
        inline_image_mime = guessed_image_mime or mime_compare
        response = send_file(
            disk_path,
            mimetype=inline_image_mime,
            as_attachment=False,
            download_name=file.original_name,
            conditional=True,
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    if ext in TEXT_PREVIEW_EXTENSIONS or mime_compare.startswith("text/"):
        preview_text, truncated = _read_text_preview(disk_path, ext)
        return render_template(
            "archive/preview_text.html",
            file=file,
            preview_text=preview_text,
            truncated=truncated,
            preview_note=None,
            download_endpoint=download_endpoint,
        )

    if ext in OFFICE_TEXT_PREVIEW_EXTENSIONS:
        try:
            preview_text, truncated = _office_text_preview(disk_path, ext)
        except (KeyError, OSError, ET.ParseError, zipfile.BadZipFile):
            preview_text, truncated = "", False

        if preview_text.strip():
            return render_template(
                "archive/preview_text.html",
                file=file,
                preview_text=preview_text,
                truncated=truncated,
                preview_note="تم استخراج النص للقراءة السريعة، وقد لا يحافظ على تنسيق الملف الأصلي.",
                download_endpoint=download_endpoint,
            )

    return render_template(
        "archive/preview_unavailable.html",
        file=file,
        file_ext=ext.upper() if ext else "",
        mime_type=mime_type,
        download_endpoint=download_endpoint,
    )

def allowed_file(filename):
    """Generic archive attachments accept every file extension."""
    return is_allowed_attachment(filename)


def get_archive_counters(user):
    base = archive_access_query(user)

    total = base.with_entities(func.count(ArchivedFile.id)).scalar()

    mine = base.filter(
        ArchivedFile.owner_id == user.id
    ).with_entities(func.count(ArchivedFile.id)).scalar()

    shared = (
        db.session.query(func.count(FilePermission.file_id))
        .filter(FilePermission.user_id == user.id)
        .scalar()
    )

    return {
        "total": total or 0,
        "mine": mine or 0,
        "shared": shared or 0
    }


def get_shared_count_map(files):
    file_ids = [f.id for f in files]
    if not file_ids:
        return {}

    rows = (
        db.session.query(
            FilePermission.file_id,
            db.func.count(FilePermission.user_id)
        )
        .filter(FilePermission.file_id.in_(file_ids))
        .group_by(FilePermission.file_id)
        .all()
    )

    return {fid: count for fid, count in rows}

def get_shared_by_map(files, current_user_id:int):
    """For each file in list, if it is shared to current_user_id, return who shared it.
    Returns dict: {file_id: sharer_email_or_label}
    """
    file_ids = [f.id for f in files]
    if not file_ids:
        return {}

    # permissions for CURRENT USER only
    rows = (
        db.session.query(FilePermission.file_id, FilePermission.shared_by, User.email)
        .join(User, User.id == FilePermission.shared_by, isouter=True)
        .filter(
            FilePermission.file_id.in_(file_ids),
            FilePermission.user_id == int(current_user_id)
        )
        .all()
    )

    out = {}
    for fid, shared_by, email in rows:
        if shared_by:
            out[int(fid)] = email or f"User#{shared_by}"
        else:
            out[int(fid)] = "غير معروف"
    return out


def get_trash_retention_days() -> int:
    """Return recycle bin retention period in days (SystemSetting TRASH_RETENTION_DAYS)."""
    setting = SystemSetting.query.filter_by(key="TRASH_RETENTION_DAYS").first()
    try:
        return int(setting.value) if setting and setting.value else 30
    except Exception:
        return 30


FILE_TYPE_FILTERS = {
    "PDF": ["pdf"],
    "IMAGE": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff"],
    "DOCUMENT": ["doc", "docx", "odt", "rtf", "txt"],
    "SPREADSHEET": ["xls", "xlsx", "ods", "csv"],
    "PRESENTATION": ["ppt", "pptx", "odp"],
    "ARCHIVE": ["zip", "rar", "7z"],
}

FILE_TYPE_OPTIONS = [
    ("", "كل الأنواع"),
    ("PDF", "PDF"),
    ("IMAGE", "صور"),
    ("DOCUMENT", "مستندات"),
    ("SPREADSHEET", "جداول"),
    ("PRESENTATION", "عروض"),
    ("ARCHIVE", "ملفات مضغوطة"),
]

VISIBILITY_OPTIONS = [
    ("", "كل الصلاحيات"),
    ("owner", "خاص/مالك"),
    ("workflow", "مرتبط بمسار"),
    ("shared", "مشارك"),
    ("PUBLIC", "عام"),
    ("DEPARTMENT", "دائرة"),
    ("PRIVATE", "خاص"),
]

WORKFLOW_STATUS_OPTIONS = [
    ("", "كل الحالات"),
    ("DRAFT", "مسودة"),
    ("IN_PROGRESS", "قيد الإجراء"),
    ("APPROVED", "معتمد"),
    ("REJECTED", "مرفوض"),
]


def _parse_date_arg(value: str | None):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except Exception:
        return None


def _archive_filters_from_request() -> dict:
    owner_id = request.args.get("owner_id", type=int)
    return {
        "q": (request.args.get("q") or "").strip(),
        "file_type": (request.args.get("file_type") or "").strip().upper(),
        "visibility": (request.args.get("visibility") or "").strip(),
        "signed": (request.args.get("signed") or "").strip(),
        "owner_id": owner_id if owner_id else None,
        "date_from": (request.args.get("date_from") or "").strip(),
        "date_to": (request.args.get("date_to") or "").strip(),
        "workflow_q": (request.args.get("workflow_q") or "").strip(),
        "workflow_status": (request.args.get("workflow_status") or "").strip().upper(),
    }


def _page_query(filters: dict) -> str:
    pairs = []
    for key, value in (filters or {}).items():
        if value is None or value == "":
            continue
        pairs.append((key, value))
    return urlencode(pairs)


def _extension_filter(exts: list[str]):
    clauses = []
    for ext in exts:
        ext = (ext or "").lower().lstrip(".")
        if not ext:
            continue
        clauses.append(func.lower(ArchivedFile.original_name).like(f"%.{ext}"))
    return or_(*clauses) if clauses else None


def _apply_archive_filters(query, filters: dict):
    q = (filters.get("q") or "").strip()
    if q:
        clauses = [
            ArchivedFile.original_name.ilike(f"%{q}%"),
            ArchivedFile.description.ilike(f"%{q}%"),
        ]
        if q.isdigit():
            clauses.append(ArchivedFile.id == int(q))
        query = query.filter(or_(*clauses))

    file_type = (filters.get("file_type") or "").strip().upper()
    if file_type:
        exts = FILE_TYPE_FILTERS.get(file_type)
        if not exts and file_type.isalnum() and len(file_type) <= 12:
            exts = [file_type.lower()]
        ext_clause = _extension_filter(exts or [])
        if ext_clause is not None:
            query = query.filter(ext_clause)

    visibility = (filters.get("visibility") or "").strip()
    if visibility:
        query = query.filter(ArchivedFile.visibility == visibility)

    signed = (filters.get("signed") or "").strip()
    if signed == "1":
        query = query.filter(ArchivedFile.is_signed.is_(True))
    elif signed == "0":
        query = query.filter(or_(ArchivedFile.is_signed.is_(False), ArchivedFile.is_signed.is_(None)))

    owner_id = filters.get("owner_id")
    if owner_id:
        query = query.filter(ArchivedFile.owner_id == int(owner_id))

    date_from = _parse_date_arg(filters.get("date_from"))
    if date_from:
        query = query.filter(ArchivedFile.upload_date >= date_from)

    date_to = _parse_date_arg(filters.get("date_to"))
    if date_to:
        query = query.filter(ArchivedFile.upload_date < (date_to + timedelta(days=1)))

    return query


def _archive_user_can_view_workflow(user, req: WorkflowRequest) -> bool:
    if not user or not req:
        return False
    if not can_user_pass_confidential_workflow_gate(user, req):
        return False
    if req.requester_id == user.id:
        return True
    if user.has_role("ADMIN") or user.has_role("SUPER_ADMIN") or user.has_role("SUPERADMIN"):
        return True

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    if not inst:
        return False

    if (
        WorkflowStepTask.query
        .filter_by(instance_id=inst.id, assignee_user_id=user.id)
        .first()
        is not None
    ):
        return True

    if (
        AuditLog.query
        .filter(
            AuditLog.request_id == req.id,
            AuditLog.action == "WORKFLOW_MENTION_ACCESS",
            AuditLog.target_type == "USER",
            AuditLog.target_id == user.id,
        )
        .first()
        is not None
    ):
        return True

    steps = WorkflowInstanceStep.query.filter_by(instance_id=inst.id).all()
    role = (user.role or "").strip().lower()
    for step in steps:
        kind = (step.approver_kind or "").strip().upper()
        if kind == "USER" and step.approver_user_id == user.id:
            return True
        if kind == "ROLE" and step.approver_role and role == (step.approver_role or "").strip().lower():
            return True
        if kind == "DEPARTMENT" and step.approver_department_id and user.department_id == step.approver_department_id:
            if role in ("dept_head", "department_head") or user.has_role("DEPT_HEAD"):
                return True
        if kind == "DIRECTORATE" and getattr(step, "approver_directorate_id", None):
            if getattr(user, "directorate_id", None) == step.approver_directorate_id and role in ("directorate_head", "directorate_deputy"):
                return True

    return False


def _search_workflows_from_archive(filters: dict) -> list[WorkflowRequest]:
    term = (filters.get("workflow_q") or "").strip()
    status = (filters.get("workflow_status") or "").strip().upper()

    if not term and not status:
        return []

    query = WorkflowRequest.query.options(joinedload(WorkflowRequest.requester))

    if term:
        clauses = [
            WorkflowRequest.title.ilike(f"%{term}%"),
            WorkflowRequest.description.ilike(f"%{term}%"),
            WorkflowRequest.status.ilike(f"%{term}%"),
        ]
        if term.isdigit():
            clauses.append(WorkflowRequest.id == int(term))
        query = query.filter(or_(*clauses))

    if status:
        query = query.filter(WorkflowRequest.status == status)

    candidates = (
        query
        .order_by(WorkflowRequest.created_at.desc())
        .limit(80)
        .all()
    )
    return [r for r in candidates if _archive_user_can_view_workflow(current_user, r)][:20]


def _filter_files_to_workflow_results(query, filters: dict, workflow_results: list[WorkflowRequest]):
    if not (filters.get("workflow_q") or filters.get("workflow_status")):
        return query
    request_ids = [row.id for row in workflow_results]
    if not request_ids:
        return query.filter(ArchivedFile.id == -1)
    return (
        query.join(RequestAttachment, RequestAttachment.archived_file_id == ArchivedFile.id)
        .filter(RequestAttachment.request_id.in_(request_ids))
        .distinct()
    )


# =========================
# Sign PDF (flag only)
# =========================
@archive_bp.route("/sign/<int:file_id>", methods=["POST"], endpoint="sign_pdf")
@login_required
def sign_pdf(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

    # Access control: must be able to see the file + have SIGN_ARCHIVE permission.
    if not can_view_archive_file(current_user, file):
        abort(403)

    # Role-based permission (RolePermission). We keep SIGN_ARCHIVE as a permission hook,
    # but it is also seeded as a "basic" permission for active roles on startup.
    try:
        if not current_user.has_role_perm("SIGN_ARCHIVE"):
            abort(403)
    except Exception:
        # Fallback: keep legacy behavior for ADMIN if permission system is unavailable.
        if not current_user.has_role("ADMIN"):
            abort(403)

    if file.is_deleted:
        abort(404)

    # Signing is intended for PDFs only.
    try:
        is_pdf = (getattr(file, "file_type", "") or "").strip().upper() == "PDF"
        if not is_pdf:
            on = (getattr(file, "original_name", "") or "").lower()
            is_pdf = on.endswith(".pdf")
    except Exception:
        is_pdf = False
    if not is_pdf:
        flash("التوقيع متاح لملفات PDF فقط.", "warning")
        return redirect(url_for("archive.file_details", file_id=file.id))

    if file.is_signed:
        flash("الملف موقّع مسبقًا", "warning")
        return redirect(url_for("archive.my_files"))

    file.is_signed = True
    file.signed_at = datetime.utcnow()
    file.signed_by = current_user.id

    db.session.add(
        AuditLog(
            action="ARCHIVE_SIGNED",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            note=f"File signed: {file.original_name}"
        )
    )

    db.session.commit()

    flash("تم توقيع الملف بنجاح", "success")
    return redirect(url_for("archive.my_files"))


# =========================
# Browse Files
# =========================
@archive_bp.route("/files")
@login_required
def archive_files():
    filters = _archive_filters_from_request()
    q = filters["q"]
    page = request.args.get("page", 1, type=int)

    workflow_results = _search_workflows_from_archive(filters)
    query = archive_access_query(current_user)
    query = _apply_archive_filters(query, filters)
    query = _filter_files_to_workflow_results(query, filters, workflow_results)

    pagination = (
        query
        .order_by(ArchivedFile.upload_date.desc())
        .paginate(page=page, per_page=15, error_out=False)
    )

    counters = get_archive_counters(current_user)

    return render_template(
        "archive/files.html",
        files=pagination.items,
        pagination=pagination,
        q=q,
        counters=counters,
        filters=filters,
        page_query=_page_query(filters),
        owners=User.query.order_by(User.email.asc()).all(),
        file_type_options=FILE_TYPE_OPTIONS,
        visibility_options=VISIBILITY_OPTIONS,
        workflow_status_options=WORKFLOW_STATUS_OPTIONS,
        workflow_results=workflow_results,
        archive_reset_endpoint="archive.archive_files",
    )


# =========================
# File Details
# =========================
@archive_bp.route("/files/<int:file_id>")
@login_required
def file_details(file_id):
    page = request.args.get("page", 1, type=int)

    cached_id = get_cached_file(file_id)

    if cached_id:
        file = (
            ArchivedFile.query
            .options(joinedload(ArchivedFile.owner))
            .get_or_404(cached_id)
        )
    else:
        file = (
            ArchivedFile.query
            .options(joinedload(ArchivedFile.owner))
            .get_or_404(file_id)
        )
        set_cached_file(file_id, file.id)

    if getattr(file, 'is_final_deleted', False) and not _is_super_admin(current_user):
        abort(404)

    if not can_view_archive_file(current_user, file):
        abort(403)

    audit_logs = (
        AuditLog.query
        .filter(
            AuditLog.target_id == file.id,
            AuditLog.target_type.in_(["ArchivedFile", "ARCHIVE_FILE"])
        )
        .order_by(AuditLog.created_at.desc())
        .paginate(page=page, per_page=10, error_out=False)
    )

    shared_with = (
        FilePermission.query
        .join(User, User.id == FilePermission.user_id)
        .filter(FilePermission.file_id == file.id)
        .all()
    )
    workflow_requests = (
        WorkflowRequest.query
        .join(RequestAttachment, RequestAttachment.request_id == WorkflowRequest.id)
        .filter(RequestAttachment.archived_file_id == file.id)
        .order_by(WorkflowRequest.created_at.desc())
        .all()
    )
    workflow_templates = (
        WorkflowTemplate.query
        .filter_by(is_active=True)
        .order_by(WorkflowTemplate.name.asc())
        .all()
    )

    return render_template(
        "archive/file_details.html",
        file=file,
        audit_logs=audit_logs,
        can_edit=can_edit_archive_file(current_user, file),
        can_manage=can_manage_archive_file(current_user, file),
        shared_with=shared_with,
        workflow_requests=workflow_requests,
        workflow_templates=workflow_templates,
    )


@archive_bp.route("/files/<int:file_id>/workflow/start", methods=["POST"])
@login_required
def start_file_workflow(file_id):
    file = ArchivedFile.query.get_or_404(file_id)
    if getattr(file, "is_final_deleted", False) and not _is_super_admin(current_user):
        abort(404)
    if not can_view_archive_file(current_user, file):
        abort(403)

    template_id = (request.form.get("template_id") or "").strip()
    templates = (
        WorkflowTemplate.query
        .filter_by(is_active=True)
        .order_by(WorkflowTemplate.name.asc())
        .all()
    )
    template = None
    if template_id.isdigit():
        template = WorkflowTemplate.query.filter_by(id=int(template_id), is_active=True).first()
    elif len(templates) == 1:
        template = templates[0]

    if not template:
        flash("يرجى اختيار قالب مسار فعال.", "danger")
        return redirect(url_for("archive.file_details", file_id=file.id))

    source_secret_req = (
        WorkflowRequest.query
        .join(RequestAttachment, RequestAttachment.request_id == WorkflowRequest.id)
        .filter(
            RequestAttachment.archived_file_id == file.id,
            WorkflowRequest.confidentiality == "SECRET",
        )
        .order_by(WorkflowRequest.id.asc())
        .first()
    )
    if source_secret_req:
        participant_ids = resolve_template_participant_user_ids(template)
        allowed_ids = filter_confidential_workflow_user_ids(
            source_secret_req,
            participant_ids,
        )
        unauthorized_ids = participant_ids.difference(allowed_ids)
        if unauthorized_ids:
            names = [
                user.full_name or user.email or f"مستخدم #{user.id}"
                for user in User.query.filter(User.id.in_(unauthorized_ids)).limit(8).all()
            ]
            flash(
                "لم يبدأ المسار لأن الملف سري والقالب يضم مشاركين غير مخولين: "
                + "، ".join(names),
                "danger",
            )
            return redirect(url_for("archive.file_details", file_id=file.id))

    try:
        title = f"مسار من الأرشيف: {file.original_name}"
        if len(title) > 200:
            title = title[:197] + "..."
        description = "\n".join([
            f"مصدر المسار: الأرشيف",
            f"اسم الملف: {file.original_name}",
            f"رقم ملف الأرشيف: {file.id}",
            "",
            file.description or "",
        ]).strip()

        req = WorkflowRequest(
            requester_id=current_user.id,
            status="DRAFT",
            title=title,
            description=description,
            confidentiality=(
                getattr(source_secret_req, "confidentiality", None) or "NORMAL"
            ),
            source_corr_kind=getattr(source_secret_req, "source_corr_kind", None),
            source_corr_id=getattr(source_secret_req, "source_corr_id", None),
        )
        db.session.add(req)
        db.session.flush()
        db.session.add(RequestAttachment(request_id=req.id, archived_file_id=file.id))
        db.session.add(AuditLog(
            request_id=req.id,
            user_id=current_user.id,
            action="WORKFLOW_ATTACHMENT_UPLOADED",
            note=f"Attachment: {file.original_name} | file_id={file.id} | source=ARCHIVE_FILE",
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            created_at=datetime.utcnow(),
        ))

        start_workflow_for_request(
            req,
            template,
            created_by_user_id=current_user.id,
            auto_commit=False,
        )

        for att in CorrAttachment.query.filter_by(archive_file_id=file.id).all():
            att.workflow_request_id = req.id
            db.session.add(att)

        db.session.add(AuditLog(
            request_id=req.id,
            user_id=current_user.id,
            action="ARCHIVE_WORKFLOW_START",
            note=f"template={template.name}",
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            created_at=datetime.utcnow(),
        ))
        db.session.commit()
        flash("تم إنشاء مسار من ملف الأرشيف.", "success")
        return redirect(url_for("workflow.view_request", request_id=req.id))
    except Exception:
        db.session.rollback()
        flash("تعذر إنشاء المسار من ملف الأرشيف.", "danger")
        return redirect(url_for("archive.file_details", file_id=file.id))


# =========================
# Upload
# =========================
@archive_bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload_file():
    if request.method == "POST":

        # Accept standard archive uploads and documents selected after scanning.
        # Keep the legacy single-file field for older archive clients.
        files = []
        if request.files:
            files = (
                (request.files.getlist("files") or [])
                + (request.files.getlist("scanned_files") or [])
            )
            if not files:
                single = request.files.get("file")
                if single:
                    files = [single]

        description = request.form.get("description")

        files = [f for f in (files or []) if f and getattr(f, "filename", "")]
        if not files:
            flash("لم يتم اختيار ملف", "danger")
            return redirect(request.url)

        # Validate all files before saving
        for f in files:
            if not allowed_file(f.filename):
                flash(f"اسم الملف غير صالح: {f.filename}", "danger")
                return redirect(request.url)

        os.makedirs(BASE_STORAGE, exist_ok=True)

        send_to_workflow = request.form.get("send_to_workflow") == "1"
        template_id = (request.form.get("template_id") or "").strip()

        if send_to_workflow and not template_id.isdigit():
            flash("يرجى اختيار مسار (Template) لبدء مسار العمل.", "danger")
            return redirect(request.url)

        saved_paths = []
        archived_files = []

        try:
            # 1) Save all files to archive
            for f in files:
                original_name = clean_original_filename(f.filename)

                if not original_name:
                    raise ValueError("اسم الملف غير صالح")

                stored_name = random_storage_name(uuid.uuid4().hex, original_name)
                file_path = os.path.join(BASE_STORAGE, stored_name)

                f.save(file_path)
                saved_paths.append(file_path)

                archived = ArchivedFile(
                    original_name=original_name,
                    stored_name=stored_name,
                    description=description,
                    file_path=file_path,
                    mime_type=f.mimetype,
                    file_size=os.path.getsize(file_path),
                    owner_id=current_user.id,
                    visibility="owner" if not send_to_workflow else "workflow",
                )
                db.session.add(archived)
                db.session.flush()
                archived_files.append(archived)

            # 2) If send to workflow: create ONE request and attach ALL files
            if send_to_workflow:
                req = WorkflowRequest(
                    requester_id=current_user.id,
                    status="DRAFT",
                    title=request.form.get("request_title", "طلب مرفق من الأرشيف"),
                    description=request.form.get("request_description", ""),
                )
                db.session.add(req)
                db.session.flush()

                for archived in archived_files:
                    db.session.add(RequestAttachment(request_id=req.id, archived_file_id=archived.id))
                    # audit attachment so we can display step-aware grouping later
                    db.session.add(AuditLog(
                        request_id=req.id,
                        user_id=current_user.id,
                        action="WORKFLOW_ATTACHMENT_UPLOADED",
                        note=f"Attachment: {archived.original_name} | file_id={archived.id} | step=0 | source=ARCHIVE_UPLOAD",
                        target_type="ARCHIVE_FILE",
                        target_id=archived.id,
                        created_at=datetime.utcnow(),
                    ))

                template = WorkflowTemplate.query.get_or_404(int(template_id))
                start_workflow_for_request(
                    req,
                    template,
                    created_by_user_id=current_user.id,
                    auto_commit=False,
                )

                # notify admins (single)
                emit_event(
                    actor_id=current_user.id,
                    action="ARCHIVE_UPLOADED",
                    message=f"تم رفع {len(archived_files)} ملف/ملفات وبدء مسار عمل للطلب #{req.id}",
                    target_type="WorkflowRequest",
                    target_id=req.id,
                    notify_role="ADMIN",
                    auto_commit=False,
                )

                db.session.commit()
                flash("تم رفع الملفات وبدء مسار العمل بنجاح", "success")
                return redirect(url_for("workflow.view_request", request_id=req.id))

            # 3) Archive only: notify admins for each file (or single message)
            for archived in archived_files:
                emit_event(
                    actor_id=current_user.id,
                    action="ARCHIVE_UPLOADED",
                    message=f"تم رفع ملف أرشيف: {archived.original_name}",
                    target_type="ARCHIVE_FILE",
                    target_id=archived.id,
                    notify_role="ADMIN",
                    auto_commit=False,
                )

            db.session.commit()
            flash("تم رفع الملفات بنجاح", "success")
            return redirect(url_for("archive.my_files"))

        except Exception as e:
            db.session.rollback()
            # cleanup saved files
            for fp in saved_paths:
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception:
                    pass

            flash(f"حدث خطأ أثناء رفع الملف: {e}", "danger")
            return redirect(request.url)


    templates = (
        WorkflowTemplate.query
        .filter_by(is_active=True)
        .order_by(WorkflowTemplate.id.desc())
        .all()
    )
    return render_template("archive/upload.html", templates=templates)


# =========================
# My Files
# =========================
@archive_bp.route("/my-files")
@login_required
def my_files():
    filters = _archive_filters_from_request()
    q = filters["q"]
    page = request.args.get("page", 1, type=int)
    per_page = 15

    workflow_results = _search_workflows_from_archive(filters)
    query = archive_access_query(current_user)
    query = _apply_archive_filters(query, filters)
    query = _filter_files_to_workflow_results(query, filters, workflow_results)

    pagination = (
        query
        .order_by(ArchivedFile.upload_date.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    delegated_files = {
        p.file_id
        for p in FilePermission.query.filter(
            FilePermission.user_id == current_user.id,
            FilePermission.delegated_by.isnot(None)
        )
    }

    counters = get_archive_counters(current_user)
    shared_count = get_shared_count_map(pagination.items)
    shared_by_map = get_shared_by_map(pagination.items, current_user.id)

    return render_template(
        "archive/my_files.html",
        files=pagination.items,
        pagination=pagination,
        q=q,
        delegated_files=delegated_files,
        counters=counters,
        shared_count=shared_count,
        shared_by_map=shared_by_map,
        filters=filters,
        page_query=_page_query(filters),
        owners=User.query.order_by(User.email.asc()).all(),
        file_type_options=FILE_TYPE_OPTIONS,
        visibility_options=VISIBILITY_OPTIONS,
        workflow_status_options=WORKFLOW_STATUS_OPTIONS,
        workflow_results=workflow_results,
        archive_reset_endpoint="archive.my_files",
    )




# =========================
# Shared By Details
# =========================
@archive_bp.route("/shared-by/<int:file_id>")
@login_required
def shared_by(file_id):
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file or file.is_deleted:
        abort(404)

    perms = []
    if current_user.has_role("ADMIN") or current_user.id == file.owner_id:
        perms = (
            FilePermission.query
            .options(joinedload(FilePermission.user))
            .filter(FilePermission.file_id == file.id)
            .all()
        )
    else:
        perm = FilePermission.query.filter_by(file_id=file.id, user_id=current_user.id).first()
        if not perm:
            abort(403)
        perms = [perm]

    # Resolve sharers
    sharer_ids = {p.shared_by for p in perms if getattr(p, "shared_by", None)}
    sharers = {}
    if sharer_ids:
        for u in User.query.filter(User.id.in_(list(sharer_ids))).all():
            sharers[int(u.id)] = u

    return render_template(
        "archive/shared_by.html",
        file=file,
        perms=perms,
        sharers=sharers,
    )
# =========================
# Download
# =========================
@archive_bp.route("/download/<int:file_id>")
@login_required
def download_file(file_id):
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file or file.is_deleted:
        abort(403)

    perm = FilePermission.query.filter_by(
        file_id=file.id,
        user_id=current_user.id
    ).first()

    if perm and not perm.can_download:
        abort(403)

    disk_path = _archive_disk_path(file)
    if not disk_path:
        abort(404)

    return send_file(
        disk_path,
        as_attachment=True,
        download_name=file.original_name
    )


# =========================
# Preview (inline)
# =========================
@archive_bp.route("/preview/<int:file_id>")
@login_required
def preview_file(file_id):
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file or file.is_deleted:
        abort(404)

    perm = FilePermission.query.filter_by(
        file_id=file.id,
        user_id=current_user.id
    ).first()

    if perm and not perm.can_download:
        abort(403)

    return _render_archive_preview(file, "archive.download_file")


# =========================
# Share / Delegate
# =========================
@archive_bp.route("/share/<int:file_id>", methods=["GET", "POST"])
@login_required
def share_file(file_id):
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file or file.is_deleted:
        abort(403)

    is_delegated_user = (
        not current_user.has_role("ADMIN")
        and file.owner_id != current_user.id
    )

    can_delegate = (
        current_user.has_role("ADMIN")
        or file.owner_id == current_user.id
        or FilePermission.query.filter_by(
            file_id=file.id,
            user_id=current_user.id,
            can_share=True
        ).first()
    )

    if not can_delegate:
        abort(403)

    users = [
        user
        for user in User.query.filter(User.id != file.owner_id).all()
        if can_user_access_archived_file_confidentiality(user, file.id)
    ]

    shared_user_ids = [
        p.user_id
        for p in FilePermission.query.filter_by(file_id=file.id).all()
    ]

    if request.method == "POST":
        selected_users = request.form.getlist("users")

        allowed_share_ids = {int(user.id) for user in users}
        requested_share_ids = {
            int(user_id) for user_id in selected_users if str(user_id).isdigit()
        }
        if not requested_share_ids.issubset(allowed_share_ids):
            flash(
                "لا يمكن مشاركة ملف معاملة سرية مع مستخدم غير موجود ضمن الأشخاص المخولين.",
                "danger",
            )
            return redirect(url_for("archive.share_file", file_id=file.id))

        if current_user.has_role("ADMIN") or file.owner_id == current_user.id:
            FilePermission.query.filter_by(file_id=file.id).delete()
        else:
            FilePermission.query.filter_by(
                file_id=file.id,
                user_id=current_user.id
            ).delete()

        for uid in selected_users:
            if is_delegated_user and request.form.get(f"can_share_{uid}") == "1":
                db.session.add(
                    AuditLog(
                        action="ARCHIVE_SHARE_DENIED",
                        user_id=current_user.id,
                        target_type="ARCHIVE_FILE",
                        target_id=file.id,
                        note="Delegated user attempted to re-delegate sharing"
                    )
                )
                continue

            expires_raw = request.form.get(f"expires_at_{uid}")
            expires_at = (
                datetime.strptime(expires_raw, "%Y-%m-%d")
                if expires_raw else None
            )

            permission = FilePermission(
                file_id=file.id,
                user_id=int(uid),
                can_download=(request.form.get(f"can_download_{uid}") == "1"),
                can_share=(
                    not is_delegated_user
                    and request.form.get(f"can_share_{uid}") == "1"
                ),
                delegated_by=current_user.id if is_delegated_user else None,
                shared_by=current_user.id,
                expires_at=expires_at
            )

            db.session.add(permission)

            db.session.add(
                AuditLog(
                    action="ARCHIVE_SHARE",
                    user_id=current_user.id,
                    target_type="ARCHIVE_FILE",
                    target_id=file.id,
                    note=(
                        f"Shared with user {uid} | "
                        f"download={permission.can_download} | "
                        f"can_share={permission.can_share} | "
                        f"expires={permission.expires_at}"
                    )
                )
            )

        file.visibility = "shared"

        # Notify each shared user with full details
        for uid in selected_users:
            try:
                uid_int = int(uid)
            except Exception:
                continue

            perm = FilePermission.query.filter_by(file_id=file.id, user_id=uid_int).first()
            if not perm:
                continue

            expires_label = perm.expires_at.strftime("%Y-%m-%d") if perm.expires_at else "بدون"
            emit_event(
                actor_id=current_user.id,
                action="ARCHIVE_FILE_SHARED",
                message=(
                    f"تمت مشاركة ملف معك: {file.original_name} (ID: {file.id}) | "
                    f"من: {current_user.email} | "
                    f"تحميل={'نعم' if perm.can_download else 'لا'} | "
                    f"مشاركة={'نعم' if perm.can_share else 'لا'} | "
                    f"انتهاء={expires_label}"
                ),
                target_type="ARCHIVE_FILE",
                target_id=file.id,
                notify_user_id=uid_int,
                level="INFO",
                track_for_actor=True,
                auto_commit=False,
            )

        db.session.commit()

        flash("تمت مشاركة الملف بنجاح", "success")
        return redirect(url_for("archive.my_files"))

    return render_template(
        "archive/share.html",
        file=file,
        users=users,
        shared_user_ids=shared_user_ids
    )


# =========================
# Delete / Restore / Audit
# =========================
@archive_bp.route("/delete/<int:file_id>", methods=["POST"])
@login_required
def delete_file(file_id):
    file = ArchivedFile.query.get_or_404(file_id)
    is_super_admin = _is_super_admin(current_user)

    # Only the uploader (owner) or ADMIN can delete
    if not (is_super_admin or current_user.has_role("ADMIN") or file.owner_id == current_user.id):
        abort(403)

    # Safety rules
    if file.is_deleted:
        flash("الملف محذوف مسبقًا", "warning")
        return redirect(url_for("archive.my_files"))

    # Signed files are protected, except when deleted explicitly by SUPER_ADMIN.
    if getattr(file, "is_signed", False) and not is_super_admin:
        flash("لا يمكن حذف ملف موقّع.", "danger")
        return redirect(url_for("archive.file_details", file_id=file.id))

    # Do not allow deleting files attached to workflow requests
    attached = RequestAttachment.query.filter_by(archived_file_id=file.id).first()
    if attached:
        flash("لا يمكن حذف ملف مرتبط بطلب/مسار عمل. قم بإزالة الربط أولاً.", "danger")
        return redirect(url_for("archive.file_details", file_id=file.id))

    # If shared, the owner must unshare first; ADMIN/SUPER_ADMIN can override.
    shared_count = FilePermission.query.filter_by(file_id=file.id).count()
    if shared_count > 0 and not (is_super_admin or current_user.has_role("ADMIN")):
        flash("لا يمكن حذف ملف تمت مشاركته. قم بإلغاء المشاركة أولاً.", "danger")
        return redirect(url_for("archive.file_details", file_id=file.id))

    file.is_deleted = True
    file.deleted_at = datetime.utcnow()
    file.deleted_by = current_user.id

    db.session.add(
        AuditLog(
            action="ARCHIVE_DELETE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            note=f"File '{file.original_name}' soft deleted"
        )
    )

    db.session.commit()
    flash("تم نقل الملف إلى سلة المحذوفات", "warning")
    return redirect(url_for("archive.my_files"))


@archive_bp.route("/recycle-bin")
@login_required
def recycle_bin():
    q = ArchivedFile.query.filter(
        ArchivedFile.is_deleted.is_(True),
        (ArchivedFile.is_final_deleted.is_(False) | ArchivedFile.is_final_deleted.is_(None))
    )

    if not current_user.has_role("ADMIN"):
        q = q.filter(ArchivedFile.owner_id == current_user.id)


    files = q.order_by(ArchivedFile.deleted_at.desc()).all()

    return render_template(
        "archive/recycle_bin.html",
        files=files,
        trash_retention_days=get_trash_retention_days(),
    )


# =========================
# Super Trash (Final Deleted)
# =========================
@archive_bp.route("/super-trash")
@login_required
def super_trash():
    """List final-deleted files. Visible only to SUPER_ADMIN."""
    if not _is_super_admin(current_user):
        abort(403)

    q = (request.args.get("q") or "").strip()
    page = request.args.get("page", 1, type=int)

    query = (
        ArchivedFile.query
        .options(joinedload(ArchivedFile.owner))
        .filter(ArchivedFile.is_final_deleted.is_(True))
    )

    if q:
        query = query.filter(
            or_(
                ArchivedFile.original_name.ilike(f"%{q}%"),
                ArchivedFile.description.ilike(f"%{q}%")
            )
        )

    pagination = (
        query
        .order_by(ArchivedFile.final_deleted_at.desc(), ArchivedFile.deleted_at.desc())
        .paginate(page=page, per_page=20, error_out=False)
    )

    return render_template(
        "archive/super_trash.html",
        files=pagination.items,
        pagination=pagination,
        q=q,
    )


@archive_bp.route("/super-trash/download/<int:file_id>")
@login_required
def super_trash_download(file_id):
    if not _is_super_admin(current_user):
        abort(403)

    f = ArchivedFile.query.get_or_404(file_id)
    if not getattr(f, "is_final_deleted", False):
        abort(404)

    disk_path = _archive_disk_path(f)
    if not disk_path:
        flash("ملف التخزين غير موجود على القرص.", "danger")
        return redirect(url_for("archive.super_trash"))

    return send_file(
        disk_path,
        as_attachment=True,
        download_name=f.original_name,
        mimetype=f.mime_type or "application/octet-stream",
    )


@archive_bp.route("/super-trash/preview/<int:file_id>")
@login_required
def super_trash_preview(file_id):
    if not _is_super_admin(current_user):
        abort(403)

    f = ArchivedFile.query.get_or_404(file_id)
    if not getattr(f, "is_final_deleted", False):
        abort(404)

    if not _archive_disk_path(f):
        flash("ملف التخزين غير موجود على القرص.", "danger")
        return redirect(url_for("archive.super_trash"))

    return _render_archive_preview(f, "archive.super_trash_download")


@archive_bp.route("/super-trash/restore-to-bin/<int:file_id>", methods=["POST"])
@login_required
def super_trash_restore_to_bin(file_id):
    if not _is_super_admin(current_user):
        abort(403)

    f = ArchivedFile.query.get_or_404(file_id)
    if not getattr(f, "is_final_deleted", False):
        abort(404)

    f.is_final_deleted = False
    f.final_deleted_at = None
    f.final_deleted_by = None

    # keep it in recycle bin
    f.is_deleted = True
    if not f.deleted_at:
        f.deleted_at = datetime.utcnow()

    db.session.add(
        AuditLog(
            action="ARCHIVE_SUPERTRASH_RESTORE_TO_BIN",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=f.id,
            note=f"File '{f.original_name}' restored from Super Trash to Recycle Bin"
        )
    )

    db.session.commit()
    flash("✅ تم استعادة الملف إلى سلة المحذوفات.", "success")
    return redirect(url_for("archive.super_trash"))


@archive_bp.route("/super-trash/restore-active/<int:file_id>", methods=["POST"])
@login_required
def super_trash_restore_active(file_id):
    if not _is_super_admin(current_user):
        abort(403)

    f = ArchivedFile.query.get_or_404(file_id)
    if not getattr(f, "is_final_deleted", False):
        abort(404)

    f.is_final_deleted = False
    f.final_deleted_at = None
    f.final_deleted_by = None

    # restore active
    f.is_deleted = False
    f.deleted_at = None
    f.deleted_by = None

    db.session.add(
        AuditLog(
            action="ARCHIVE_SUPERTRASH_RESTORE_ACTIVE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=f.id,
            note=f"File '{f.original_name}' restored from Super Trash to Active"
        )
    )

    db.session.commit()
    flash("✅ تم استعادة الملف إلى الملفات النشطة.", "success")
    return redirect(url_for("archive.super_trash"))

@archive_bp.route("/recycle-bin/purge", methods=["POST"])
@login_required
def purge_recycle_bin():
    """Move expired recycle-bin files to Super Trash (final delete).

    Behavior:
    - Files disappear from users/admin lists once final-deleted.
    - Files remain accessible only to SUPER_ADMIN via /archive/super-trash.
    - Files linked to workflow requests are never final-deleted.
    """

    if not current_user.has_role("ADMIN"):
        abort(403)

    days = get_trash_retention_days()
    cutoff = datetime.utcnow() - timedelta(days=days)

    candidates = (
        ArchivedFile.query
        .filter(
            ArchivedFile.is_deleted.is_(True),
            (ArchivedFile.is_final_deleted.is_(False) | ArchivedFile.is_final_deleted.is_(None)),
            ArchivedFile.deleted_at.isnot(None),
            ArchivedFile.deleted_at < cutoff
        )
        .order_by(ArchivedFile.deleted_at.asc())
        .all()
    )

    moved = 0
    skipped = 0

    for f in candidates:
        attached = RequestAttachment.query.filter_by(archived_file_id=f.id).first()
        if attached:
            skipped += 1
            continue

        # Remove sharing permissions first
        FilePermission.query.filter_by(file_id=f.id).delete(synchronize_session=False)

        f.is_final_deleted = True
        f.final_deleted_at = datetime.utcnow()
        f.final_deleted_by = current_user.id

        db.session.add(
            AuditLog(
                action="ARCHIVE_FINAL_DELETE_RETENTION",
                user_id=current_user.id,
                target_type="ARCHIVE_FILE",
                target_id=f.id,
                note=f"File '{f.original_name}' moved to Super Trash (retention {days} days)"
            )
        )

        moved += 1

    db.session.commit()

    if moved:
        flash(f"✅ تم نقل {moved} ملف/ملفات إلى سلة السوبر أدمن (سياسة الاحتفاظ {days} يوم)", "success")
    else:
        flash("لا توجد ملفات منتهية للنقل إلى سلة السوبر أدمن حاليًا.", "info")

    if skipped:
        flash(f"⚠️ تم تجاوز {skipped} ملف لأنه مرتبط بطلب/مسار عمل.", "warning")

    return redirect(url_for("archive.recycle_bin"))


@archive_bp.route("/recycle-bin/purge/<int:file_id>", methods=["POST"])
@login_required
def purge_single_from_recycle_bin(file_id):
    """Move a single file from recycle bin to Super Trash (final delete).

    Allowed for ADMIN or the file owner.
    Safety rules:
      - Must be already soft-deleted (is_deleted=True)
      - Cannot final-delete files linked to workflow requests
      - If shared, owner must unshare first (ADMIN can override)

    NOTE: This does NOT delete the DB record or the physical file.
    """

    f = ArchivedFile.query.get_or_404(file_id)

    if not (current_user.has_role("ADMIN") or f.owner_id == current_user.id):
        abort(403)

    if not f.is_deleted:
        flash("الملف ليس في سلة المحذوفات.", "warning")
        return redirect(url_for("archive.file_details", file_id=f.id))

    if getattr(f, 'is_final_deleted', False):
        flash("هذا الملف موجود بالفعل في سلة السوبر أدمن.", "info")
        return redirect(url_for("archive.recycle_bin"))

    # Never final-delete files linked to workflow requests
    attached = RequestAttachment.query.filter_by(archived_file_id=f.id).first()
    if attached:
        flash("لا يمكن حذف الملف نهائيًا لأنه مرتبط بطلب/مسار عمل.", "danger")
        return redirect(url_for("archive.recycle_bin"))

    # If shared with others, owner must unshare first (admin can override)
    shared_count = FilePermission.query.filter_by(file_id=f.id).count()
    if shared_count > 0 and not current_user.has_role("ADMIN"):
        flash("لا يمكن حذف ملف تمت مشاركته. قم بإلغاء المشاركة أولاً.", "danger")
        return redirect(url_for("archive.recycle_bin"))

    # Remove sharing permissions first
    FilePermission.query.filter_by(file_id=f.id).delete(synchronize_session=False)

    f.is_final_deleted = True
    f.final_deleted_at = datetime.utcnow()
    f.final_deleted_by = current_user.id

    db.session.add(
        AuditLog(
            action="ARCHIVE_FINAL_DELETE_SINGLE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=f.id,
            note=f"File '{f.original_name}' moved to Super Trash (manual final delete)"
        )
    )

    db.session.commit()
    flash("✅ تم نقل الملف إلى سلة السوبر أدمن (حذف نهائي).", "success")
    return redirect(url_for("archive.recycle_bin"))


@archive_bp.route("/restore/<int:file_id>")
@login_required
def restore_file(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

    if getattr(file, 'is_final_deleted', False):
        abort(404)

    if not (current_user.has_role("ADMIN") or file.owner_id == current_user.id):
        abort(403)

    file.is_deleted = False
    file.deleted_at = None
    file.deleted_by = None

    db.session.add(
        AuditLog(
            action="ARCHIVE_RESTORE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            note=f"File '{file.original_name}' restored"
        )
    )

    db.session.commit()
    flash("تمت استعادة الملف بنجاح", "success")
    return redirect(url_for("archive.recycle_bin"))


@archive_bp.route("/audit-log")
@login_required
def archive_audit_log():
    if not current_user.has_role("ADMIN"):
        abort(403)

    logs = AuditLog.query.filter(
        AuditLog.action.in_([
            "ARCHIVE_DELETE",
            "ARCHIVE_RESTORE",
            "ARCHIVE_SHARE",
            "ARCHIVE_UNSHARE",
            "ARCHIVE_SIGNED",
            "ARCHIVE_SHARE_DENIED"
        ])
    ).order_by(
        AuditLog.created_at.desc()
    ).all()

    return render_template(
        "archive/audit_log.html",
        logs=logs
    )


@archive_bp.route("/unshare/<int:permission_id>", methods=["POST"])
@login_required
def unshare_file(permission_id):
    permission = FilePermission.query.get_or_404(permission_id)
    file = ArchivedFile.query.get_or_404(permission.file_id)

    if not (
        current_user.has_role("ADMIN")
        or file.owner_id == current_user.id
        or permission.delegated_by == current_user.id
    ):
        abort(403)

    target_user = User.query.get(permission.user_id)

    db.session.delete(permission)

    db.session.add(
        AuditLog(
            action="ARCHIVE_UNSHARE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            note=f"تم إلغاء مشاركة الملف مع المستخدم {target_user.email}"
        )
    )

    db.session.commit()

    flash("تم إلغاء المشاركة بنجاح", "success")
    return redirect(url_for("archive.file_details", file_id=file.id))


@archive_bp.route("/shared-files")
@login_required
def shared_files():
    if not current_user.has_role("ADMIN"):
        abort(403)

    permissions = (
        FilePermission.query
        .join(ArchivedFile, ArchivedFile.id == FilePermission.file_id)
        .join(User, User.id == FilePermission.user_id)
        .order_by(FilePermission.id.desc())
        .all()
    )

    return render_template(
        "archive/shared_files.html",
        permissions=permissions
    )
