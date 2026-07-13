# workflow/routes.py

import os
import re
import uuid
import json
import time
import logging
import mimetypes
import math
from io import BytesIO
from datetime import datetime, timedelta
from urllib.parse import quote

from flask import (
    send_file, abort, render_template,
    request, redirect, url_for,
    flash, jsonify, Response, stream_with_context
)
from flask_login import login_required, current_user

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph,
    Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors

from sqlalchemy import func, update, or_
from sqlalchemy.orm import joinedload

from . import workflow_bp
from extensions import db
from permissions import roles_required
from utils.perms import perm_required
from utils.permissions import can_access_request, get_effective_user, get_active_delegation, get_active_delegations
from utils.audit_helpers import delegation_audit_fields
from utils.events import emit_event
from utils.ui_labels import ui_label, ui_text

from models import (
    WorkflowRequest,
    ArchivedFile,
    AuditLog,
    Notification,
    PortalCircular,
    User,
    Message,
    MessageRecipient,
    WorkflowTemplate,
    WorkflowTemplateStep,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowStepTask,
    RequestEscalation,
    RequestAttachment,
    Approval,
    RequestType,
    WorkflowRoutingRule,
    Role,
    Department,
    Directorate,
    Unit,
    Section,
    Division,
    Organization,
    Committee,
    CommitteeAssignee,

    OrgUnitManager,

    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,

)

from utils.org_dynamic import resolve_user_org_node_id, get_node_ancestor_ids

from workflow.engine import (
    start_workflow_for_request,
    decide_step,
    bypass_parallel_task,
    bypass_all_parallel_tasks,
    ensure_parallel_tasks,
)

logger = logging.getLogger(__name__)


# =========================
# Role normalization helpers
# =========================
def _norm_role(value: str | None) -> str:
    """Normalize role codes to a stable comparison form.

    We use this to tolerate minor differences between stored template values
    and the user's role (spaces, hyphens, case).
    """
    s = (value or "").strip().lower()
    if not s:
        return ""
    s = s.replace("-", "_").replace(" ", "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def _role_variants(role: str | None) -> list[str]:
    """Generate a small set of acceptable string variants for SQL filters."""
    raw = (role or "").strip()
    if not raw:
        return []

    base = _norm_role(raw)
    variants = {
        raw,
        raw.lower(),
        raw.upper(),
        base,
        base.replace("_", " "),
        base.replace("_", "-"),
        base.replace("_", ""),
    }

    # Common: stored value might be with spaces while user role is with underscores
    if "_" in raw:
        variants.add(raw.replace("_", " "))
        variants.add(raw.replace("_", "-"))
    if "-" in raw:
        variants.add(raw.replace("-", "_"))
        variants.add(raw.replace("-", " "))
    if " " in raw:
        variants.add(raw.replace(" ", "_"))
        variants.add(raw.replace(" ", "-"))

    cleaned = [v for v in {str(v).strip() for v in variants} if v]
    return cleaned


MENTION_ACCESS_ACTION = "WORKFLOW_MENTION_ACCESS"
MENTION_TASK_NOTE = "تمت الإضافة عبر المنشن"


def _mention_tokens(text: str | None) -> list[str]:
    """Extract user/role mentions from workflow comments.

    Supported forms:
      - @email@example.com
      - @username or @display-name
      - @[display name with spaces]
      - @role:ROLE_CODE or @دور:ROLE_CODE
    """
    text = (text or "").strip()
    if not text:
        return []

    tokens: list[str] = []

    for m in re.finditer(r"@\[(.{1,160}?)\]", text):
        token = (m.group(1) or "").strip()
        if token:
            tokens.append(token)

    for m in re.finditer(r"(?<![\w@])@([^\s<>\[\]{}()\"']{1,160})", text):
        token = (m.group(1) or "").strip()
        token = token.strip(".,،؛;!?؟")
        if token:
            tokens.append(token)

    seen = set()
    out = []
    for token in tokens:
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _role_from_mention_token(token: str) -> str | None:
    token = (token or "").strip()
    low = token.casefold()
    prefixes = ("role:", "role=", "r:", "دور:", "دور=")
    for prefix in prefixes:
        if low.startswith(prefix):
            raw = token[len(prefix):].strip()
            return raw or None
    return None


def _resolve_role_mention_users(role_token: str) -> list[User]:
    role_raw = (role_token or "").strip()
    if not role_raw:
        return []

    role_code = role_raw
    try:
        low = role_raw.casefold()
        role_row = (
            Role.query
            .filter(or_(
                func.lower(Role.code) == low,
                func.lower(Role.name_en) == low,
                Role.name_ar == role_raw,
            ))
            .first()
        )
        if role_row and getattr(role_row, "code", None):
            role_code = role_row.code
    except Exception:
        role_code = role_raw

    variants = _role_variants(role_code) + _role_variants(role_raw)
    variants = [v for v in {str(v).strip() for v in variants} if v]
    if not variants:
        return []

    return (
        User.query
        .filter(or_(*[User.role.ilike(v) for v in variants]))
        .order_by(User.email.asc())
        .all()
    )


def _resolve_user_mention_users(token: str) -> list[User]:
    raw = (token or "").strip()
    if not raw:
        return []

    for prefix in ("user:", "user=", "u:", "مستخدم:", "مستخدم="):
        if raw.casefold().startswith(prefix):
            raw = raw[len(prefix):].strip()
            break

    if not raw:
        return []

    if raw.isdigit():
        u = User.query.get(int(raw))
        return [u] if u else []

    low = raw.casefold()
    exact = (
        User.query
        .filter(or_(
            func.lower(User.email) == low,
            func.lower(User.name) == low,
        ))
        .first()
    )
    if exact:
        return [exact]

    # If the user mentioned the email local part, resolve it conservatively.
    local_part_matches = []
    if "@" not in raw and len(raw) >= 3:
        local_part_matches = (
            User.query
            .filter(func.lower(User.email).like(f"{low}@%"))
            .order_by(User.email.asc())
            .limit(10)
            .all()
        )
        if len(local_part_matches) == 1:
            return local_part_matches

    # Bracketed names often contain spaces. Keep this bounded to avoid surprises.
    if len(raw) >= 3:
        return (
            User.query
            .filter(or_(
                User.name.ilike(f"%{raw}%"),
                User.email.ilike(f"%{raw}%"),
            ))
            .order_by(User.email.asc())
            .limit(10)
            .all()
        )

    return []


def _resolve_workflow_mentions(text: str | None) -> tuple[list[User], list[str]]:
    users_by_id: dict[int, User] = {}
    unresolved: list[str] = []

    for token in _mention_tokens(text):
        role_token = _role_from_mention_token(token)
        found = _resolve_role_mention_users(role_token) if role_token else _resolve_user_mention_users(token)
        found = [u for u in (found or []) if u and getattr(u, "id", None)]
        if not found:
            unresolved.append(token)
            continue
        for u in found:
            users_by_id[int(u.id)] = u

    return list(users_by_id.values()), unresolved


@workflow_bp.route("/mentions/search")
@login_required
def mention_search():
    q = (request.args.get("q") or "").strip()
    limit = request.args.get("limit", 8, type=int) or 8
    limit = max(1, min(limit, 12))

    role_query = q
    q_low = q.casefold()
    for prefix in ("role:", "role=", "r:", "دور:", "دور="):
        if q_low.startswith(prefix):
            role_query = q[len(prefix):].strip()
            break
    else:
        if q_low in {"role", "roles", "r", "دور", "ادوار", "أدوار"}:
            role_query = ""

    results = []

    try:
        user_query = User.query
        if q:
            like = f"%{q}%"
            user_query = user_query.filter(or_(
                User.email.ilike(like),
                User.name.ilike(like),
                User.role.ilike(like),
                User.job_title.ilike(like),
            ))
        users = user_query.order_by(User.name.asc(), User.email.asc()).limit(limit).all()
        for u in users:
            email = (u.email or "").strip()
            label = (u.full_name or email or f"User #{u.id}").strip()
            results.append({
                "type": "user",
                "label": label,
                "detail": " · ".join([v for v in [email, (u.role or "").strip()] if v]),
                "insert": f"@{email} " if email else f"@user:{u.id} ",
            })
    except Exception:
        pass

    try:
        role_query_obj = Role.query.filter(Role.is_active.is_(True))
        if role_query:
            like = f"%{role_query}%"
            role_query_obj = role_query_obj.filter(or_(
                Role.code.ilike(like),
                Role.name_ar.ilike(like),
                Role.name_en.ilike(like),
            ))
        roles = role_query_obj.order_by(Role.name_ar.asc(), Role.code.asc()).limit(limit).all()
        for r in roles:
            code = (r.code or "").strip()
            if not code:
                continue
            label = (r.name_ar or r.name_en or code).strip()
            results.append({
                "type": "role",
                "label": label,
                "detail": code,
                "insert": f"@role:{code} ",
            })
    except Exception:
        pass

    return jsonify({"q": q, "results": results[: limit * 2]})


def _mentioned_user_ids_for_request(req_id: int) -> set[int]:
    rows = (
        db.session.query(AuditLog.target_id)
        .filter(
            AuditLog.request_id == int(req_id),
            AuditLog.action == MENTION_ACCESS_ACTION,
            AuditLog.target_type == "USER",
            AuditLog.target_id.isnot(None),
        )
        .all()
    )
    out: set[int] = set()
    for (uid,) in rows:
        try:
            out.add(int(uid))
        except Exception:
            pass
    return out


def _send_mention_internal_message(req: WorkflowRequest, user: User, note: str | None, *, step_order: int | None) -> None:
    uid = int(getattr(user, "id", 0) or 0)
    if not uid:
        return

    link = _build_absolute_url(url_for("workflow.view_request", request_id=req.id))
    actor_label = current_user.full_name or current_user.email or f"#{current_user.id}"
    note_text = (note or "").strip()
    if len(note_text) > 1200:
        note_text = note_text[:1200].rstrip() + "..."

    body_lines = [
        f"قام {actor_label} بإضافتك إلى متابعة مسار الطلب #{req.id} عبر المنشن.",
        "",
        f"العنوان: {req.title or '-'}",
        f"الحالة: {ui_label(req.status) or req.status or '-'}",
    ]
    if step_order:
        body_lines.append(f"الخطوة الحالية: {step_order}")
    if note_text:
        body_lines.extend(["", "نص التعليق:", note_text])
    body_lines.extend(["", "رابط فتح المسار:", link])

    msg = Message(
        sender_id=current_user.id,
        subject=f"تمت إضافتك إلى مسار الطلب #{req.id}",
        body="\n".join(body_lines),
        target_kind="USER",
        target_id=uid,
        created_at=datetime.utcnow(),
        reply_to_id=None,
    )
    db.session.add(msg)
    db.session.flush()
    db.session.add(MessageRecipient(
        message_id=msg.id,
        recipient_user_id=uid,
        is_read=False,
        read_at=None,
        is_deleted=False,
        deleted_at=None,
    ))


def _grant_mention_access(req: WorkflowRequest, inst: WorkflowInstance | None, note: str, *, step_order: int | None) -> tuple[list[User], list[str]]:
    """Grant request visibility to users mentioned in a comment.

    Mentions are intentionally modeled as audit-backed participants instead of
    changing the template. Mentioned users also get a pending task on the
    current step so the request appears in "مهامي".
    """
    mentioned_users, unresolved = _resolve_workflow_mentions(note)
    if not mentioned_users:
        return [], unresolved

    existing_ids = _mentioned_user_ids_for_request(req.id)
    added: list[User] = []

    current_step = None
    if inst and step_order:
        try:
            current_step = WorkflowInstanceStep.query.filter_by(
                instance_id=inst.id,
                step_order=int(step_order),
            ).first()
        except Exception:
            current_step = None

    add_current_step_task = bool(
        current_step
        and getattr(current_step, "status", None) == "PENDING"
        and inst
        and not getattr(inst, "is_completed", False)
    )

    now = datetime.utcnow()
    for user in mentioned_users:
        uid = int(user.id)
        if uid == int(current_user.id):
            continue

        if uid not in existing_ids:
            db.session.add(AuditLog(
                request_id=req.id,
                user_id=current_user.id,
                action=MENTION_ACCESS_ACTION,
                old_status=req.status,
                new_status=req.status,
                note=f"step={step_order or ''} | المستخدم المشار إليه={user.full_name}",
                target_type="USER",
                target_id=uid,
                created_at=now,
            ))
            existing_ids.add(uid)
            added.append(user)

        task_created = False
        if add_current_step_task and current_step:
            task = WorkflowStepTask.query.filter_by(
                instance_id=inst.id,
                step_order=current_step.step_order,
                assignee_user_id=uid,
            ).first()
            if not task:
                db.session.add(WorkflowStepTask(
                    instance_id=inst.id,
                    request_id=req.id,
                    step_order=current_step.step_order,
                    assignee_user_id=uid,
                    status="PENDING",
                    response="NONE",
                    note=MENTION_TASK_NOTE,
                    created_at=now,
                ))
                task_created = True

        _send_mention_internal_message(req, user, note, step_order=step_order)

        emit_event(
            actor_id=current_user.id,
            action=MENTION_ACCESS_ACTION,
            message=f"تمت إضافتك إلى متابعة المسار #{req.id} بواسطة {current_user.full_name or current_user.email}.",
            target_type="WorkflowRequest",
            target_id=req.id,
            notify_user_id=uid,
            level="WORKFLOW",
            track_for_actor=True,
            auto_commit=False,
        )

        if task_created and user not in added:
            added.append(user)

    return added, unresolved


def _get_effective_directorate_id(user) -> int | None:
    """Return user's directorate id.

    Priority:
      1) user.directorate_id (explicit assignment)
      2) derived from user's department_id
    """
    try:
        uid = getattr(user, "directorate_id", None)
        if uid is not None and str(uid).isdigit():
            return int(uid)
    except Exception:
        pass

    try:
        dept_id = getattr(user, "department_id", None)
        if dept_id is not None and str(dept_id).isdigit():
            dept = Department.query.get(int(dept_id))
            if dept and getattr(dept, "directorate_id", None) is not None:
                return int(dept.directorate_id)
    except Exception:
        pass
    return None



# =========================
# Escalation helpers
# =========================

def _build_absolute_url(path_: str) -> str:
    """Build an absolute URL for the current host (best-effort)."""
    try:
        root = (request.url_root or "").rstrip("/")
        return root + path_
    except Exception:
        return path_


def _step_target_label(step, users_map=None, depts_map=None, dirs_map=None, units_map=None, sections_map=None, divisions_map=None, committees_map=None) -> str:
    """Human readable label for where the step is routed."""
    try:
        kind = (getattr(step, 'approver_kind', None) or '').upper()
    except Exception:
        kind = ''

    if kind == 'USER':
        uid = getattr(step, 'approver_user_id', None)
        try:
            uid_int = int(uid) if uid is not None else None
        except Exception:
            uid_int = None
        u = users_map.get(uid_int) if (users_map and uid_int) else None
        return f"مستخدم: {(getattr(u, 'email', None) if u else ('#' + str(uid_int)))}"

    if kind == 'ROLE':
        return f"دور: {getattr(step, 'approver_role', '-') or '-'}"

    if kind == 'DIRECTORATE':
        did = getattr(step, 'approver_directorate_id', None)
        try:
            did_int = int(did) if did is not None else None
        except Exception:
            did_int = None
        d = dirs_map.get(did_int) if (dirs_map and did_int) else None
        name = getattr(d, 'name_ar', None) if d else (f"#{did_int}" if did_int else '-')
        return f"إدارة: {name} (directorate_head)"

    if kind == 'UNIT':
        uid = getattr(step, 'approver_unit_id', None)
        try:
            uid_int = int(uid) if uid is not None else None
        except Exception:
            uid_int = None
        uo = units_map.get(uid_int) if (units_map and uid_int) else None
        name = getattr(uo, 'name_ar', None) if uo else (f"#{uid_int}" if uid_int else '-')
        return f"وحدة: {name} (manager)"

    if kind == 'SECTION':
        sid = getattr(step, 'approver_section_id', None)
        try:
            sid_int = int(sid) if sid is not None else None
        except Exception:
            sid_int = None
        so = sections_map.get(sid_int) if (sections_map and sid_int) else None
        name = getattr(so, 'name_ar', None) if so else (f"#{sid_int}" if sid_int else '-')
        return f"قسم: {name} (manager)"

    if kind == 'DIVISION':
        vid = getattr(step, 'approver_division_id', None)
        try:
            vid_int = int(vid) if vid is not None else None
        except Exception:
            vid_int = None
        vo = divisions_map.get(vid_int) if (divisions_map and vid_int) else None
        name = getattr(vo, 'name_ar', None) if vo else (f"#{vid_int}" if vid_int else '-')
        return f"شعبة: {name} (manager)"

    if kind == 'COMMITTEE':
        cid = getattr(step, 'approver_committee_id', None)
        try:
            cid_int = int(cid) if cid is not None else None
        except Exception:
            cid_int = None
        co = committees_map.get(cid_int) if (committees_map and cid_int) else None
        name = getattr(co, 'name_ar', None) if co else (f"#{cid_int}" if cid_int else '-')
        return f"لجنة: {name}"


    # DEPARTMENT (default)
    depid = getattr(step, 'approver_department_id', None)
    try:
        depid_int = int(depid) if depid is not None else None
    except Exception:
        depid_int = None
    dep = depts_map.get(depid_int) if (depts_map and depid_int) else None
    name = getattr(dep, 'name_ar', None) if dep else (f"#{depid_int}" if depid_int else '-')
    return f"دائرة: {name} (dept_head)"


def _infer_directorate_id_for_step(req, step) -> int | None:
    """Infer directorate id for escalation CC (best-effort)."""
    try:
        if (getattr(step, 'approver_kind', '') or '').upper() == 'DIRECTORATE' and getattr(step, 'approver_directorate_id', None):
            return int(step.approver_directorate_id)
    except Exception:
        pass

    try:
        if (getattr(step, 'approver_kind', '') or '').upper() == 'DEPARTMENT' and getattr(step, 'approver_department_id', None):
            dept = Department.query.get(int(step.approver_department_id))
            if dept and getattr(dept, 'directorate_id', None) is not None:
                return int(dept.directorate_id)
    except Exception:
        pass

    try:
        if (getattr(step, 'approver_kind', '') or '').upper() == 'USER' and getattr(step, 'approver_user_id', None):
            u = User.query.get(int(step.approver_user_id))
            if u:
                did = _get_effective_directorate_id(u)
                if did:
                    return int(did)
    except Exception:
        pass

    try:
        if req and getattr(req, 'requester', None):
            did = _get_effective_directorate_id(req.requester)
            if did:
                return int(did)
    except Exception:
        pass

    return None


def _directorate_head_user_ids(directorate_id: int | None) -> list[int]:
    """Return directorate_head users for a directorate id."""
    if not directorate_id:
        return []

    role_vars = _role_variants('directorate_head')

    # departments belonging to this directorate (for users without explicit directorate_id)
    dept_ids: list[int] = []
    try:
        dept_ids = [int(did) for (did,) in (
            db.session.query(Department.id)
            .filter(Department.directorate_id == int(directorate_id))
            .all()
        )]
    except Exception:
        dept_ids = []

    q = User.query
    if role_vars:
        q = q.filter(or_(*[User.role.ilike(v) for v in role_vars]))
    else:
        q = q.filter(User.role.ilike('directorate_head'))

    if dept_ids:
        q = q.filter(or_(User.directorate_id == int(directorate_id), User.department_id.in_(dept_ids)))
    else:
        q = q.filter(User.directorate_id == int(directorate_id))

    users = q.all()
    return sorted({int(u.id) for u in users if u and getattr(u, 'id', None)})


def _step_actor_user_ids(step) -> list[int]:
    """Return user ids that can act on the given step (best-effort)."""
    try:
        kind = (getattr(step, 'approver_kind', None) or '').upper()
    except Exception:
        kind = ''

    if kind == 'USER':
        try:
            return [int(step.approver_user_id)] if getattr(step, 'approver_user_id', None) else []
        except Exception:
            return []

    if kind == 'ROLE':
        role = (getattr(step, 'approver_role', None) or '').strip()
        if not role:
            return []
        vars_ = _role_variants(role)
        q = User.query
        if vars_:
            q = q.filter(or_(*[User.role.ilike(v) for v in vars_]))
        else:
            q = q.filter(User.role.ilike(role))
        users = q.all()
        return sorted({int(u.id) for u in users if u and getattr(u, 'id', None)})

    if kind == 'DEPARTMENT':
        if not getattr(step, 'approver_department_id', None):
            return []
        vars_ = _role_variants('dept_head')
        q = User.query.filter(User.department_id == int(step.approver_department_id))
        if vars_:
            q = q.filter(or_(*[User.role.ilike(v) for v in vars_]))
        else:
            q = q.filter(User.role.ilike('dept_head'))
        users = q.all()
        return sorted({int(u.id) for u in users if u and getattr(u, 'id', None)})

    if kind == 'DIRECTORATE':
        if not getattr(step, 'approver_directorate_id', None):
            return []

        role_vars = _role_variants('directorate_head') + _role_variants('directorate_deputy')
        role_vars = sorted({v for v in role_vars if v})

        dept_ids: list[int] = []
        try:
            dept_ids = [int(did) for (did,) in (
                db.session.query(Department.id)
                .filter(Department.directorate_id == int(step.approver_directorate_id))
                .all()
            )]
        except Exception:
            dept_ids = []

        q = User.query
        if role_vars:
            q = q.filter(or_(*[User.role.ilike(v) for v in role_vars]))

        if dept_ids:
            q = q.filter(or_(User.directorate_id == int(step.approver_directorate_id), User.department_id.in_(dept_ids)))
        else:
            q = q.filter(User.directorate_id == int(step.approver_directorate_id))

        users = q.all()
        return sorted({int(u.id) for u in users if u and getattr(u, 'id', None)})

    return []
# =========================
# Storage (same as archive)
# =========================
BASE_STORAGE = os.path.join(os.getcwd(), "storage", "archive")

ALLOWED_EXTENSIONS = {
    # Documents
    "pdf", "txt", "rtf",
    "doc", "docx", "odt",
    "xls", "xlsx", "ods", "csv",
    "ppt", "pptx", "odp",

    # Images
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff",

    # Archives (common in institutions)
    "zip", "rar", "7z",

    # Audio/Video (optional but common)
    "mp3", "wav", "m4a", "mp4", "mov", "avi",
    # html, web, sql, dll
    "html", "css", "js", "py", "java", "php", "sql", "db", "dll",
}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# =========================
# Helpers
# =========================
def _sanitize_original_name(name: str) -> str:
    """Best-effort sanitize for display name (keep Arabic, remove path traversal/nulls)."""
    if not name:
        return ""
    name = (name or "").strip()
    name = os.path.basename(name).replace("\x00", "")
    return name.strip()


def _save_upload_to_archive(file_storage, *, owner_id: int, visibility: str = "workflow", description: str | None = None):
    """Save a Werkzeug FileStorage into storage/archive and return (ArchivedFile, saved_path)."""
    if not file_storage or not getattr(file_storage, "filename", None):
        raise ValueError("No file selected")

    original_name = _sanitize_original_name(file_storage.filename)
    if not original_name:
        raise ValueError("Invalid file name")

    if "." not in original_name:
        raise ValueError("File has no extension")

    if not allowed_file(original_name):
        raise ValueError("File type not allowed")

    ext = original_name.rsplit(".", 1)[1].lower().strip()
    if not ext:
        raise ValueError("Invalid extension")

    stored_name = f"{uuid.uuid4().hex}.{ext}"
    os.makedirs(BASE_STORAGE, exist_ok=True)
    saved_path = os.path.join(BASE_STORAGE, stored_name)

    file_storage.save(saved_path)

    archived = ArchivedFile(
        original_name=original_name,
        stored_name=stored_name,
        description=description,
        file_path=saved_path,
        mime_type=getattr(file_storage, "mimetype", None),
        file_size=os.path.getsize(saved_path),
        owner_id=owner_id,
        visibility=visibility,
    )
    return archived, saved_path


def _parse_attachment_meta(note: str | None):
    """Parse meta from AuditLog.note, expecting tokens like 'step=3' and 'source=COMMENT'."""
    step = None
    source = None
    if not note:
        return step, source
    try:
        parts = [p.strip() for p in str(note).split("|")]
        for p in parts:
            if p.lower().startswith("step="):
                v = p.split("=", 1)[1].strip()
                if v and v.isdigit():
                    step = int(v)
            elif p.lower().startswith("source="):
                source = p.split("=", 1)[1].strip() or None
    except Exception:
        return None, None
    return step, source


def _audit_attachment(*, req_id: int, file_id: int, step_order: int | None, source: str, original_name: str, uploaded_by_id: int):
    """Write a consistent audit log line for an attachment so we can group by step without DB changes."""
    meta = f"file_id={file_id} | step={step_order if step_order is not None else ''} | source={source}"
    db.session.add(AuditLog(
        request_id=req_id,
        user_id=uploaded_by_id,
        action="WORKFLOW_ATTACHMENT_UPLOADED",
        old_status=None,
        new_status=None,
        note=f"Attachment: {original_name} | {meta}",
        target_type="ARCHIVE_FILE",
        target_id=file_id,
        created_at=datetime.utcnow(),
            **delegation_audit_fields()
        ))


def _guess_mime_for_file(f: ArchivedFile) -> str:
    """Best-effort MIME type for preview/headers."""
    mt = (getattr(f, "mime_type", None) or "").strip()
    if mt:
        return mt

    name = (getattr(f, "original_name", None) or getattr(f, "stored_name", None) or "").strip()
    guess, _ = mimetypes.guess_type(name)
    return guess or "application/octet-stream"


def _is_inline_previewable(mime: str) -> bool:
    mime = (mime or "").lower().strip()
    if not mime:
        return False
    if mime.startswith("image/"):
        return True
    if mime.startswith("text/"):
        return True
    if mime in {"application/pdf"}:
        return True
    return False

def _get_user_hierarchy(user):
    """Return (organization_id, directorate_id, department_id) for user, best-effort."""
    dept_id = getattr(user, "department_id", None)
    org_id = None

    # Prefer explicit directorate assignment on the user (helps directorate heads)
    dir_id = getattr(user, "directorate_id", None)
    try:
        if dir_id is not None and str(dir_id).isdigit():
            dir_id = int(dir_id)
        else:
            dir_id = None
    except Exception:
        dir_id = None

    # Fallback: derive from department
    if dir_id is None and dept_id:
        try:
            dept = Department.query.get(int(dept_id))
            if dept:
                dir_id = int(getattr(dept, "directorate_id", None) or 0) or None
        except Exception:
            dir_id = None

    # Derive organization from directorate if possible
    if dir_id:
        try:
            d = Directorate.query.get(int(dir_id))
            if d:
                org_id = d.organization_id
        except Exception:
            org_id = None

    return org_id, dir_id, dept_id


def _select_template_for(user, request_type_id: int):
    """Pick best WorkflowTemplate by routing rules for this user + request_type."""
    org_id, dir_id, dept_id = _get_user_hierarchy(user)

    user_node_id = None
    user_node_anc = set()
    try:
        user_node_id = resolve_user_org_node_id(user)
        if user_node_id:
            user_node_anc = get_node_ancestor_ids(int(user_node_id))
    except Exception:
        user_node_id = None
        user_node_anc = set()

    rules = (
        WorkflowRoutingRule.query
        .filter_by(request_type_id=request_type_id, is_active=True)
        .all()
    )

    candidates = []
    for r in rules:
        # Dynamic org node targeting (exact or subtree)
        if getattr(r, "org_node_id", None) is not None:
            if not user_node_id:
                continue
            if getattr(r, "match_subtree", False):
                if int(r.org_node_id) not in user_node_anc:
                    continue
            else:
                if int(r.org_node_id) != int(user_node_id):
                    continue

        if r.organization_id is not None and r.organization_id != org_id:
            continue
        if r.directorate_id is not None and r.directorate_id != dir_id:
            continue
        if r.department_id is not None and r.department_id != dept_id:
            continue
        candidates.append(r)

    if not candidates:
        return None, None

    # specificity DESC, priority ASC, id DESC
    candidates.sort(key=lambda x: (-x.specificity_score(), int(x.priority or 100), -int(x.id or 0)))
    best = candidates[0]
    return best.template, best


def _is_admin(user) -> bool:
    role = (getattr(user, "role", "") or "").strip().upper()
    return role in ("ADMIN", "SUPER_ADMIN")


def _user_can_act_on_step(user, step: WorkflowInstanceStep) -> bool:
    # PARALLEL_SYNC: allow response only for assignees who still have a PENDING task
    try:
        if (getattr(step, "mode", "") or "").strip().upper() == "PARALLEL_SYNC":
            return (
                WorkflowStepTask.query
                .filter_by(
                    instance_id=step.instance_id,
                    step_order=step.step_order,
                    assignee_user_id=user.id,
                    status="PENDING",
                )
                .first()
                is not None
            )
    except Exception:
        pass

    if _is_admin(user):
        return True

    kind = (step.approver_kind or "").upper()

    if kind == "USER" and step.approver_user_id:
        return step.approver_user_id == user.id

    if kind == "ROLE" and step.approver_role:
        return (user.role or "").strip().lower() == (step.approver_role or "").strip().lower()

    if kind == "DEPARTMENT" and step.approver_department_id:
        return (
            user.department_id == step.approver_department_id
            and (user.role or "").strip().lower() == "dept_head"
        )

    if kind == "DIRECTORATE" and getattr(step, "approver_directorate_id", None):
        dir_id = _get_effective_directorate_id(user)
        if not dir_id:
            return False
        return (
            int(dir_id) == int(step.approver_directorate_id)
            and (user.role or "").strip().lower() in ("directorate_head", "directorate_deputy")
        )

    return False



def _get_request_files(req: WorkflowRequest):
    """
    Returns a list of ArchivedFile linked to the request.
    Supports RequestAttachment table and (optionally) legacy workflow_request_id on ArchivedFile.
    """
    files = []

    # Preferred: RequestAttachment linking table
    try:
        atts = (
            RequestAttachment.query
            .options(joinedload(RequestAttachment.archived_file))
            .filter_by(request_id=req.id)
            .all()
        )
        for a in atts:
            f = getattr(a, "archived_file", None)
            if f and not getattr(f, "is_deleted", False):
                files.append(f)
        if files:
            return files
    except Exception:
        pass

    # Optional legacy: ArchivedFile.workflow_request_id
    try:
        if hasattr(ArchivedFile, "workflow_request_id"):
            files = (
                ArchivedFile.query
                .filter(
                    ArchivedFile.is_deleted.is_(False),
                    ArchivedFile.workflow_request_id == req.id
                )
                .order_by(ArchivedFile.upload_date.desc())
                .all()
            )
            return files
    except Exception:
        pass

    return []


# =========================
# PDF Report
# =========================
@workflow_bp.route("/<int:request_id>/pdf")
@login_required
def request_pdf(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    if not _user_can_view_request(current_user, req):
        abort(403)

    logs = (
        AuditLog.query
        .filter_by(request_id=request_id)
        .order_by(AuditLog.created_at.asc())
        .all()
    )

    attachments = _get_request_files(req)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=50,
        bottomMargin=40
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="Header",
        fontSize=14,
        leading=18,
        alignment=1,
        spaceAfter=20
    ))
    styles.add(ParagraphStyle(
        name="Small",
        fontSize=9,
        textColor=colors.grey
    ))

    elements = []
    elements.append(Paragraph("Workflow Request Report", styles["Header"]))
    elements.append(Paragraph(f"<b>Request ID:</b> {req.id}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Title:</b> {req.title or '-'}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Status:</b> {ui_label(req.status) or '-'}", styles["Normal"]))
    elements.append(
        Paragraph(
            f"<b>Generated at:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
            styles["Small"]
        )
    )
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("<b>Attachments</b>", styles["Heading2"]))
    if attachments:
        att_table = [["#", "File Name", "Type", "Size (KB)", "Uploaded"]]
        for i, f in enumerate(attachments, start=1):
            att_table.append([
                i,
                f.original_name,
                f.mime_type or "-",
                round((f.file_size or 0) / 1024, 1),
                (f.upload_date.strftime("%Y-%m-%d") if f.upload_date else "-")
            ])

        elements.append(
            Table(
                att_table,
                colWidths=[30, 180, 80, 70, 80],
                style=TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ])
            )
        )
    else:
        elements.append(Paragraph("No attachments.", styles["Normal"]))

    elements.append(PageBreak())
    elements.append(Paragraph("<b>Workflow Timeline</b>", styles["Heading2"]))

    if logs:
        log_table = [["Date", "Action", "From → To", "Note"]]
        for log in logs:
            log_table.append([
                log.created_at.strftime("%Y-%m-%d %H:%M") if log.created_at else "-",
                ui_label(log.action) or "-",
                f"{ui_label(log.old_status) or '-'} → {ui_label(log.new_status) or '-'}",
                ui_text(log.note) or ""
            ])

        elements.append(
            Table(
                log_table,
                colWidths=[90, 90, 100, 160],
                style=TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP")
                ])
            )
        )
    else:
        elements.append(Paragraph("No workflow actions recorded.", styles["Normal"]))

    signed_attachments = [f for f in attachments if getattr(f, "is_signed", False)]
    if signed_attachments:
        elements.append(Spacer(1, 20))
        elements.append(
            Paragraph(
                f"<b>Signed:</b> {len(signed_attachments)} attachment(s) are signed.",
                styles["Normal"]
            )
        )

    doc.build(elements)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"workflow_request_{req.id}.pdf",
        mimetype="application/pdf"
    )


# =========================
# Upload attachment to request
# =========================
@workflow_bp.route("/<int:request_id>/upload-attachment", methods=["POST"])
@login_required
def upload_attachment(request_id):
    """Upload one or more attachments to an existing request (manual upload)."""
    req = WorkflowRequest.query.get_or_404(request_id)

    if not _user_can_view_request(current_user, req):
        abort(403)

    description = request.form.get("description")

    files = []
    if request.files:
        files = request.files.getlist("files") or []
        if not files:
            single = request.files.get("file")
            if single:
                files = [single]

    files = [f for f in (files or []) if f and getattr(f, "filename", "")]
    if not files:
        abort(400)

    # best-effort step association
    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    step_order = 0
    if inst and getattr(inst, "current_step_order", None):
        try:
            step_order = int(inst.current_step_order)
        except Exception:
            step_order = 0

    saved_paths = []
    try:
        for fs in files:
            archived, saved_path = _save_upload_to_archive(
                fs, owner_id=current_user.id, visibility="workflow", description=description
            )
            if hasattr(archived, "workflow_request_id"):
                setattr(archived, "workflow_request_id", req.id)

            db.session.add(archived)
            db.session.flush()
            saved_paths.append(saved_path)

            # Preferred linkage table
            db.session.add(RequestAttachment(request_id=req.id, archived_file_id=archived.id))

            _audit_attachment(
                req_id=req.id,
                file_id=archived.id,
                step_order=step_order,
                source="MANUAL_UPLOAD",
                original_name=archived.original_name,
                uploaded_by_id=current_user.id,
            )

        # optional admin notification
        try:
            emit_event(
                actor_id=current_user.id,
                action="WORKFLOW_ATTACHMENT_UPLOADED",
                message=f"تم رفع {len(files)} مرفق/مرفقات على الطلب #{req.id}",
                target_type="WorkflowRequest",
                target_id=req.id,
                notify_role="ADMIN",
                level="WORKFLOW",
                auto_commit=False,
            )
        except Exception:
            pass

        db.session.commit()
        flash("تم رفع المرفقات بنجاح", "success")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    except Exception as e:
        db.session.rollback()
        for sp in saved_paths:
            try:
                if sp and os.path.exists(sp):
                    os.remove(sp)
            except Exception:
                pass

        flash(f"حدث خطأ أثناء رفع المرفقات: {e}", "danger")
        return redirect(url_for("workflow.view_request", request_id=req.id))


# =========================
# Download workflow attachment
# =========================
@workflow_bp.route("/attachment/<int:file_id>/download")
@login_required
def download_workflow_attachment(file_id):
    file = ArchivedFile.query.filter(
        ArchivedFile.id == file_id,
        ArchivedFile.is_deleted.is_(False)
    ).first_or_404()

    req = None

    # Optional legacy
    if hasattr(file, "workflow_request_id") and getattr(file, "workflow_request_id", None):
        req = WorkflowRequest.query.get_or_404(file.workflow_request_id)
    else:
        att = RequestAttachment.query.filter_by(archived_file_id=file.id).first()
        if att:
            req = WorkflowRequest.query.get_or_404(att.request_id)

    if not req or not _user_can_view_request(current_user, req):
        abort(403)

    return send_file(
        file.file_path,
        as_attachment=True,
        download_name=file.original_name
    )


@workflow_bp.route("/attachment/<int:file_id>/preview")
@login_required
def preview_workflow_attachment(file_id):
    """Preview workflow attachment.

    - For previewable types (PDF/images/text): streams inline.
    - For non-previewable types (e.g., DOCX): shows a safe HTML page with a download button.

    This prevents the UX confusion where "Preview" triggers a download for unsupported types.
    """
    file = ArchivedFile.query.filter(
        ArchivedFile.id == file_id,
        ArchivedFile.is_deleted.is_(False)
    ).first_or_404()

    req = None
    if hasattr(file, "workflow_request_id") and getattr(file, "workflow_request_id", None):
        req = WorkflowRequest.query.get_or_404(file.workflow_request_id)
    else:
        att = RequestAttachment.query.filter_by(archived_file_id=file.id).first()
        if att:
            req = WorkflowRequest.query.get_or_404(att.request_id)

    if not req or not _user_can_view_request(current_user, req):
        abort(403)

    mime = _guess_mime_for_file(file)

    # Stream inline for types browsers usually can render
    if _is_inline_previewable(mime):
        resp = send_file(
            file.file_path,
            mimetype=mime,
            as_attachment=False,
            conditional=True,
        )
        # Force inline disposition with UTF-8 filename (best-effort)
        fname = file.original_name or f"file_{file.id}"
        resp.headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(fname)}"
        return resp

    # Otherwise show a preview page (no auto-download)
    return render_template(
        "workflow/attachment_preview.html",
        file=file,
        mime=mime,
        request_obj=req,
        download_url=url_for("workflow.download_workflow_attachment", file_id=file.id),
        back_url=url_for("workflow.request_attachments", request_id=req.id),
    )





# =========================
# Notifications
# =========================
@workflow_bp.route("/notifications")
@login_required
def notifications():
    page = request.args.get("page", 1, type=int)
    per_page = 20

    scope = (request.args.get("scope") or "inbox").strip().lower()
    if scope not in {"inbox", "sent"}:
        scope = "inbox"

    notif_type = request.args.get("type")
    read_state = request.args.get("read")
    role = request.args.get("role")

    date_from = request.args.get("from")   # YYYY-MM-DD
    date_to = request.args.get("to")       # YYYY-MM-DD

    query = (
        Notification.query
        .filter(Notification.user_id == current_user.id)
        .filter(Notification.is_mirror.is_(scope == "sent"))
        .filter(or_(Notification.source.is_(None), Notification.source == "workflow"))
    )

    if notif_type:
        query = query.filter(Notification.type == notif_type)

    if read_state == "unread":
        query = query.filter(Notification.is_read.is_(False))
    elif read_state == "read":
        query = query.filter(Notification.is_read.is_(True))

    if role:
        query = query.filter(Notification.role == role)

    if date_from:
        start = datetime.strptime(date_from, "%Y-%m-%d")
        query = query.filter(Notification.created_at >= start)

    if date_to:
        end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(Notification.created_at < end)

    pagination = query.order_by(Notification.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # Counts shown in header
    unread_count = (
        Notification.query
        .filter_by(user_id=current_user.id, is_mirror=False, is_read=False)
        .filter(or_(Notification.source.is_(None), Notification.source == "workflow"))
        .count()
    )
    pending_sent_count = (
        Notification.query
        .filter_by(user_id=current_user.id, is_mirror=True, is_read=False)
        .filter(or_(Notification.source.is_(None), Notification.source == "workflow"))
        .count()
    )

    return render_template(
        "workflow/notifications.html",
        notifications=pagination.items,
        pagination=pagination,
        unread_count=unread_count,
        pending_sent_count=pending_sent_count,
        scope=scope,
        filters={
            "type": notif_type,
            "read": read_state,
            "role": role,
            "from": date_from,
            "to": date_to,
            "scope": scope,
        }
    )


@workflow_bp.route("/notifications/unread-count")
@login_required
def unread_notifications_count():
    count = (Notification.query.filter_by(
        user_id=current_user.id,
        is_mirror=False,
        is_read=False
    ).filter(or_(Notification.source.is_(None), Notification.source == "workflow"))
     .count())
    return jsonify({"count": count})


def _sync_mirror_for_event(event_key: str):
    """Auto-mark sender mirror notification as read when all recipients have read."""
    if not event_key:
        return

    pending = (
        db.session.query(func.count(Notification.id))
        .filter(
            Notification.event_key == event_key,
            Notification.is_mirror.is_(False),
            Notification.is_read.is_(False),
            or_(Notification.source.is_(None), Notification.source == "workflow")
        )
        .scalar()
    ) or 0

    if int(pending) == 0:
        db.session.execute(
            update(Notification)
            .where(
                Notification.event_key == event_key,
                Notification.is_mirror.is_(True),
                Notification.is_read.is_(False),
                or_(Notification.source.is_(None), Notification.source == "workflow")
            )
            .values(is_read=True)
        )


@workflow_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    start = time.perf_counter()
    try:
        # Collect event_keys to sync mirrors after bulk update
        event_keys = [
            ek for (ek,) in (
                db.session.query(Notification.event_key)
                .filter(
                    Notification.user_id == current_user.id,
                    Notification.is_mirror.is_(False),
                    Notification.is_read.is_(False),
                    or_(Notification.source.is_(None), Notification.source == "workflow"),
                    Notification.event_key.isnot(None)
                )
                .distinct()
                .all()
            )
            if ek
        ]

        result = (
            db.session.execute(
                update(Notification)
                .where(
                    Notification.user_id == current_user.id,
                    Notification.is_mirror.is_(False),
                    Notification.is_read.is_(False),
                    or_(Notification.source.is_(None), Notification.source == "workflow")
                )
                .values(is_read=True)
            )
        )

        for ek in event_keys:
            _sync_mirror_for_event(ek)

        db.session.commit()

        elapsed = time.perf_counter() - start
        logger.info(
            "Marked %s notifications as read for user_id=%s in %.2fs",
            getattr(result, "rowcount", None),
            current_user.id,
            elapsed
        )
        flash("تم تعليم جميع الإشعارات كمقروءة", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to mark notifications as read for user_id=%s", current_user.id)
        flash("حدث خطأ أثناء تحديث الإشعارات", "danger")

    return redirect(url_for("workflow.notifications"))


@workflow_bp.route("/notifications/<int:notif_id>/read", methods=["POST"])
@login_required
def mark_notification_read(notif_id):
    n = (Notification.query
         .filter(Notification.id == notif_id)
         .filter(Notification.user_id == current_user.id)
         .filter(or_(Notification.source.is_(None), Notification.source == "workflow"))
         .first_or_404())

    # Mirror (sent-tracking) notifications are read-only (auto-updated)
    if getattr(n, "is_mirror", False):
        return "", 204

    if not n.is_read:
        n.is_read = True
        if getattr(n, "event_key", None):
            _sync_mirror_for_event(n.event_key)
        db.session.commit()

    return "", 204



@workflow_bp.route("/notifications/stream")
@login_required
def event_stream():
    @stream_with_context
    def gen():
        last = None
        while True:
            try:
                unread = (
                    db.session.query(func.count(Notification.id))
                    .filter(
                        Notification.user_id == current_user.id,
                        Notification.is_mirror.is_(False),
                        Notification.is_read.is_(False),
                        or_(Notification.source.is_(None), Notification.source == "workflow")
                    )
                    .scalar()
                ) or 0

                payload = {"unread": int(unread)}

                if payload != last:
                    yield f"data: {json.dumps(payload)}\n\n"
                    last = payload

            except Exception:
                yield "event: ping\ndata: {}\n\n"
            finally:
                db.session.remove()

            time.sleep(6)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return Response(gen(), headers=headers)


@workflow_bp.route("/notifications/dashboard")
@login_required
@perm_required("WORKFLOW_NOTIFICATIONS_DASHBOARD_READ")
def notifications_dashboard():
    total = Notification.query.filter(or_(Notification.source.is_(None), Notification.source == 'workflow')).count()
    unread = Notification.query.filter_by(is_read=False).filter(or_(Notification.source.is_(None), Notification.source == 'workflow')).count()

    top_users = (
        db.session.query(
            User.email,
            func.count(Notification.id).label("count")
        )
        .join(Notification, Notification.user_id == User.id)
        .filter(or_(Notification.source.is_(None), Notification.source == 'workflow'))
        .group_by(User.email)
        .order_by(func.count(Notification.id).desc())
        .limit(5)
        .all()
    )

    by_type = (
        db.session.query(
            Notification.type,
            func.count(Notification.id)
        )
        .filter(or_(Notification.source.is_(None), Notification.source == 'workflow'))
        .group_by(Notification.type)
        .all()
    )

    return render_template(
        "notifications/dashboard.html",
        total=total,
        unread=unread,
        top_users=top_users,
        by_type=by_type
    )


# =========================
# Helpers: Who can view/act
# =========================
def _user_can_act_on_step(user, step: WorkflowInstanceStep) -> bool:
    """Return True if the user can respond on this step."""

    # PARALLEL_SYNC: allow response only for assignees who still have a PENDING task
    try:
        if (getattr(step, "mode", "") or "").strip().upper() == "PARALLEL_SYNC":
            return (
                WorkflowStepTask.query
                .filter_by(
                    instance_id=step.instance_id,
                    step_order=step.step_order,
                    assignee_user_id=user.id,
                    status="PENDING",
                )
                .first()
                is not None
            )
    except Exception:
        pass

    if _is_admin(user):
        return True

    # Delegation: treat delegated-from users as actors too
    actor_users = [user]
    try:
        base = get_effective_user(user)
        actor_users = [base] + get_delegated_from_users(base)
    except Exception:
        pass

    kind = (step.approver_kind or "").strip().upper()

    def _is_org_manager(unit_type: str, unit_id_val, actor: User) -> bool:
        """Check OrgUnitManager (manager/deputy) for a given unit type/id."""
        try:
            uid = int(unit_id_val) if unit_id_val is not None else None
        except Exception:
            uid = None
        if not uid:
            return False
        try:
            row = OrgUnitManager.query.filter_by(
                unit_type=(unit_type or "").strip().upper(),
                unit_id=uid,
            ).first()
        except Exception:
            row = None
        if not row:
            return False
        try:
            if getattr(row, "manager_user_id", None) and int(row.manager_user_id) == int(actor.id):
                return True
            if getattr(row, "deputy_user_id", None) and int(row.deputy_user_id) == int(actor.id):
                return True
        except Exception:
            return False
        return False

    for u in actor_users:
        if kind == "USER" and step.approver_user_id:
            if step.approver_user_id == u.id:
                return True

        elif kind == "ROLE" and step.approver_role:
            if (u.role or "").strip().lower() == (step.approver_role or "").strip().lower():
                return True

        elif kind == "DEPARTMENT" and step.approver_department_id:
            # Prefer OrgUnitManager (DEPARTMENT) if configured
            if _is_org_manager("DEPARTMENT", step.approver_department_id, u):
                return True
            # Fallback: legacy role-based
            if (u.department_id == step.approver_department_id) and ((u.role or "").strip().lower() == "dept_head"):
                return True

        elif kind == "DIRECTORATE" and getattr(step, "approver_directorate_id", None):
            # Prefer OrgUnitManager (DIRECTORATE) if configured
            if _is_org_manager("DIRECTORATE", getattr(step, "approver_directorate_id", None), u):
                return True
            dir_id = _get_effective_directorate_id(u)
            if not dir_id:
                continue
            if (
                int(dir_id) == int(step.approver_directorate_id)
                and (u.role or "").strip().lower() in ("directorate_head", "directorate_deputy")
            ):
                return True

        elif kind == "UNIT" and getattr(step, "approver_unit_id", None):
            if _is_org_manager("UNIT", getattr(step, "approver_unit_id", None), u):
                return True

        elif kind == "SECTION" and getattr(step, "approver_section_id", None):
            if _is_org_manager("SECTION", getattr(step, "approver_section_id", None), u):
                return True

        elif kind == "DIVISION" and getattr(step, "approver_division_id", None):
            if _is_org_manager("DIVISION", getattr(step, "approver_division_id", None), u):
                return True

        elif kind == "ORG_NODE" and getattr(step, "approver_org_node_id", None):
            mgr = OrgNodeManager.query.filter_by(node_id=int(step.approver_org_node_id)).first()
            if mgr and u.id in (mgr.manager_user_id, mgr.deputy_user_id):
                return True

        elif kind == "COMMITTEE" and getattr(step, "approver_committee_id", None):
            cmode = (getattr(step, "committee_delivery_mode", None) or "Committee_ALL")
            cmode_up = cmode.strip().upper()

            q = CommitteeAssignee.query.filter_by(
                committee_id=int(step.approver_committee_id),
                is_active=True,
            )

            role_vars = _role_variants(getattr(u, "role", "") or "")
            role_clause = (
                db.or_(*[CommitteeAssignee.role.ilike(v) for v in role_vars])
                if role_vars else CommitteeAssignee.role.ilike(getattr(u, "role", "") or "")
            )

            q = q.filter(
                db.or_(
                    db.and_(CommitteeAssignee.kind == "USER", CommitteeAssignee.user_id == u.id),
                    db.and_(CommitteeAssignee.kind == "ROLE", role_clause),
                )
            )

            # Delivery mode filter
            if "CHAIR" in cmode_up:
                q = q.filter(CommitteeAssignee.member_role.ilike("CHAIR"))
            elif "SECRETARY" in cmode_up:
                q = q.filter(CommitteeAssignee.member_role.ilike("SECRETARY"))

            if q.first() is not None:
                return True

    return False
def _user_can_view_request(user, req: WorkflowRequest) -> bool:
    # Owner can always view
    if req.requester_id == user.id:
        return True

    # SUPER_ADMIN can view everything
    if user.has_role("SUPER_ADMIN"):
        return True

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    if not inst:
        return False

    steps = WorkflowInstanceStep.query.filter_by(instance_id=inst.id).all()

    # Anyone who can act on (any) step can view
    if any(_user_can_act_on_step(user, s) for s in steps):
        return True

    # ✅ Followers: anyone who has already decided at least one step can keep viewing
    try:
        if any(getattr(s, 'decided_by_id', None) == user.id for s in steps):
            return True
    except Exception:
        pass

    # ✅ PARALLEL_SYNC followers: anyone who has a StepTask (responded/bypassed/pending) can keep viewing
    try:
        if (
            WorkflowStepTask.query
            .filter(
                WorkflowStepTask.instance_id == inst.id,
                WorkflowStepTask.assignee_user_id == user.id
            )
            .first()
            is not None
        ):
            return True
    except Exception:
        pass

    # ✅ Mention-added participants: a comment can grant access to a user/role
    # without changing the original template.
    try:
        if (
            AuditLog.query
            .filter(
                AuditLog.request_id == req.id,
                AuditLog.action == MENTION_ACCESS_ACTION,
                AuditLog.target_type == "USER",
                AuditLog.target_id == user.id,
            )
            .first()
            is not None
        ):
            return True
    except Exception:
        pass

    return False


def _get_request_followers_user_ids(req_id: int) -> set[int]:
    """Users who decided at least one step (followers)."""
    ids: set[int] = set()
    inst = WorkflowInstance.query.filter_by(request_id=req_id).first()
    if not inst:
        return ids

    # From sequential steps
    rows = (
        db.session.query(WorkflowInstanceStep.decided_by_id)
        .filter(WorkflowInstanceStep.instance_id == inst.id)
        .filter(WorkflowInstanceStep.decided_by_id.isnot(None))
        .all()
    )
    for (uid,) in rows:
        try:
            if uid:
                ids.add(int(uid))
        except Exception:
            pass

    # ✅ Include PARALLEL_SYNC responders/bypassed users as followers
    try:
        rows2 = (
            db.session.query(WorkflowStepTask.assignee_user_id)
            .filter(WorkflowStepTask.instance_id == inst.id)
            .filter(WorkflowStepTask.status.in_(["RESPONDED", "BYPASSED"]))
            .all()
        )
        for (uid2,) in rows2:
            try:
                if uid2:
                    ids.add(int(uid2))
            except Exception:
                pass
    except Exception:
        pass

    # ✅ Include users brought into the workflow through comment mentions.
    try:
        ids.update(_mentioned_user_ids_for_request(req_id))
    except Exception:
        pass

    return ids

    rows = (
        db.session.query(WorkflowInstanceStep.decided_by_id)
        .filter(WorkflowInstanceStep.instance_id == inst.id)
        .filter(WorkflowInstanceStep.decided_by_id.isnot(None))
        .all()
    )
    for (uid,) in rows:
        try:
            if uid:
                ids.add(int(uid))
        except Exception:
            pass
    return ids



# =========================
# View request
# =========================
@workflow_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_request():
    # load request types (may be empty)
    request_types = (
        RequestType.query
        .filter_by(is_active=True)
        .order_by(RequestType.name_ar.asc())
        .all()
    )

    # templates always available as manual fallback
    templates = (
        WorkflowTemplate.query
        .filter_by(is_active=True)
        .order_by(WorkflowTemplate.name.asc())
        .all()
    )

    selected_rt_id = request.args.get("request_type_id")
    suggested_template = None
    matched_rule = None

    if selected_rt_id and str(selected_rt_id).isdigit():
        suggested_template, matched_rule = _select_template_for(current_user, int(selected_rt_id))

    if request.method == "POST":
        title = (request.form.get("title") or "").strip() or "طلب جديد"
        description = (request.form.get("description") or "").strip()

        rt_id = (request.form.get("request_type_id") or "").strip()
        template_id = (request.form.get("template_id") or "").strip()

        # validate request type if exists
        request_type_id = None
        if request_types:
            if not rt_id.isdigit():
                flash("يرجى اختيار نوع الطلب.", "danger")
                return redirect(request.url)
            request_type_id = int(rt_id)

        # choose template: manual first, else routing, else single-template fallback
        template = None
        if template_id.isdigit():
            template = WorkflowTemplate.query.get_or_404(int(template_id))
        elif request_type_id:
            template, matched = _select_template_for(current_user, request_type_id)

        # If there is only one active template, auto-select it (common during early setup)
        if not template and templates and len(templates) == 1:
            template = templates[0]

        if not template:
            flash("لا يوجد مسار مناسب. يرجى اختيار مسار (Template) يدويًا أو إضافة Routing Rule.", "danger")
            return redirect(request.url)

        req = WorkflowRequest(
            requester_id=current_user.id,
            status="DRAFT",
            title=title,
            description=description,
            request_type_id=request_type_id
        )
        db.session.add(req)
        db.session.flush()

        # IMPORTANT: single commit per route (engine does not commit)
        start_workflow_for_request(
            req,
            template,
            created_by_user_id=current_user.id,
            auto_commit=False
        )


        # ✅ Attach files (multiple) with the request creation
        uploaded_files = request.files.getlist("files") if request.files else []
        uploaded_files = [f for f in (uploaded_files or []) if f and getattr(f, "filename", "")]

        saved_paths = []
        try:
            for fs in uploaded_files:
                archived, saved_path = _save_upload_to_archive(
                    fs, owner_id=current_user.id, visibility="workflow", description=None
                )

                # Optional legacy linkage if exists
                if hasattr(archived, "workflow_request_id"):
                    setattr(archived, "workflow_request_id", req.id)

                db.session.add(archived)
                db.session.flush()
                saved_paths.append(saved_path)

                db.session.add(RequestAttachment(request_id=req.id, archived_file_id=archived.id))
                _audit_attachment(
                    req_id=req.id,
                    file_id=archived.id,
                    step_order=0,
                    source="CREATE",
                    original_name=archived.original_name,
                    uploaded_by_id=current_user.id,
                )
        except Exception as e:
            db.session.rollback()
            for sp in saved_paths:
                try:
                    if os.path.exists(sp):
                        os.remove(sp)
                except Exception:
                    pass
            flash(f"تعذر رفع المرفقات: {e}", "danger")
            return redirect(request.url)

        # ✅ Notification for requester: request created and workflow started
        try:
            emit_event(
                actor_id=current_user.id,
                action='REQUEST_CREATED',
                message=f"تم إنشاء طلب جديد #{req.id} وبدء المسار: {template.name}",
                target_type='WorkflowRequest',
                target_id=req.id,
                notify_user_id=current_user.id,
                level='WORKFLOW',
                auto_commit=False,
            )
        except Exception:
            # do not block request creation if notification fails
            pass

        db.session.commit()
        flash("تم إنشاء الطلب وبدء مسار العمل.", "success")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    return render_template(
        "workflow/new_request.html",
        request_types=request_types,
        templates=templates,
        selected_rt_id=int(selected_rt_id) if (selected_rt_id and str(selected_rt_id).isdigit()) else None,
        suggested_template=suggested_template,
        matched_rule=matched_rule
    )




# =========================
# Inbox: pending steps for me
# =========================
@workflow_bp.route("/inbox")
@login_required
def inbox():
    """Pending steps for current user (task inbox), including delegated tasks."""
    search = (request.args.get("q") or "").strip()

    effective_user = get_effective_user()
    delegations = get_active_delegations()

    # Candidate "actors": myself + any active delegators I'm delegated from
    actor_users = [current_user]
    for d in (delegations or []):
        try:
            if d and getattr(d, "from_user", None) and d.from_user.id not in [u.id for u in actor_users]:
                actor_users.append(d.from_user)
        except Exception:
            pass

    # SUPER_ADMIN sees all pending current steps
    q = (
        db.session.query(WorkflowRequest, WorkflowInstance, WorkflowInstanceStep)
        .join(WorkflowInstance, WorkflowInstance.request_id == WorkflowRequest.id)
        .join(WorkflowInstanceStep, WorkflowInstanceStep.instance_id == WorkflowInstance.id)
        .filter(WorkflowInstance.is_completed.is_(False))
        .filter(WorkflowInstanceStep.status == "PENDING")
        .filter(WorkflowInstanceStep.step_order == WorkflowInstance.current_step_order)
    )

    # Search (by id/title/description/requester email)
    if search:
        like = f"%{search}%"
        q = q.join(User, User.id == WorkflowRequest.requester_id)
        conds = [
            WorkflowRequest.title.ilike(like),
            WorkflowRequest.description.ilike(like),
            User.email.ilike(like),
        ]
        if search.isdigit():
            try:
                conds.insert(0, WorkflowRequest.id == int(search))
            except Exception:
                pass
        q = q.filter(or_(*conds))

    # If not SUPER_ADMIN, restrict by any actor context (self OR delegated-from users)
    is_super = current_user.has_role("SUPER_ADMIN") or any(getattr(u, "has_role", lambda r: False)("SUPER_ADMIN") for u in actor_users)
    if not is_super:
        all_clauses = []

        for u in actor_users:
            user_role_norm = _norm_role(getattr(u, 'role', '') or '')
            role_variants = _role_variants(getattr(u, 'role', '') or '')

            clauses = [
                # USER
                db.and_(
                    WorkflowInstanceStep.approver_kind == "USER",
                    WorkflowInstanceStep.approver_user_id == u.id,
                ),
                # ROLE
                db.and_(
                    WorkflowInstanceStep.approver_kind == "ROLE",
                    or_(*[WorkflowInstanceStep.approver_role.ilike(v) for v in role_variants]) if role_variants else WorkflowInstanceStep.approver_role.ilike(u.role),
                ),
                # DEPARTMENT (رئيس دائرة)
                db.and_(
                    WorkflowInstanceStep.approver_kind == "DEPARTMENT",
                    WorkflowInstanceStep.approver_department_id == u.department_id,
                    user_role_norm == "dept_head",
                ),
            ]

            # DIRECTORATE (رئيس إدارة)
            if user_role_norm in ("directorate_head", "directorate_deputy"):
                dir_id = _get_effective_directorate_id(u)
                if dir_id:
                    clauses.append(
                        db.and_(
                            WorkflowInstanceStep.approver_kind == "DIRECTORATE",
                            WorkflowInstanceStep.approver_directorate_id == int(dir_id),
                        )
                    )



            # COMMITTEE (sequential steps): show request if user is a member of the committee for this step.
            # Note: PARALLEL_SYNC committee steps are already covered by WorkflowStepTask exists-clause below.
            try:
                cm_role_vars = role_variants
                committee_member_exists = (
                    db.session.query(CommitteeAssignee.id)
                    .filter(
                        CommitteeAssignee.committee_id == WorkflowInstanceStep.approver_committee_id,
                        CommitteeAssignee.is_active.is_(True),
                        db.or_(
                            db.and_(CommitteeAssignee.kind == "USER", CommitteeAssignee.user_id == u.id),
                            db.and_(
                                CommitteeAssignee.kind == "ROLE",
                                db.or_(*[CommitteeAssignee.role.ilike(v) for v in (cm_role_vars or [])]) if (cm_role_vars or []) else CommitteeAssignee.role.ilike(getattr(u, "role", "")),
                            ),
                        ),
                        db.or_(
                            WorkflowInstanceStep.committee_delivery_mode.is_(None),
                            WorkflowInstanceStep.committee_delivery_mode.in_(["Committee_ALL", "COMMITTEE_ALL"]),
                            db.and_(
                                WorkflowInstanceStep.committee_delivery_mode.in_(["Committee_CHAIR", "COMMITTEE_CHAIR"]),
                                CommitteeAssignee.member_role.ilike("CHAIR"),
                            ),
                            db.and_(
                                WorkflowInstanceStep.committee_delivery_mode.in_(["Committee_SECRETARY", "COMMITTEE_SECRETARY"]),
                                CommitteeAssignee.member_role.ilike("SECRETARY"),
                            ),
                        ),
                    )
                    .exists()
                )
                clauses.append(
                    db.and_(
                        WorkflowInstanceStep.approver_kind == "COMMITTEE",
                        committee_member_exists,
                    )
                )
            except Exception:
                pass
            all_clauses.extend(clauses)
        # ✅ PARALLEL_SYNC tasks: if I am one of the parallel assignees (StepTask PENDING),
        # show the request in my inbox even if I'm not the primary approver target.
        try:
            actor_ids = [int(getattr(u, "id", 0)) for u in (actor_users or []) if getattr(u, "id", None)]
            if actor_ids:
                pending_parallel_exists = (
                    db.session.query(WorkflowStepTask.id)
                    .filter(
                        WorkflowStepTask.instance_id == WorkflowInstance.id,
                        WorkflowStepTask.step_order == WorkflowInstance.current_step_order,
                        WorkflowStepTask.assignee_user_id.in_(actor_ids),
                        WorkflowStepTask.status == "PENDING",
                    )
                    .exists()
                )
                all_clauses.append(pending_parallel_exists)
        except Exception:
            pass

        if all_clauses:
            q = q.filter(or_(*all_clauses))
        else:
            # No clauses -> no results
            q = q.filter(db.text("1=0"))

    rows = q.order_by(WorkflowRequest.id.desc()).all()

    # PARALLEL_SYNC: if the step is still pending but the current user already responded/bypassed,
    # hide it from their inbox (it will remain for other pending assignees).
    if not (current_user.has_role("ADMIN") or current_user.has_role("SUPER_ADMIN")):
        actor_ids = [int(u.id) for u in (actor_users or []) if getattr(u, "id", None)]
        if actor_ids:
            filtered = []
            for req, inst, step in rows:
                try:
                    if (getattr(step, "mode", "") or "").strip().upper() == "PARALLEL_SYNC":
                        pending = (
                            WorkflowStepTask.query
                            .filter(
                                WorkflowStepTask.instance_id == inst.id,
                                WorkflowStepTask.step_order == step.step_order,
                                WorkflowStepTask.assignee_user_id.in_(actor_ids),
                                WorkflowStepTask.status == "PENDING",
                            )
                            .count()
                            > 0
                        )
                        if not pending:
                            continue
                except Exception:
                    # If anything goes wrong, keep the row (better than hiding it)
                    pass
                filtered.append((req, inst, step))
            rows = filtered
    

    # --- Circulars (last 5) ---
    last_circulars = []
    try:
        last_circulars = (PortalCircular.query
                          .order_by(PortalCircular.created_at.desc(), PortalCircular.id.desc())
                          .limit(5)
                          .all())
    except Exception:
        last_circulars = []

    return render_template("workflow/inbox.html", rows=rows, q=search, last_circulars=last_circulars)


# =========================
# Circulars (for all users)
# =========================
@workflow_bp.route("/circulars")
@login_required
def circulars_list():
    rows = []
    try:
        rows = (PortalCircular.query
                .order_by(PortalCircular.created_at.desc(), PortalCircular.id.desc())
                .limit(200)
                .all())
    except Exception:
        rows = []
    return render_template("workflow/circulars.html", rows=rows)


@workflow_bp.route("/circulars/<int:circular_id>")
@login_required
def circulars_view(circular_id: int):
    row = PortalCircular.query.get_or_404(circular_id)
    return render_template("workflow/circular_view.html", row=row)




@workflow_bp.route("/following")
@login_required
def following():
    """Requests I can follow, with advanced filters."""
    search = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().upper()
    relation = (request.args.get("relation") or "").strip().lower()
    template_id = (request.args.get("template_id") or "").strip()
    request_type_id = (request.args.get("request_type_id") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    current_step = (request.args.get("current_step") or "").strip()
    sla_state = (request.args.get("sla_state") or "").strip().lower()
    sort = (request.args.get("sort") or "id_desc").strip().lower()

    def _arg_int(value):
        try:
            return int(value)
        except Exception:
            return None

    def _parse_day(value, *, end_of_day=False):
        value = (value or "").strip()
        if not value:
            return None
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
            if end_of_day:
                return dt + timedelta(days=1)
            return dt
        except Exception:
            return None

    effective_user = get_effective_user()
    delegations = get_active_delegations()

    actor_users = [current_user]
    for d in (delegations or []):
        try:
            if d and getattr(d, "from_user", None) and d.from_user.id not in [u.id for u in actor_users]:
                actor_users.append(d.from_user)
        except Exception:
            pass

    actor_ids = [int(getattr(u, "id", 0)) for u in (actor_users or []) if getattr(u, "id", None)]

    def _parallel_task_exists(*, pending_only: bool = False):
        clauses = [
            WorkflowStepTask.instance_id == WorkflowInstance.id,
        ]
        if actor_ids:
            clauses.append(WorkflowStepTask.assignee_user_id.in_(actor_ids))
        else:
            clauses.append(WorkflowStepTask.assignee_user_id == effective_user.id)
        if pending_only:
            clauses.extend([
                WorkflowStepTask.step_order == WorkflowInstance.current_step_order,
                WorkflowStepTask.status == "PENDING",
            ])
        return db.session.query(WorkflowStepTask.id).filter(*clauses).exists()

    def _current_assignment_clause():
        clauses = []
        for u in (actor_users or []):
            uid = int(getattr(u, "id", 0) or 0)
            if not uid:
                continue

            role_value = getattr(u, "role", "") or ""
            role_norm = _norm_role(role_value)
            role_variants = _role_variants(role_value)

            clauses.append(db.and_(
                WorkflowInstanceStep.approver_kind == "USER",
                WorkflowInstanceStep.approver_user_id == uid,
            ))

            if role_variants:
                clauses.append(db.and_(
                    WorkflowInstanceStep.approver_kind == "ROLE",
                    or_(*[WorkflowInstanceStep.approver_role.ilike(v) for v in role_variants]),
                ))

            dept_id = getattr(u, "department_id", None)
            dept_manager_exists = db.session.query(OrgUnitManager.id).filter(
                OrgUnitManager.unit_type == "DEPARTMENT",
                OrgUnitManager.unit_id == WorkflowInstanceStep.approver_department_id,
                or_(OrgUnitManager.manager_user_id == uid, OrgUnitManager.deputy_user_id == uid),
            ).exists()
            dept_parts = [dept_manager_exists]
            if dept_id and role_norm == "dept_head":
                dept_parts.append(WorkflowInstanceStep.approver_department_id == int(dept_id))
            clauses.append(db.and_(
                WorkflowInstanceStep.approver_kind == "DEPARTMENT",
                or_(*dept_parts),
            ))

            dir_manager_exists = db.session.query(OrgUnitManager.id).filter(
                OrgUnitManager.unit_type == "DIRECTORATE",
                OrgUnitManager.unit_id == WorkflowInstanceStep.approver_directorate_id,
                or_(OrgUnitManager.manager_user_id == uid, OrgUnitManager.deputy_user_id == uid),
            ).exists()
            dir_parts = [dir_manager_exists]
            if role_norm in ("directorate_head", "directorate_deputy"):
                dir_id = _get_effective_directorate_id(u)
                if dir_id:
                    dir_parts.append(WorkflowInstanceStep.approver_directorate_id == int(dir_id))
            clauses.append(db.and_(
                WorkflowInstanceStep.approver_kind == "DIRECTORATE",
                or_(*dir_parts),
            ))

            for kind, unit_type, step_col in (
                ("UNIT", "UNIT", WorkflowInstanceStep.approver_unit_id),
                ("SECTION", "SECTION", WorkflowInstanceStep.approver_section_id),
                ("DIVISION", "DIVISION", WorkflowInstanceStep.approver_division_id),
            ):
                manager_exists = db.session.query(OrgUnitManager.id).filter(
                    OrgUnitManager.unit_type == unit_type,
                    OrgUnitManager.unit_id == step_col,
                    or_(OrgUnitManager.manager_user_id == uid, OrgUnitManager.deputy_user_id == uid),
                ).exists()
                clauses.append(db.and_(
                    WorkflowInstanceStep.approver_kind == kind,
                    manager_exists,
                ))

            org_node_manager_exists = db.session.query(OrgNodeManager.id).filter(
                OrgNodeManager.node_id == WorkflowInstanceStep.approver_org_node_id,
                or_(OrgNodeManager.manager_user_id == uid, OrgNodeManager.deputy_user_id == uid),
            ).exists()
            clauses.append(db.and_(
                WorkflowInstanceStep.approver_kind == "ORG_NODE",
                org_node_manager_exists,
            ))

            committee_role_clause = (
                or_(*[CommitteeAssignee.role.ilike(v) for v in role_variants])
                if role_variants else CommitteeAssignee.role.ilike(role_value)
            )
            committee_member_exists = db.session.query(CommitteeAssignee.id).filter(
                CommitteeAssignee.committee_id == WorkflowInstanceStep.approver_committee_id,
                CommitteeAssignee.is_active.is_(True),
                or_(
                    db.and_(CommitteeAssignee.kind == "USER", CommitteeAssignee.user_id == uid),
                    db.and_(CommitteeAssignee.kind == "ROLE", committee_role_clause),
                ),
                or_(
                    WorkflowInstanceStep.committee_delivery_mode.is_(None),
                    WorkflowInstanceStep.committee_delivery_mode.in_(["Committee_ALL", "COMMITTEE_ALL"]),
                    db.and_(
                        WorkflowInstanceStep.committee_delivery_mode.in_(["Committee_CHAIR", "COMMITTEE_CHAIR"]),
                        CommitteeAssignee.member_role.ilike("CHAIR"),
                    ),
                    db.and_(
                        WorkflowInstanceStep.committee_delivery_mode.in_(["Committee_SECRETARY", "COMMITTEE_SECRETARY"]),
                        CommitteeAssignee.member_role.ilike("SECRETARY"),
                    ),
                ),
            ).exists()
            clauses.append(db.and_(
                WorkflowInstanceStep.approver_kind == "COMMITTEE",
                committee_member_exists,
            ))

        if not clauses:
            return db.text("0")

        current_normal_step = db.and_(
            WorkflowInstanceStep.step_order == WorkflowInstance.current_step_order,
            WorkflowInstanceStep.status == "PENDING",
            or_(*clauses),
        )

        try:
            return or_(current_normal_step, _parallel_task_exists(pending_only=True))
        except Exception:
            return current_normal_step

    q = (
        db.session.query(WorkflowRequest, WorkflowInstance, WorkflowTemplate)
        .join(WorkflowInstance, WorkflowInstance.request_id == WorkflowRequest.id)
        .outerjoin(WorkflowTemplate, WorkflowTemplate.id == WorkflowInstance.template_id)
        .outerjoin(WorkflowInstanceStep, WorkflowInstanceStep.instance_id == WorkflowInstance.id)
        .outerjoin(User, User.id == WorkflowRequest.requester_id)
    )

    # Visibility filter
    if not (current_user.has_role("ADMIN") or current_user.has_role("SUPER_ADMIN")):
        decided_clause = (
            WorkflowInstanceStep.decided_by_id.in_(actor_ids)
            if actor_ids
            else (WorkflowInstanceStep.decided_by_id == effective_user.id)
        )

        try:
            parallel_involved_exists = _parallel_task_exists()
        except Exception:
            parallel_involved_exists = db.text("0")

        q = q.filter(
            or_(
                WorkflowRequest.requester_id.in_(actor_ids) if actor_ids else WorkflowRequest.requester_id == effective_user.id,
                decided_clause,
                parallel_involved_exists,
                _current_assignment_clause(),
            )
        )

    # Advanced filters
    if search:
        like = f"%{search}%"
        conds = [
            WorkflowRequest.title.ilike(like),
            WorkflowRequest.description.ilike(like),
            User.name.ilike(like),
            User.email.ilike(like),
            WorkflowTemplate.name.ilike(like),
        ]
        if search.isdigit():
            try:
                conds.insert(0, WorkflowRequest.id == int(search))
            except Exception:
                pass
        q = q.filter(or_(*conds))

    allowed_statuses = ["DRAFT", "IN_PROGRESS", "APPROVED", "REJECTED"]
    if status in allowed_statuses:
        q = q.filter(WorkflowRequest.status == status)

    tid = _arg_int(template_id)
    if tid:
        q = q.filter(WorkflowInstance.template_id == tid)

    rtid = _arg_int(request_type_id)
    if rtid:
        q = q.filter(WorkflowRequest.request_type_id == rtid)

    step_no = _arg_int(current_step)
    if step_no is not None:
        q = q.filter(WorkflowInstance.current_step_order == step_no)

    start_dt = _parse_day(date_from)
    if start_dt:
        q = q.filter(WorkflowRequest.created_at >= start_dt)

    end_dt = _parse_day(date_to, end_of_day=True)
    if end_dt:
        q = q.filter(WorkflowRequest.created_at < end_dt)

    if relation == "created" and actor_ids:
        q = q.filter(WorkflowRequest.requester_id.in_(actor_ids))
    elif relation == "current":
        q = q.filter(_current_assignment_clause())
    elif relation == "decided" and actor_ids:
        q = q.filter(WorkflowInstanceStep.decided_by_id.in_(actor_ids))
    elif relation == "parallel" and actor_ids:
        try:
            q = q.filter(_parallel_task_exists())
        except Exception:
            pass

    sort_map = {
        "id_asc": WorkflowRequest.id.asc(),
        "id_desc": WorkflowRequest.id.desc(),
        "created_asc": WorkflowRequest.created_at.asc(),
        "created_desc": WorkflowRequest.created_at.desc(),
        "title_asc": WorkflowRequest.title.asc(),
        "status_asc": WorkflowRequest.status.asc(),
        "step_asc": WorkflowInstance.current_step_order.asc(),
        "step_desc": WorkflowInstance.current_step_order.desc(),
    }
    order_expr = sort_map.get(sort, WorkflowRequest.id.desc())

    rows = (
        q.distinct(WorkflowRequest.id)
        .order_by(order_expr, WorkflowRequest.id.desc())
        .all()
    )

    # SLA filter is applied after loading because it depends on template SLA + current time.
    now = datetime.utcnow()

    def _is_overdue(req, tpl):
        try:
            days = int(getattr(tpl, "sla_days_default", 0) or 0)
            created_at = getattr(req, "created_at", None)
            if not days or not created_at:
                return False
            if (getattr(req, "status", "") or "").upper() in ("APPROVED", "REJECTED"):
                return False
            return now > (created_at + timedelta(days=days))
        except Exception:
            return False

    if sla_state == "overdue":
        rows = [(req, inst, tpl) for (req, inst, tpl) in rows if _is_overdue(req, tpl)]
    elif sla_state == "on_time":
        rows = [(req, inst, tpl) for (req, inst, tpl) in rows if not _is_overdue(req, tpl)]

    try:
        templates = WorkflowTemplate.query.order_by(WorkflowTemplate.name.asc()).all()
    except Exception:
        templates = []

    try:
        request_types = RequestType.query.order_by(RequestType.name_ar.asc()).all()
    except Exception:
        request_types = []

    filters = {
        "q": search,
        "status": status,
        "relation": relation,
        "template_id": template_id,
        "request_type_id": request_type_id,
        "date_from": date_from,
        "date_to": date_to,
        "current_step": current_step,
        "sla_state": sla_state,
        "sort": sort,
    }
    active_filters_count = sum(
        1 for k, v in filters.items()
        if v and not (k == "sort" and v == "id_desc")
    )

    summary = {
        "total": len(rows),
        "open": sum(1 for req, _inst, _tpl in rows if (getattr(req, "status", "") or "").upper() not in ("APPROVED", "REJECTED")),
        "closed": sum(1 for req, _inst, _tpl in rows if (getattr(req, "status", "") or "").upper() in ("APPROVED", "REJECTED")),
        "overdue": sum(1 for req, _inst, tpl in rows if _is_overdue(req, tpl)),
        "active_filters": active_filters_count,
    }

    return render_template(
        "workflow/following.html",
        rows=rows,
        q=search,
        filters=filters,
        templates=templates,
        request_types=request_types,
        status_options=allowed_statuses,
        summary=summary,
    )

# =========================
# View Request (Timeline + Action)
# =========================
@workflow_bp.route("/request/<int:request_id>")
@login_required
def view_request(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)
    effective_user = get_effective_user()
    delegations = get_active_delegations()

    actor_users = [current_user]
    for d in (delegations or []):
        try:
            if d and getattr(d, "from_user", None) and d.from_user.id not in [u.id for u in actor_users]:
                actor_users.append(d.from_user)
        except Exception:
            pass

    if not any(_user_can_view_request(u, req) for u in actor_users):
        flash("غير مصرح لك بمراجعة هذا الطلب", "danger")
        return redirect(url_for("workflow.inbox"))

    # ✅ Mark related WORKFLOW notifications as read when the approver opens the request
    try:
        pending_notifs = (
            Notification.query
            .filter(
                Notification.user_id == current_user.id,
                Notification.is_mirror.is_(False),
                Notification.is_read.is_(False),
                Notification.type == "WORKFLOW",
                Notification.message.contains(f"#{req.id}")
            )
            .all()
        )
        if pending_notifs:
            for n in pending_notifs:
                n.is_read = True
                if n.event_key:
                    _sync_mirror_for_event(n.event_key)
            db.session.commit()
    except Exception:
        db.session.rollback()

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    template = WorkflowTemplate.query.get(inst.template_id) if inst else None


    steps = []
    current_step = None
    can_decide = False
    can_escalate = False

    if inst:
        steps = (
            WorkflowInstanceStep.query
            .filter_by(instance_id=inst.id)
            .order_by(WorkflowInstanceStep.step_order.asc())
            .all()
        )
        current_step = next((s for s in steps if s.step_order == inst.current_step_order), None)
        if current_step and current_step.status == "PENDING":
            can_decide = any(_user_can_act_on_step(u, current_step) for u in actor_users)

        # ✅ Allow escalation for viewers while workflow is still in progress
        try:
            can_escalate = not bool(getattr(inst, 'is_completed', False))
        except Exception:
            can_escalate = False

    # =========================
    # SLA helpers for UI (step SLA value + remaining days)
    # =========================
    step_sla_days_map = {}
    sla_days_remaining_map = {}
    try:
        if template:
            tsteps = WorkflowTemplateStep.query.filter_by(template_id=template.id).all()
            for ts in tsteps:
                try:
                    so = int(getattr(ts, 'step_order', 0) or 0)
                except Exception:
                    continue
                val = getattr(ts, 'sla_days', None)
                if val is None:
                    val = getattr(template, 'sla_days_default', None)
                step_sla_days_map[so] = val

        now = datetime.utcnow()
        for s in (steps or []):
            due = getattr(s, 'due_at', None)
            if not due:
                continue
            try:
                diff_sec = (due - now).total_seconds()
                days = int(math.ceil(diff_sec / 86400.0)) if diff_sec > 0 else 0
                sla_days_remaining_map[int(getattr(s, 'step_order', 0) or 0)] = days
            except Exception:
                continue
    except Exception:
        step_sla_days_map = {}
        sla_days_remaining_map = {}

    # Attachments (linked table)
    atts = RequestAttachment.query.filter_by(request_id=req.id).all()
    file_ids = [a.archived_file_id for a in atts]
    files_map = {}
    if file_ids:
        files = ArchivedFile.query.filter(ArchivedFile.id.in_(file_ids)).all()
        files_map = {f.id: f for f in files}

    audit = (
        AuditLog.query
        .filter_by(request_id=req.id)
        .order_by(AuditLog.created_at.desc())
        .limit(200)
        .all()
    )

    # attachment counts per step (best-effort via audit meta)
    step_att_counts = {}
    try:
        logs = (
            AuditLog.query
            .filter(AuditLog.request_id == req.id)
            .filter(AuditLog.action == 'WORKFLOW_ATTACHMENT_UPLOADED')
            .all()
        )
        for lg in logs:
            so, _src = _parse_attachment_meta(getattr(lg, 'note', None))
            if so is None:
                continue
            try:
                so = int(so)
            except Exception:
                continue
            step_att_counts[so] = step_att_counts.get(so, 0) + 1
    except Exception:
        pass

    # attachment upload logs map (file_id -> meta)
    log_map = {}
    try:
        if file_ids:
            logs = (
                AuditLog.query
                .filter(AuditLog.request_id == req.id)
                .filter(AuditLog.action == 'WORKFLOW_ATTACHMENT_UPLOADED')
                .filter(AuditLog.target_id.in_(file_ids))
                .order_by(AuditLog.created_at.asc())
                .all()
            )
            for lg in logs:
                fid = getattr(lg, 'target_id', None)
                if not fid:
                    continue
                fid = int(fid)
                if fid in log_map:
                    continue
                so, src = _parse_attachment_meta(getattr(lg, 'note', None))
                log_map[fid] = {
                    'step_order': so,
                    'source': src,
                    'uploaded_at': getattr(lg, 'created_at', None),
                    'user_id': getattr(lg, 'user_id', None),
                }
    except Exception:
        log_map = {}

    # maps for readable routing display
    users_map = {u.id: u for u in User.query.all()}
    depts_map = {d.id: d for d in Department.query.all()}
    dirs_map = {d.id: d for d in Directorate.query.all()}
    committees_map = {c.id: c for c in Committee.query.all()}
    mentioned_users = []
    try:
        mentioned_users = [
            users_map[uid]
            for uid in sorted(_mentioned_user_ids_for_request(req.id))
            if uid in users_map
        ]
    except Exception:
        mentioned_users = []

    def _human_size(num_bytes):
        try:
            n = int(num_bytes or 0)
        except Exception:
            n = 0
        if n < 1024:
            return f"{n} B"
        kb = n / 1024
        if kb < 1024:
            return f"{kb:.1f} KB"
        mb = kb / 1024
        if mb < 1024:
            return f"{mb:.1f} MB"
        gb = mb / 1024
        return f"{gb:.2f} GB"

    def _source_label(src):
        s = (src or '').strip().upper()
        if not s:
            return 'غير محدد'
        if s == 'CREATE':
            return 'إنشاء الطلب'
        if s == 'STEP_DECISION':
            return 'قرار خطوة'
        if s == 'ARCHIVE_UPLOAD':
            return 'رفع من الأرشيف'
        if s == 'MANUAL_UPLOAD':
            return 'رفع يدوي'
        if s == 'CORRESPONDENCE_CARD':
            return 'بطاقة PDF للمعاملة'
        if s == 'CORRESPONDENCE':
            return 'مرفق معاملة'
        if s == 'NOTE_COMMENT':
            return 'تعليق'
        if s == 'NOTE_REPLY':
            return 'رد'
        if s.startswith('NOTE_'):
            return 'تعليق/رد'
        return s

    pre_att_items = []
    step_att_items = {}
    unknown_att_items = []
    try:
        for a in atts:
            f = files_map.get(a.archived_file_id)
            if not f or getattr(f, 'is_deleted', False):
                continue

            meta = log_map.get(int(f.id), {})
            so = meta.get('step_order')
            src = meta.get('source')

            # Best-effort: consider create/archive/manual as step 0
            if so is None and src and str(src).strip().upper() in ('CREATE', 'ARCHIVE_UPLOAD', 'MANUAL_UPLOAD'):
                so = 0

            uploaded_at_dt = meta.get('uploaded_at') or getattr(f, 'upload_date', None)
            uploaded_at = uploaded_at_dt.strftime('%Y-%m-%d %H:%M') if uploaded_at_dt else None

            u = None
            uid = meta.get('user_id')
            if uid:
                u = users_map.get(int(uid))
            if u is None and getattr(f, 'owner_id', None):
                u = users_map.get(int(getattr(f, 'owner_id')))

            uploaded_by = None
            if u:
                uploaded_by = getattr(u, 'email', None) or getattr(u, 'full_name', None) or getattr(u, 'name', None)

            item = {
                'file_id': int(f.id),
                'name': getattr(f, 'original_name', None) or getattr(f, 'stored_name', None) or f"File #{f.id}",
                'size_human': _human_size(getattr(f, 'file_size', 0)),
                'file_type': getattr(f, 'file_type', None),
                'uploaded_by': uploaded_by,
                'uploaded_at': uploaded_at,
                'source_label': _source_label(src),
            }

            if so is None:
                unknown_att_items.append(item)
                continue

            try:
                so_int = int(so)
            except Exception:
                unknown_att_items.append(item)
                continue

            if so_int <= 0:
                pre_att_items.append(item)
            else:
                step_att_items.setdefault(so_int, []).append(item)
    except Exception:
        pre_att_items = []
        step_att_items = {}
        unknown_att_items = []


    # escalation counts per step
    step_esc_counts = {}
    total_escalations = 0
    try:
        escs = RequestEscalation.query.filter_by(request_id=req.id).all()
        for e in escs:
            total_escalations += 1
            so = getattr(e, 'step_order', None)
            if so is None:
                continue
            try:
                so_int = int(so)
            except Exception:
                continue
            step_esc_counts[so_int] = step_esc_counts.get(so_int, 0) + 1
    except Exception:
        step_esc_counts = {}
        total_escalations = 0
    # =========================
    # PARALLEL_SYNC UI helpers
    # =========================
    parallel_tasks = []
    my_parallel_task = None
    can_bypass_parallel = False
    try:
        if current_step and (getattr(current_step, "mode", "") or "").strip().upper() == "PARALLEL_SYNC":
            try:
                ensure_parallel_tasks(req, inst, current_step)
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception("Failed to ensure parallel tasks for request %s step %s", req.id, current_step.step_order)

            parallel_tasks = (
                WorkflowStepTask.query
                .options(joinedload(WorkflowStepTask.assignee))
                .filter_by(instance_id=inst.id, step_order=current_step.step_order)
                .order_by(WorkflowStepTask.assignee_user_id.asc())
                .all()
            )

            # Find my task (directly or via delegation identity)
            actor_ids = []
            try:
                actor_ids = [int(u.id) for u in (actor_users or []) if getattr(u, "id", None)]
            except Exception:
                actor_ids = []
            if not actor_ids:
                actor_ids = [int(current_user.id)]

            my_parallel_task = (
                WorkflowStepTask.query
                .filter(
                    WorkflowStepTask.instance_id == inst.id,
                    WorkflowStepTask.step_order == current_step.step_order,
                    WorkflowStepTask.assignee_user_id.in_(actor_ids),
                )
                .first()
            )

            last_actor_id = int(getattr(inst, "last_step_actor_id", 0) or 0)
            if current_user.has_role("ADMIN") or current_user.has_role("SUPER_ADMIN"):
                can_bypass_parallel = True
            elif last_actor_id and last_actor_id == int(current_user.id):
                can_bypass_parallel = True
            else:
                try:
                    for d in (delegations or []):
                        u = getattr(d, "from_user", None)
                        if u and int(u.id) == last_actor_id:
                            can_bypass_parallel = True
                            break
                except Exception:
                    pass
    except Exception:
        parallel_tasks = []
        my_parallel_task = None
        can_bypass_parallel = False

    # In PARALLEL_SYNC: show action buttons only to the assignee who still has a PENDING task.
    try:
        if current_step and (getattr(current_step, "mode", "") or "").strip().upper() == "PARALLEL_SYNC":
            if my_parallel_task and (getattr(my_parallel_task, "status", "") or "") == "PENDING":
                can_decide = True
            else:
                can_decide = False
    except Exception:
        pass


    return render_template(
        "workflow/view_request.html",
        req=req,
        inst=inst,
        steps=steps,
        current_step=current_step,
        can_decide=can_decide,
        can_escalate=can_escalate,
        attachments=atts,
        files_map=files_map,
        audit=audit,
        users_map=users_map,
        depts_map=depts_map,
        dirs_map=dirs_map,
        committees_map=committees_map,
        step_att_counts=step_att_counts,
        step_att_items=step_att_items,
        pre_att_items=pre_att_items,
        template=template,
        step_sla_days_map=step_sla_days_map,
        sla_days_remaining_map=sla_days_remaining_map,
        step_escalation_counts=step_esc_counts,
        total_escalations=total_escalations,
        parallel_tasks=parallel_tasks,
        my_parallel_task=my_parallel_task,
        can_bypass_parallel=can_bypass_parallel,
        mentioned_users=mentioned_users,
    )






# =========================
# Request Attachments (Step-aware view)
# =========================
@workflow_bp.route("/request/<int:request_id>/attachments")
@login_required
def request_attachments(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    

    # Delegation-aware viewer:
    # allow current_user + primary effective_user + ANY active delegator (if multiple delegations exist)
    effective_user = get_effective_user()
    can_view = _user_can_view_request(current_user, req) or _user_can_view_request(effective_user, req)
    if not can_view:
        try:
            for d in (get_active_delegations() or []):
                if d and getattr(d, "from_user", None) and _user_can_view_request(d.from_user, req):
                    can_view = True
                    break
        except Exception:
            pass

    if not can_view:
        abort(403)

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    template = WorkflowTemplate.query.get(inst.template_id) if inst else None

    atts = (
        RequestAttachment.query
        .options(joinedload(RequestAttachment.archived_file))
        .filter_by(request_id=req.id)
        .all()
    )

    files = []
    for a in atts:
        f = getattr(a, "archived_file", None)
        if f and not getattr(f, "is_deleted", False):
            files.append(f)

    total_count = len(files)

    def _human_size(num_bytes: int | None) -> str:
        n = int(num_bytes or 0)
        if n < 1024:
            return f"{n} B"
        kb = n / 1024
        if kb < 1024:
            return f"{kb:.1f} KB"
        mb = kb / 1024
        if mb < 1024:
            return f"{mb:.1f} MB"
        gb = mb / 1024
        return f"{gb:.2f} GB"

    def _source_label(src: str | None) -> str:
        s = (src or "").strip().upper()
        if not s:
            return "غير محدد"
        if s == "CREATE":
            return "إنشاء الطلب"
        if s == "STEP_DECISION":
            return "قرار خطوة"
        if s == "ARCHIVE_UPLOAD":
            return "رفع من الأرشيف"
        if s == "MANUAL_UPLOAD":
            return "رفع يدوي"
        if s == "CORRESPONDENCE_CARD":
            return "بطاقة PDF للمعاملة"
        if s == "CORRESPONDENCE":
            return "مرفق معاملة"
        if s == "NOTE_COMMENT":
            return "تعليق"
        if s == "NOTE_REPLY":
            return "رد"
        if s.startswith("NOTE_"):
            return "تعليق/رد"
        return s

    # Map each file to its first attachment-audit log (upload moment)
    file_ids = [f.id for f in files]
    log_map = {}

    user_ids = set()
    for f in files:
        if getattr(f, "owner_id", None):
            user_ids.add(int(f.owner_id))

    if file_ids:
        logs = (
            AuditLog.query
            .filter(AuditLog.request_id == req.id)
            .filter(AuditLog.action == "WORKFLOW_ATTACHMENT_UPLOADED")
            .filter(AuditLog.target_id.in_(file_ids))
            .order_by(AuditLog.created_at.asc())
            .all()
        )
        for lg in logs:
            fid = getattr(lg, "target_id", None)
            if fid and fid not in log_map:
                log_map[int(fid)] = lg
            if getattr(lg, "user_id", None):
                user_ids.add(int(lg.user_id))

    users_map = {}
    if user_ids:
        users = User.query.filter(User.id.in_(list(user_ids))).all()
        users_map = {u.id: u for u in users}

    items = []
    for f in files:
        lg = log_map.get(int(f.id))
        step_order = None
        source = None
        uploaded_at = None
        uploader = None

        if lg:
            step_order, source = _parse_attachment_meta(getattr(lg, "note", None))
            uploaded_at = getattr(lg, "created_at", None)
            uploader = users_map.get(getattr(lg, "user_id", None))

        # best-effort fallback
        if step_order is None and source and source.strip().upper() in ("CREATE", "ARCHIVE_UPLOAD"):
            step_order = 0

        if uploaded_at is None:
            uploaded_at = getattr(f, "upload_date", None)

        if uploader is None:
            uploader = users_map.get(getattr(f, "owner_id", None))

        uploaded_by = None
        if uploader:
            uploaded_by = uploader.email or uploader.name

        items.append({
            "file_id": int(f.id),
            "name": getattr(f, "original_name", None) or getattr(f, "stored_name", None) or f"File #{f.id}",
            "mime_type": getattr(f, "mime_type", None),
            "file_type": getattr(f, "file_type", None),
            "size_human": _human_size(getattr(f, "file_size", 0)),
            "uploaded_by": uploaded_by,
            "uploaded_at": (uploaded_at.strftime("%Y-%m-%d %H:%M") if uploaded_at else None),
            "step_order": step_order,
            "source_label": _source_label(source),
        })

    # Group by step order
    grouped = {}
    for it in items:
        so = it.get("step_order")
        key = 9999 if so is None else int(so)
        grouped.setdefault(key, []).append(it)

    groups = []
    for key in sorted(grouped.keys()):
        if key == 0:
            title = "📌 عند إنشاء الطلب / الإرفاق"
        elif key == 9999:
            title = "❓ غير محدد (مرفقات قديمة)"
        else:
            title = f"🧩 الخطوة {key}"

        groups.append({
            "key": key,
            "title": title,
            "items": grouped[key]
        })

    return render_template(
        "workflow/request_attachments.html",
        req=req,
        template=template,
        groups=groups,
        total_count=total_count,
    )


# =========================
# Request Escalations (Per request/step)
# =========================
@workflow_bp.route("/request/<int:request_id>/escalations")
@login_required
def request_escalations(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    

    # Delegation-aware viewer:
    # allow current_user + primary effective_user + ANY active delegator (if multiple delegations exist)
    effective_user = get_effective_user()
    can_view = _user_can_view_request(current_user, req) or _user_can_view_request(effective_user, req)
    if not can_view:
        try:
            for d in (get_active_delegations() or []):
                if d and getattr(d, "from_user", None) and _user_can_view_request(d.from_user, req):
                    can_view = True
                    break
        except Exception:
            pass

    if not can_view:
        abort(403)

    step_raw = (request.args.get('step') or '').strip()
    step_filter = None
    if step_raw and step_raw.isdigit():
        try:
            step_filter = int(step_raw)
        except Exception:
            step_filter = None

    q = (
        RequestEscalation.query
        .options(joinedload(RequestEscalation.from_user))
        .options(joinedload(RequestEscalation.to_user))
        .filter(RequestEscalation.request_id == req.id)
    )

    if step_filter is not None:
        q = q.filter(RequestEscalation.step_order == step_filter)

    escalations = q.order_by(RequestEscalation.created_at.desc()).all()

    users_map = {u.id: u for u in User.query.all()}
    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    template = WorkflowTemplate.query.get(inst.template_id) if inst else None

    return render_template(
        "workflow/request_escalations.html",
        req=req,
        inst=inst,
        template=template,
        escalations=escalations,
        users_map=users_map,
        step_filter=step_filter,
    )


# =========================
# Delete Request (SUPER_ADMIN only)
# =========================
@workflow_bp.route("/request/<int:request_id>/delete", methods=["POST"])
@login_required
@roles_required("SUPER_ADMIN")
def delete_request(request_id):
    """Hard-delete a request (Super Admin only) while preserving audit trail."""
    req = WorkflowRequest.query.get_or_404(request_id)

    rid = req.id
    requester_id = req.requester_id

    # Collect recipients who have received/handled the request so far (steps + commenters)
    recipients_user_ids = set()
    roles_to_notify = set()

    if requester_id:
        recipients_user_ids.add(int(requester_id))

    try:
        # Anyone who wrote an audit entry on this request (best-effort)
        audit_uids = (
            db.session.query(AuditLog.user_id)
            .filter(AuditLog.request_id == rid)
            .filter(AuditLog.user_id.isnot(None))
            .distinct()
            .all()
        )
        for (uid,) in audit_uids:
            if uid:
                recipients_user_ids.add(int(uid))
    except Exception:
        pass

    inst_for_notify = WorkflowInstance.query.filter_by(request_id=rid).first()
    if inst_for_notify:
        steps_q = WorkflowInstanceStep.query.filter_by(instance_id=inst_for_notify.id)
        if inst_for_notify.current_step_order:
            steps_q = steps_q.filter(WorkflowInstanceStep.step_order <= int(inst_for_notify.current_step_order))
        steps_for_notify = steps_q.all()

        for st in steps_for_notify:
            # who decided
            if getattr(st, 'decided_by_id', None):
                recipients_user_ids.add(int(st.decided_by_id))

            # who should have received the step
            kind = (getattr(st, 'approver_kind', None) or '').upper()
            if kind == 'USER' and getattr(st, 'approver_user_id', None):
                recipients_user_ids.add(int(st.approver_user_id))
            elif kind == 'ROLE' and getattr(st, 'approver_role', None):
                roles_to_notify.add((st.approver_role or '').strip())
            elif kind == 'DEPARTMENT' and getattr(st, 'approver_department_id', None):
                dept_id = int(st.approver_department_id)
                dept_users = (
                    User.query
                    .filter(User.department_id == dept_id)
                    .filter(User.role.in_(['dept_head', 'deputy_head']))
                    .all()
                )
                for u in dept_users:
                    recipients_user_ids.add(int(u.id))
            elif kind == 'DIRECTORATE' and getattr(st, 'approver_directorate_id', None):
                dir_id = int(st.approver_directorate_id)
                dept_ids = [d.id for d in Department.query.filter(Department.directorate_id == dir_id).all()]
                if dept_ids:
                    dir_users = (
                        User.query
                        .filter(User.department_id.in_(dept_ids))
                        .filter(User.role.in_(['directorate_head']))
                        .all()
                    )
                    for u in dir_users:
                        recipients_user_ids.add(int(u.id))

    # Do not notify the deleter themselves
    try:
        recipients_user_ids.discard(int(current_user.id))
    except Exception:
        pass

    try:
        # Build a lightweight snapshot before deleting (for audit + timeline preview)
        snapshot = None
        try:
            inst_snapshot = WorkflowInstance.query.filter_by(request_id=rid).first()
            template_snapshot = WorkflowTemplate.query.get(inst_snapshot.template_id) if (inst_snapshot and inst_snapshot.template_id) else None

            steps_snapshot = []
            if inst_snapshot:
                steps_rows = (
                    WorkflowInstanceStep.query
                    .filter_by(instance_id=inst_snapshot.id)
                    .order_by(WorkflowInstanceStep.step_order.asc())
                    .all()
                )
                # Resolve decided_by emails (best effort)
                decided_by_ids = {int(s.decided_by_id) for s in steps_rows if getattr(s, "decided_by_id", None)}
                decided_map = {}
                if decided_by_ids:
                    for u in User.query.filter(User.id.in_(decided_by_ids)).all():
                        decided_map[int(u.id)] = (u.email or u.name or str(u.id))
                for s in steps_rows:
                    steps_snapshot.append({
                        "step_order": s.step_order,
                        "mode": getattr(s, "mode", "SEQUENTIAL"),
                        "approver_kind": getattr(s, "approver_kind", None),
                        "status": getattr(s, "status", None),
                        "decided_by_id": getattr(s, "decided_by_id", None),
                        "decided_by_email": decided_map.get(int(s.decided_by_id)) if getattr(s, "decided_by_id", None) else None,
                        "decided_at": s.decided_at.isoformat() if getattr(s, "decided_at", None) else None,
                    })

            approvals_snapshot = []
            for a in (
                Approval.query.filter_by(request_id=rid)
                .order_by(Approval.created_at.asc(), Approval.id.asc())
                .all()
            ):
                approvals_snapshot.append({
                    "user_id": a.user_id,
                    "action": a.action,
                    "note": a.note,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                })

            requester_obj = req.requester
            snapshot = {
                "id": rid,
                "title": req.title,
                "description": req.description,
                "status": req.status,
                "created_at": req.created_at.isoformat() if req.created_at else None,
                "requester": {
                    "id": requester_obj.id,
                    "name": requester_obj.name,
                    "email": requester_obj.email,
                    "role": requester_obj.role,
                } if requester_obj else None,
                "request_type": {
                    "id": req.request_type.id,
                    "code": req.request_type.code,
                    "name_ar": req.request_type.name_ar,
                    "name_en": req.request_type.name_en,
                } if getattr(req, "request_type", None) else None,
                "instance": {
                    "id": inst_snapshot.id,
                    "template_id": inst_snapshot.template_id,
                    "template_name": template_snapshot.name if template_snapshot else None,
                    "current_step_order": inst_snapshot.current_step_order,
                    "is_completed": inst_snapshot.is_completed,
                    "last_step_actor_id": inst_snapshot.last_step_actor_id,
                    "last_step_actor_email": inst_snapshot.last_step_actor.email if getattr(inst_snapshot, "last_step_actor", None) else None,
                } if inst_snapshot else None,
                "steps": steps_snapshot,
                "approvals": approvals_snapshot,
                "deleted_at": datetime.utcnow().isoformat(),
                "deleted_by": current_user.email,
            }
        except Exception:
            snapshot = None

        # Detach previous audit entries so FK won't block deletion

        AuditLog.query.filter(AuditLog.request_id == rid).update(
            {
                AuditLog.request_id: None,
                AuditLog.target_type: "WorkflowRequest",
                AuditLog.target_id: rid,
            },
            synchronize_session=False
        )

        # Delete dependent workflow rows
        Approval.query.filter_by(request_id=rid).delete(synchronize_session=False)
        RequestEscalation.query.filter_by(request_id=rid).delete(synchronize_session=False)
        RequestAttachment.query.filter_by(request_id=rid).delete(synchronize_session=False)

        inst = WorkflowInstance.query.filter_by(request_id=rid).first()
        if inst:
            # WorkflowInstanceStep uses instance_id (no request_id)
            WorkflowInstanceStep.query.filter_by(instance_id=inst.id).delete(synchronize_session=False)
            db.session.delete(inst)

        # Create deletion audit log (without request_id FK)
        db.session.add(AuditLog(
            action="REQUEST_DELETED",
            user_id=current_user.id,
            target_type="WorkflowRequest",
            target_id=rid,
            note=(f"Request #{rid} deleted by {current_user.email}\n" + (f"SNAPSHOT_JSON:{json.dumps(snapshot, ensure_ascii=False)}" if snapshot else ""))
        ))

        # Notify requester + all parties who received the request so far (steps/handlers)
        message = f"تم حذف الطلب رقم #{rid} بواسطة الإدارة."

        for uid in sorted(recipients_user_ids):
            try:
                emit_event(
                    actor_id=current_user.id,
                    action="REQUEST_DELETED",
                    message=message,
                    target_type="WorkflowRequest",
                    target_id=rid,
                    notify_user_id=int(uid),
                    level="WARNING",
                    auto_commit=False,
                )
            except Exception:
                pass

        for role in sorted({r for r in roles_to_notify if (r or '').strip()}):
            try:
                emit_event(
                    actor_id=current_user.id,
                    action="REQUEST_DELETED",
                    message=message,
                    target_type="WorkflowRequest",
                    target_id=rid,
                    notify_role=(role or '').strip(),
                    level="WARNING",
                    auto_commit=False,
                )
            except Exception:
                pass

        # Finally delete the request
        db.session.delete(req)
        db.session.commit()

        flash("✅ تم حذف الطلب نهائياً.", "success")
        return redirect(url_for("workflow.inbox"))

    except Exception as e:
        db.session.rollback()
        flash(f"تعذّر حذف الطلب: {e}", "danger")
        return redirect(url_for("workflow.view_request", request_id=rid))

@workflow_bp.route("/request/<int:request_id>/escalate", methods=["GET", "POST"])
@login_required
def escalate_request(request_id):
    """Escalate the current workflow step.

    Behavior:
    - Escalation is routed automatically to the current step assignee(s) (who can act now)
      PLUS the directorate head of the related directorate.
    - Creates a warning notification, and also a message in internal 'Messages' inbox.
    - Stores escalation with step_order and targets for traceability.
    """
    req = WorkflowRequest.query.get_or_404(request_id)

    

    # Delegation-aware viewer:
    # allow current_user + primary effective_user + ANY active delegator (if multiple delegations exist)
    effective_user = get_effective_user()
    can_view = _user_can_view_request(current_user, req) or _user_can_view_request(effective_user, req)
    if not can_view:
        try:
            for d in (get_active_delegations() or []):
                if d and getattr(d, "from_user", None) and _user_can_view_request(d.from_user, req):
                    can_view = True
                    break
        except Exception:
            pass

    if not can_view:
        flash("غير مصرح لك بالوصول لهذا الطلب", "danger")
        return redirect(url_for("workflow.inbox"))

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    template = WorkflowTemplate.query.get(inst.template_id) if inst else None

    if not inst or getattr(inst, 'is_completed', False):
        flash("لا يمكن تصعيد طلب مكتمل أو بدون مسار.", "warning")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    steps = (
        WorkflowInstanceStep.query
        .filter_by(instance_id=inst.id)
        .order_by(WorkflowInstanceStep.step_order.asc())
        .all()
    )
    current_step = next((s for s in steps if s.step_order == inst.current_step_order), None)

    if not current_step or current_step.status != "PENDING":
        flash("لا توجد خطوة حالية قابلة للتصعيد.", "warning")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    # Recipients: current step actor(s) + directorate_head
    step_recips = _step_actor_user_ids(current_step)
    dir_id = _infer_directorate_id_for_step(req, current_step)
    dir_head_ids = _directorate_head_user_ids(dir_id)

    base_recipient_ids = sorted({int(rid) for rid in (step_recips + dir_head_ids) if rid and int(rid) != int(current_user.id)})

    # Delegation-aware escalation recipients:
    # if any primary recipient (الأصيل) has an active delegation now, notify the delegatee too.
    recipient_ids = list(base_recipient_ids)
    delegation_pairs = []  # (from_user_id, to_user_id)
    try:
        from models import Delegation
        now = datetime.now()
        if base_recipient_ids:
            delegated_rows = (
                Delegation.query
                .filter(
                    Delegation.from_user_id.in_(base_recipient_ids),
                    Delegation.is_active.is_(True),
                    Delegation.starts_at <= now,
                    Delegation.expires_at >= now,
                )
                .all()
            )
            for d in (delegated_rows or []):
                try:
                    fid = int(getattr(d, "from_user_id", 0) or 0)
                    tid = int(getattr(d, "to_user_id", 0) or 0)
                    if tid and tid != int(current_user.id):
                        recipient_ids.append(tid)
                        delegation_pairs.append((fid, tid))
                except Exception:
                    pass
    except Exception:
        pass

    recipient_ids = sorted({int(rid) for rid in recipient_ids if rid and int(rid) != int(current_user.id)})

    recipient_users = []
    if recipient_ids:
        recipient_users = User.query.filter(User.id.in_(recipient_ids)).order_by(User.email.asc()).all()

    # For display
    users_map = {u.id: u for u in User.query.all()}
    depts_map = {d.id: d for d in Department.query.all()}
    dirs_map = {d.id: d for d in Directorate.query.all()}
    units_map = {u.id: u for u in Unit.query.all()}
    sections_map = {s.id: s for s in Section.query.all()}
    divisions_map = {d.id: d for d in Division.query.all()}
    committees_map = {c.id: c for c in Committee.query.all()}
    target_label = _step_target_label(current_step, users_map=users_map, depts_map=depts_map, dirs_map=dirs_map, units_map=units_map, sections_map=sections_map, divisions_map=divisions_map, committees_map=committees_map)

    categories = [
        "SLA_RISK",          # خطر تجاوز SLA
        "URGENT",            # عاجل
        "MISSING_INFO",      # نقص معلومات
        "BLOCKED",           # معيق/متوقف
        "CONFLICT",          # تعارض/خلاف
        "NEED_GUIDANCE",     # بحاجة لتوجيه
        "OTHER",             # أخرى
    ]

    if request.method == "POST":
        category = (request.form.get("category") or "").strip()
        desc = (request.form.get("description") or "").strip()

        if category not in categories:
            flash("اختر نوع تصعيد صحيح.", "danger")
            return redirect(request.url)

        if not desc:
            flash("وصف التصعيد مطلوب.", "danger")
            return redirect(request.url)

        if not recipient_ids:
            flash("لا يوجد مستلمون للخطوة الحالية لإرسال التصعيد إليهم.", "danger")
            return redirect(request.url)

        primary_to = int((base_recipient_ids[0] if 'base_recipient_ids' in locals() and base_recipient_ids else recipient_ids[0]))
        targets_str = ",".join(str(i) for i in recipient_ids)

        # Record escalation
        esc = RequestEscalation(
            request_id=req.id,
            from_user_id=current_user.id,
            to_user_id=primary_to,
            category=category,
            description=desc,
            step_order=int(getattr(current_step, 'step_order', 0) or 0),
            targets=targets_str,
        )
        db.session.add(esc)

        # Mark request escalated (legacy flags)
        try:
            req.is_escalated = True
            req.escalated_at = datetime.utcnow()
        except Exception:
            pass

        # Build message (internal communications)
        link = _build_absolute_url(url_for('workflow.view_request', request_id=req.id))
        due = getattr(current_step, 'due_at', None)
        due_str = due.strftime('%Y-%m-%d %H:%M') if due else 'غير محدد'
        remaining_days = None
        try:
            if due:
                diff = (due - datetime.utcnow()).total_seconds()
                remaining_days = int(math.ceil(diff / 86400.0)) if diff > 0 else 0
        except Exception:
            remaining_days = None

        rec_emails = []
        try:
            rec_emails = [u.email for u in recipient_users if getattr(u, 'email', None)]
        except Exception:
            rec_emails = []

        subject = f"🚨 تصعيد على الطلب #{req.id} (الخطوة {current_step.step_order})"
        body_lines = [
            "تم تسجيل تصعيد على مسار الطلب (Warning).",
            "",
            f"الطلب: #{req.id} — {req.title or ''}",
            f"المسار: {template.name if template else '-'}",
            f"الخطوة الحالية: {current_step.step_order}",
            f"الجهة المسؤولة: {target_label}",
            f"موعد SLA لهذه الخطوة: {due_str}" + (f" (متبقي {remaining_days} يوم)" if remaining_days is not None else ""),
            "",
            f"سبب/شرح التصعيد (من {current_user.email}):",
            desc,
            "",
        ]

        # إذا كان أحد المستلمين لديه تفويض فعّال، أرسلنا نسخة أيضاً للمفوّض إليه
        if delegation_pairs:
            body_lines.append("تم إرسال نسخة للمفوّض إليه بسبب تفويض فعّال.")
            seen_pairs = set()
            for fid, tid in delegation_pairs:
                if (fid, tid) in seen_pairs:
                    continue
                seen_pairs.add((fid, tid))
                fu = users_map.get(fid)
                tu = users_map.get(tid)
                ftxt = (getattr(fu, "email", None) or getattr(fu, "full_name", None) or str(fid))
                ttxt = (getattr(tu, "email", None) or getattr(tu, "full_name", None) or str(tid))
                body_lines.append(f"- {ftxt} → {ttxt}")
            body_lines.append("")

        body_lines += [
            "تم إرسال هذا التنبيه إلى:",
            ("- " + "\n- ".join(rec_emails)) if rec_emails else "- (غير متاح)",
            "",
            "رابط للاطلاع فقط:",
            link,
        ]
        body = "\n".join(body_lines)

        # choose a target_kind/id that fits schema
        target_kind = "DIRECTORATE" if dir_id else "USER"
        target_id = int(dir_id) if dir_id else primary_to

        msg = Message(
            sender_id=current_user.id,
            subject=subject,
            body=body,
            target_kind=target_kind,
            target_id=target_id,
            created_at=datetime.utcnow(),
            reply_to_id=None,
        )
        db.session.add(msg)
        db.session.flush()

        # recipients
        rows = [
            MessageRecipient(
                message_id=msg.id,
                recipient_user_id=int(rid),
                is_read=False,
                read_at=None,
                is_deleted=False,
                deleted_at=None,
            )
            for rid in recipient_ids
        ]
        db.session.add_all(rows)

        # Audit
        db.session.add(
            AuditLog(
                request_id=req.id,
                user_id=current_user.id,
                action="REQUEST_ESCALATION",
                note=f"Escalation step={current_step.step_order} ({category}) targets={targets_str}: {desc[:200]}",
                target_type="WorkflowRequest",
                target_id=req.id,
                created_at=datetime.utcnow(),
            )
        )

        # Notifications (bell + SSE)
        for rid in recipient_ids:
            try:
                emit_event(
                    actor_id=current_user.id,
                    action="REQUEST_ESCALATION",
                    message=f"🚨 تصعيد يحتاج انتباه: # {req.id} (الخطوة {current_step.step_order})",
                    target_type="WorkflowRequest",
                    target_id=req.id,
                    notify_user_id=int(rid),
                    level="WARNING",
                    auto_commit=False,
                )
            except Exception:
                pass

        try:
            db.session.commit()
            flash("تم إرسال التصعيد بنجاح (Warning).", "warning")
            return redirect(url_for("workflow.view_request", request_id=req.id))
        except Exception as e:
            db.session.rollback()
            flash(f"تعذر إرسال التصعيد: {e}", "danger")

    return render_template(
        "workflow/escalate.html",
        req=req,
        categories=categories,
        recipient_users=recipient_users,
        recipients=recipient_users,
        step_order=(current_step.step_order if current_step else None),
        current_step=current_step,
        target_label=target_label,
        template=template,
    )

# =========================
# Decide Step (Approve/Reject)
# =========================
@workflow_bp.route("/request/<int:request_id>/step/<int:step_order>/decide", methods=["POST"])
@login_required
def decide_request_step(request_id, step_order):
    req = WorkflowRequest.query.get_or_404(request_id)
    effective_user = get_effective_user()
    delegations = get_active_delegations()

    actor_users = [current_user]
    for d in (delegations or []):
        try:
            if d and getattr(d, "from_user", None) and d.from_user.id not in [u.id for u in actor_users]:
                actor_users.append(d.from_user)
        except Exception:
            pass

    if not any(_user_can_view_request(u, req) for u in actor_users):
        abort(403)

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    if not inst:
        flash("لا يوجد مسار عمل لهذا الطلب.", "warning")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    step = WorkflowInstanceStep.query.filter_by(
        instance_id=inst.id,
        step_order=step_order
    ).first()

    if not step:
        flash("الخطوة غير موجودة.", "danger")
        return redirect(url_for("workflow.view_request", request_id=req.id))
    # Determine which identity the current user is acting as
    acting_user = None
    used_delegation = None

    if _user_can_act_on_step(current_user, step):
        acting_user = current_user
    else:
        for d in (delegations or []):
            u = getattr(d, "from_user", None)
            if u and _user_can_act_on_step(u, step):
                acting_user = u
                used_delegation = d
                break

    if not acting_user:
        abort(403)

    decision = (request.form.get("decision") or "").strip().upper()
    note = (request.form.get("note") or "").strip()

    if decision not in ("APPROVED", "REJECTED"):
        flash("قرار غير صالح.", "danger")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    saved_paths = []

    delegation = used_delegation

    try:
        decide_step(req.id, step_order, current_user.id, decision,
                  note=note, auto_commit=False,
                  effective_user_id=acting_user.id,
                  on_behalf_of_id=(acting_user.id if acting_user.id != current_user.id else None),
                  delegation_id=(delegation.id if delegation and acting_user.id != current_user.id else None))

        # Attach files uploaded with the decision (multiple)
        uploaded_files = request.files.getlist("files") if request.files else []
        uploaded_files = [f for f in (uploaded_files or []) if f and getattr(f, "filename", "")]
        for fs in uploaded_files:
            archived, saved_path = _save_upload_to_archive(
                fs, owner_id=current_user.id, visibility="workflow", description=None
            )
            if hasattr(archived, "workflow_request_id"):
                setattr(archived, "workflow_request_id", req.id)

            db.session.add(archived)
            db.session.flush()
            saved_paths.append(saved_path)

            db.session.add(RequestAttachment(request_id=req.id, archived_file_id=archived.id))
            _audit_attachment(
                req_id=req.id,
                file_id=archived.id,
                step_order=step_order,
                source="STEP_DECISION",
                original_name=archived.original_name,
                uploaded_by_id=current_user.id,
            )

        db.session.commit()
        flash("تم حفظ الإجراء بنجاح.", "success")
    except Exception as e:
        db.session.rollback()
        for sp in saved_paths:
            try:
                if os.path.exists(sp):
                    os.remove(sp)
            except Exception:
                pass
        flash(f"خطأ أثناء حفظ الإجراء: {e}", "danger")

    return redirect(url_for("workflow.view_request", request_id=req.id))


# =========================
# PARALLEL_SYNC - Bypass assignee
# =========================
@workflow_bp.route("/request/<int:request_id>/step/<int:step_order>/parallel/bypass", methods=["POST"])
@login_required
def bypass_parallel_assignee(request_id: int, step_order: int):
    req = WorkflowRequest.query.get_or_404(request_id)

    # Delegation-aware viewer
    effective_user = get_effective_user()
    can_view = _user_can_view_request(current_user, req) or _user_can_view_request(effective_user, req)
    if not can_view:
        try:
            for d in (get_active_delegations() or []):
                if d and getattr(d, "from_user", None) and _user_can_view_request(d.from_user, req):
                    can_view = True
                    break
        except Exception:
            pass
    if not can_view:
        abort(403)

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    if not inst:
        abort(404)

    step = WorkflowInstanceStep.query.filter_by(instance_id=inst.id, step_order=step_order).first()
    if not step:
        abort(404)

    if (getattr(step, "mode", "") or "").strip().upper() != "PARALLEL_SYNC":
        flash("هذه ليست خطوة متزامنة.", "warning")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    # Who is allowed to bypass?
    acting_user = None
    used_delegation = None

    if current_user.has_role("ADMIN") or current_user.has_role("SUPER_ADMIN"):
        acting_user = current_user
    elif int(getattr(inst, "last_step_actor_id", 0) or 0) == int(current_user.id):
        acting_user = current_user
    else:
        try:
            for d in (get_active_delegations() or []):
                u = getattr(d, "from_user", None)
                if u and int(getattr(inst, "last_step_actor_id", 0) or 0) == int(u.id):
                    acting_user = u
                    used_delegation = d
                    break
        except Exception:
            pass

    if not acting_user:
        abort(403)

    bypass_all = (request.form.get("bypass_all") or "").strip() == "1"

    raw_assignee = (request.form.get("assignee_user_id") or "").strip()
    assignee_user_id = 0
    if raw_assignee:
        try:
            assignee_user_id = int(raw_assignee)
        except Exception:
            assignee_user_id = 0

    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("سبب التجاوز مطلوب.", "warning")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    # If bypass_all=1 -> bypass all remaining pending assignees
    if bypass_all:
        try:
            bypass_all_parallel_tasks(
                request_id=req.id,
                step_order=step_order,
                actor_user_id=current_user.id,
                effective_user_id=acting_user.id,
                reason=reason,
                on_behalf_of_id=(acting_user.id if acting_user.id != current_user.id else None),
                auto_commit=False,
            )
            db.session.commit()
            flash("تم تجاوز جميع المتبقين في الخطوة المتزامنة.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"تعذّر تجاوز المتبقين: {e}", "danger")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    if not assignee_user_id:
        flash("يرجى اختيار مستخدم للتجاوز أو استخدام (تجاوز المتبقين).", "warning")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    try:
        bypass_parallel_task(
            request_id=req.id,
            step_order=step_order,
            actor_user_id=current_user.id,
            effective_user_id=acting_user.id,
            assignee_user_id=assignee_user_id,
            reason=reason,
            on_behalf_of_id=(acting_user.id if acting_user.id != current_user.id else None),
            auto_commit=False,
        )
        db.session.commit()
        flash("تم تنفيذ التجاوز بنجاح.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"تعذّر تنفيذ التجاوز: {e}", "danger")

    return redirect(url_for("workflow.view_request", request_id=req.id))



# =========================
# Add Note / Comment (without decision)
# =========================
@workflow_bp.route("/request/<int:request_id>/note", methods=["POST"])
@login_required
def add_request_note(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    

    # Delegation-aware viewer:
    # allow current_user + primary effective_user + ANY active delegator (if multiple delegations exist)
    effective_user = get_effective_user()
    can_view = _user_can_view_request(current_user, req) or _user_can_view_request(effective_user, req)
    if not can_view:
        try:
            for d in (get_active_delegations() or []):
                if d and getattr(d, "from_user", None) and _user_can_view_request(d.from_user, req):
                    can_view = True
                    break
        except Exception:
            pass

    if not can_view:
        abort(403)

    note = (request.form.get("note") or "").strip()
    kind = (request.form.get("kind") or "COMMENT").strip().upper()

    uploaded_files = request.files.getlist("files") if request.files else []
    uploaded_files = [f for f in (uploaded_files or []) if f and getattr(f, "filename", "")]

    if not note and not uploaded_files:
        flash("يرجى كتابة نص أو إرفاق ملف/ملفات.", "warning")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    if kind not in ("COMMENT", "REPLY"):
        kind = "COMMENT"

    # determine current step order (best-effort)
    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    step_order = None
    if inst and inst.current_step_order:
        step_order = int(inst.current_step_order)

    saved_paths = []
    try:
        # 1) Save note (if any)
        if note:
            db.session.add(AuditLog(
                request_id=req.id,
                user_id=current_user.id,
                action=f"WORKFLOW_{kind}",
                old_status=req.status,
                new_status=req.status,
                note=note,
                target_type="WorkflowRequest",
                target_id=req.id,
            ))

        # 2) Save attachments (if any)
        attached_count = 0
        for fs in uploaded_files:
            archived, saved_path = _save_upload_to_archive(
                fs, owner_id=current_user.id, visibility="workflow", description=None
            )
            if hasattr(archived, "workflow_request_id"):
                setattr(archived, "workflow_request_id", req.id)

            db.session.add(archived)
            db.session.flush()
            saved_paths.append(saved_path)

            db.session.add(RequestAttachment(request_id=req.id, archived_file_id=archived.id))
            _audit_attachment(
                req_id=req.id,
                file_id=archived.id,
                step_order=step_order,
                source=f"NOTE_{kind}",
                original_name=archived.original_name,
                uploaded_by_id=current_user.id,
            )
            attached_count += 1

        # 3) Mentions: grant access + notify mentioned users/roles.
        mentioned_users, unresolved_mentions = _grant_mention_access(
            req,
            inst,
            note,
            step_order=step_order,
        )

        actor_label = current_user.email

        # 4) Notifications
        # Build message (note + attachments)
        msg_parts = []
        if note:
            msg_parts.append(note)
        if attached_count:
            msg_parts.append(f"📎 مرفقات جديدة: {attached_count}")
        msg_tail = " | ".join(msg_parts) if msg_parts else "📎 مرفقات جديدة"

        notified_user_ids = set()

        if current_user.id == req.requester_id:
            # Requester -> notify current pending approvers (if any)
            target_ids = []
            if inst:
                st = WorkflowInstanceStep.query.filter_by(
                    instance_id=inst.id,
                    step_order=inst.current_step_order
                ).first()
                if st and st.status == "PENDING":
                    if st.approver_kind == "USER" and st.approver_user_id:
                        target_ids = [int(st.approver_user_id)]
                    elif st.approver_kind == "ROLE" and st.approver_role:
                        users = User.query.filter(User.role.ilike((st.approver_role or "").strip())).all()
                        target_ids = [u.id for u in users]
                    elif st.approver_kind == "DEPARTMENT" and st.approver_department_id:
                        users = User.query.filter(
                            User.department_id == st.approver_department_id,
                            User.role.ilike("dept_head")
                        ).all()
                        target_ids = [u.id for u in users]

            for uid in set(target_ids):
                notified_user_ids.add(int(uid))
                emit_event(
                    actor_id=current_user.id,
                    action="WORKFLOW_REQUESTER_NOTE",
                    message=f"تحديث من مقدم الطلب على الطلب #{req.id}: {msg_tail}",
                    target_type="WorkflowRequest",
                    target_id=req.id,
                    notify_user_id=uid,
                    level="WORKFLOW",
                    track_for_actor=True,
                    auto_commit=False,
                )

        else:
            # Reviewer -> notify requester
            label = "تعليق" if kind == "COMMENT" else "رد"
            notified_user_ids.add(int(req.requester_id))
            emit_event(
                actor_id=current_user.id,
                action="WORKFLOW_NOTE",
                message=f"{label} على طلبك #{req.id} من {actor_label}: {msg_tail}",
                target_type="WorkflowRequest",
                target_id=req.id,
                notify_user_id=req.requester_id,
                level="WORKFLOW",
                track_for_actor=True,
                auto_commit=False,
            )

        # ✅ Followers: notify previous approvers so they stay informed
        try:
            followers = _get_request_followers_user_ids(req.id)
            followers.discard(int(current_user.id))
            # avoid duplicates for users already notified above
            for uid in notified_user_ids:
                followers.discard(int(uid))
            if followers:
                label2 = "تحديث" if current_user.id == req.requester_id else ("تعليق" if kind == "COMMENT" else "رد")
                for uid in sorted(followers):
                    emit_event(
                        actor_id=current_user.id,
                        action="WORKFLOW_FOLLOWER_UPDATE",
                        message=f"{label2} على الطلب #{req.id} من {actor_label}: {msg_tail}",
                        target_type="WorkflowRequest",
                        target_id=req.id,
                        notify_user_id=int(uid),
                        level="WORKFLOW",
                        auto_commit=False,
                    )
        except Exception:
            pass

        db.session.commit()
        flash("تم إرسال التحديث بنجاح.", "success")
        if mentioned_users:
            labels = ", ".join([(u.full_name or u.email or f"#{u.id}") for u in mentioned_users[:5]])
            extra = "" if len(mentioned_users) <= 5 else f" +{len(mentioned_users) - 5}"
            flash(f"تمت إضافة المذكورين إلى المسار: {labels}{extra}", "info")
        if unresolved_mentions:
            flash(f"لم يتم العثور على: {', '.join(unresolved_mentions[:5])}", "warning")

    except Exception as e:
        db.session.rollback()
        for sp in saved_paths:
            try:
                if os.path.exists(sp):
                    os.remove(sp)
            except Exception:
                pass
        flash(f"خطأ أثناء إرسال التحديث: {e}", "danger")

    return redirect(url_for("workflow.view_request", request_id=req.id))
