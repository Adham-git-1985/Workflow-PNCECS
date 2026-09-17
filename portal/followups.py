"""Employee accomplishment reports and responsible-manager review workflow."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import mimetypes
from pathlib import Path
import shutil
import uuid

from flask import abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import aliased

from extensions import db
from models import (
    AuditLog,
    EmployeeFile,
    EmployeeResponsibleAssignment,
    EmployeeFollowupAttachment,
    EmployeeFollowupItem,
    EmployeeFollowupReport,
    Notification,
    PortalMeetingTask,
    User,
)
from services.followup_assistant import build_followup_analysis
from services.followup_docx import DOCX_MIME, build_followup_docx, is_valid_docx
from services.hr_request_workflow import (
    resolve_responsible_managers,
    secretary_general_user_ids,
)
from utils.file_uploads import clean_original_filename, random_storage_name
from utils.notification_links import notification_target_path
from utils.role_codes import canonical_role_key

from . import portal_bp


FOLLOWUPS_READ = "FOLLOWUPS_READ"
FOLLOWUPS_CREATE = "FOLLOWUPS_CREATE"
FOLLOWUPS_REVIEW = "FOLLOWUPS_REVIEW"
FOLLOWUPS_MANAGE = "FOLLOWUPS_MANAGE"

REPORT_STATUS_LABELS = {
    "DRAFT": "مسودة",
    "SUBMITTED": "مرسل للمدير",
    "NEEDS_REVISION": "يحتاج تعديل",
    "REVIEWED": "تمت المراجعة",
}
ITEM_STATUS_LABELS = {
    "COMPLETED": "منجز",
    "INCOMPLETE": "غير مكتمل",
    "IN_PROGRESS": "قيد التنفيذ",
}
RATING_LABELS = {
    "EXCELLENT": "ممتاز",
    "GOOD": "جيد",
    "NEEDS_SUPPORT": "يحتاج دعم",
}
WORKFLOW_ACCOMPLISHMENT_ACTIONS = {
    "WORKFLOW_STARTED": "بدء معاملة",
    "STEP_APPROVED": "متابعة واعتماد خطوة",
    "STEP_REJECTED": "اتخاذ قرار في خطوة",
    "PARALLEL_SYNC_RESPONDED": "متابعة خطوة متزامنة",
    "WORKFLOW_COMPLETED": "إكمال مسار معاملة",
}


def _has_permission(permission: str) -> bool:
    try:
        return bool(current_user.has_perm(permission))
    except Exception:
        return False


def _can_create() -> bool:
    return _has_permission(FOLLOWUPS_CREATE) or _has_permission(FOLLOWUPS_MANAGE)


def _can_review() -> bool:
    return _has_permission(FOLLOWUPS_REVIEW) or _has_permission(FOLLOWUPS_MANAGE)


def _can_manage_all() -> bool:
    return _has_permission(FOLLOWUPS_MANAGE)


def _is_super_admin_account(user: User | None = None) -> bool:
    selected_user = user or current_user
    role_key = canonical_role_key(getattr(selected_user, "role", None))
    if role_key.startswith("SUPER") or role_key in {"ROOT", "SYSADMIN", "SYSTEMADMIN"}:
        return True
    try:
        return bool(
            selected_user.has_role("SUPER_ADMIN")
            or selected_user.has_role("SUPERADMIN")
        )
    except Exception:
        return False


def _has_secretary_general_role(user: User | None = None) -> bool:
    selected_user = user or current_user
    role_key = canonical_role_key(getattr(selected_user, "role", None))
    return role_key in {"GENERALSECRETARY", "SECRETARYGENERAL"}


def _is_secretary_general_account(user: User | None = None) -> bool:
    selected_user = user or current_user
    if not selected_user or not getattr(selected_user, "id", None):
        return False
    if _has_secretary_general_role(selected_user):
        return True
    try:
        return int(selected_user.id) in {
            int(user_id) for user_id in secretary_general_user_ids() if user_id
        }
    except Exception:
        return False


def _can_view_secretary_reports() -> bool:
    return _is_super_admin_account() or _is_secretary_general_account()


def _followup_super_admin_user_ids() -> list[int]:
    """Return super-admin recipients without loading User relationships."""
    try:
        rows = User.query.with_entities(User.id, User.role).all()
    except Exception:
        return []
    return sorted({
        int(user_id)
        for user_id, role in rows
        if user_id
        and (
            canonical_role_key(role).startswith("SUPER")
            or canonical_role_key(role) in {"ROOT", "SYSADMIN", "SYSTEMADMIN"}
        )
    })


def _require_followups_access() -> None:
    if not (
        _has_permission(FOLLOWUPS_READ)
        or _can_create()
        or _can_review()
        or _can_view_secretary_reports()
    ):
        abort(403)


def _report_storage_dir(report_id: int) -> Path:
    directory = Path(current_app.instance_path) / "uploads" / "employee_followups" / str(int(report_id))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _parse_date(value: str | None, *, fallback: date | None = None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _month_bounds(today: date | None = None) -> tuple[date, date]:
    current_day = today or date.today()
    return current_day.replace(day=1), current_day


def _display_user(user: User | None) -> str:
    if not user:
        return "-"
    return (getattr(user, "full_name", "") or getattr(user, "email", "") or f"#{user.id}").strip()


def _followup_manager_ids(report: EmployeeFollowupReport) -> list[int]:
    """Return the managers captured for a report, with legacy fallback."""
    raw = (getattr(report, "manager_user_ids", None) or "").strip()
    values: list[int] = []
    if raw:
        try:
            parsed = json.loads(raw)
            parsed = parsed if isinstance(parsed, list) else [parsed]
            values = [int(value) for value in parsed if str(value).strip().isdigit()]
        except (TypeError, ValueError, json.JSONDecodeError):
            values = [
                int(value.strip())
                for value in raw.split(",")
                if value.strip().isdigit()
            ]
    if not values and getattr(report, "manager_user_id", None):
        values = [int(report.manager_user_id)]
    return list(dict.fromkeys(values))


def _manager_snapshot_contains(column, user_id: int):
    """Match a manager id in both current JSON and legacy CSV snapshots."""
    value = str(int(user_id))
    return or_(
        column == value,
        column == f"[{value}]",
        column.like(f"[{value},%"),
        column.like(f"%,{value},%"),
        column.like(f"%,{value}]"),
        column.like(f"%, {value},%"),
        column.like(f"%, {value}]"),
        column.like(f"{value},%"),
        column.like(f"%,{value}"),
        column.like(f"%, {value}"),
    )


def _followup_summary_query():
    """Build a lightweight report list query with no report relationships."""
    employee = aliased(User)
    manager = aliased(User)
    return db.session.query(
        EmployeeFollowupReport.id.label("report_id"),
        EmployeeFollowupReport.period_start.label("period_start"),
        EmployeeFollowupReport.period_end.label("period_end"),
        EmployeeFollowupReport.status.label("status"),
        EmployeeFollowupReport.submitted_at.label("submitted_at"),
        EmployeeFollowupReport.reviewed_at.label("reviewed_at"),
        EmployeeFollowupReport.updated_at.label("updated_at"),
        func.coalesce(employee.name, employee.username, employee.email).label("employee_name"),
        employee.email.label("employee_email"),
        func.coalesce(manager.name, manager.username, manager.email).label("manager_name"),
        manager.email.label("manager_email"),
    ).select_from(EmployeeFollowupReport).join(
        employee,
        employee.id == EmployeeFollowupReport.employee_user_id,
    ).outerjoin(
        manager,
        manager.id == EmployeeFollowupReport.manager_user_id,
    )


def _followup_page_size() -> int:
    try:
        configured = int(current_app.config.get("FOLLOWUPS_PAGE_SIZE", 50))
    except (TypeError, ValueError):
        configured = 50
    return min(max(configured, 10), 100)


def _paginate_followup_summary(query, page_arg: str, *order_by):
    page = max(request.args.get(page_arg, type=int, default=1), 1)
    per_page = _followup_page_size()
    total = int(query.order_by(None).count() or 0)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = (
        query
        .order_by(*order_by)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return rows, page, pages, total


def _secretary_followup_reports_page():
    query = _followup_summary_query().filter(
        EmployeeFollowupReport.status == "REVIEWED"
    )
    return _paginate_followup_summary(
        query,
        "secretary_page",
        EmployeeFollowupReport.reviewed_at.desc(),
        EmployeeFollowupReport.id.desc(),
    )


def _super_admin_followup_reports_page():
    # Include drafts so a super admin can approve a report even when it has
    # never received a manager decision.
    return _paginate_followup_summary(
        _followup_summary_query(),
        "admin_page",
        EmployeeFollowupReport.updated_at.desc(),
        EmployeeFollowupReport.id.desc(),
    )


def _approved_followup_item_counts() -> tuple[int, int]:
    """Count approved-report items in SQL, without materializing report rows."""
    completed_count, total_items = db.session.query(
        func.coalesce(
            func.sum(case((EmployeeFollowupItem.status == "COMPLETED", 1), else_=0)),
            0,
        ),
        func.count(EmployeeFollowupItem.id),
    ).join(
        EmployeeFollowupReport,
        EmployeeFollowupReport.id == EmployeeFollowupItem.report_id,
    ).filter(
        EmployeeFollowupReport.status == "REVIEWED",
        EmployeeFollowupItem.is_included.is_(True),
    ).one()
    return int(completed_count or 0), int(total_items or 0)


def _access_level(report: EmployeeFollowupReport) -> str | None:
    user_id = int(current_user.id)
    if _is_super_admin_account():
        return "super_admin"
    if _is_secretary_general_account() and report.status == "REVIEWED":
        return "secretary_general"
    if int(report.employee_user_id) == user_id:
        return "employee"
    if (
        _can_review()
        and report.status != "DRAFT"
        and user_id in _followup_manager_ids(report)
    ):
        return "manager"
    return None


def _selected_direct_manager(
    manager_user_id: int | str | None,
    manager_options: list[User],
) -> User | None:
    try:
        selected_user_id = int(manager_user_id)
    except (TypeError, ValueError):
        return None
    return next(
        (manager for manager in manager_options if int(manager.id) == selected_user_id),
        None,
    )


def _get_report_or_abort(report_id: int) -> tuple[EmployeeFollowupReport, str]:
    report = EmployeeFollowupReport.query.get_or_404(report_id)
    level = _access_level(report)
    if not level:
        abort(403)
    return report, level


def _employee_can_edit(report: EmployeeFollowupReport) -> bool:
    return (
        int(report.employee_user_id) == int(current_user.id)
        and report.status in {"DRAFT", "NEEDS_REVISION"}
        and _can_create()
    )


def _can_delete_followup_reports(access_level: str | None = None) -> bool:
    # The Secretary General has read-only visibility. Existing FOLLOWUPS_READ
    # users retain the legacy delete capability, while the super admin is
    # explicitly allowed to delete every report.
    if access_level == "secretary_general":
        return False
    return _has_permission(FOLLOWUPS_READ) or _is_super_admin_account()


def _remove_report_storage(report_id: int) -> None:
    storage_root = (
        Path(current_app.instance_path) / "uploads" / "employee_followups"
    ).resolve()
    storage_directory = (storage_root / str(int(report_id))).resolve()
    if storage_directory.parent != storage_root or not storage_directory.is_dir():
        return
    try:
        shutil.rmtree(storage_directory)
    except OSError:
        current_app.logger.warning("Failed to remove followup storage for report %s", report_id)


def _extract_completed_meeting_tasks(report: EmployeeFollowupReport) -> int:
    start_at = datetime.combine(report.period_start, time.min)
    end_at = datetime.combine(report.period_end + timedelta(days=1), time.min)
    tasks = (
        PortalMeetingTask.query
        .filter(PortalMeetingTask.assignee_user_id == report.employee_user_id)
        .filter(PortalMeetingTask.status == "DONE")
        .filter(or_(
            and_(
                PortalMeetingTask.completed_at.isnot(None),
                PortalMeetingTask.completed_at >= start_at,
                PortalMeetingTask.completed_at < end_at,
            ),
            and_(
                PortalMeetingTask.completed_at.is_(None),
                PortalMeetingTask.due_date.isnot(None),
                PortalMeetingTask.due_date >= report.period_start,
                PortalMeetingTask.due_date <= report.period_end,
            ),
        ))
        .order_by(PortalMeetingTask.completed_at.desc(), PortalMeetingTask.id.desc())
        .all()
    )
    added = 0
    for task in tasks:
        existing = EmployeeFollowupItem.query.filter_by(
            report_id=report.id,
            source_type="MEETING_TASK",
            source_id=task.id,
        ).first()
        if existing:
            continue
        completed_on = task.completed_at.date() if task.completed_at else task.due_date
        db.session.add(EmployeeFollowupItem(
            report_id=report.id,
            source_type="MEETING_TASK",
            source_id=task.id,
            title=task.title or "مهمة منجزة",
            description=(task.description or "")[:5000] or None,
            completed_on=completed_on,
            status="COMPLETED",
            is_included=True,
        ))
        added += 1
    return added


def _extract_workflow_accomplishments(report: EmployeeFollowupReport) -> int:
    start_at = datetime.combine(report.period_start, time.min)
    end_at = datetime.combine(report.period_end + timedelta(days=1), time.min)
    audit_logs = (
        AuditLog.query
        .filter(AuditLog.user_id == report.employee_user_id)
        .filter(AuditLog.request_id.isnot(None))
        .filter(AuditLog.action.in_(WORKFLOW_ACCOMPLISHMENT_ACTIONS))
        .filter(AuditLog.created_at >= start_at, AuditLog.created_at < end_at)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .all()
    )

    synchronized = 0
    for audit_log in audit_logs:
        existing = EmployeeFollowupItem.query.filter_by(
            report_id=report.id,
            source_type="WORKFLOW_AUDIT",
            source_id=audit_log.id,
        ).first()
        request_title = (getattr(audit_log.request, "title", None) or "").strip()
        request_label = request_title or f"معاملة #{audit_log.request_id}"
        action_label = WORKFLOW_ACCOMPLISHMENT_ACTIONS[audit_log.action]
        details = []
        request_description = (
            getattr(audit_log.request, "description", None) or ""
        ).strip()
        if request_description:
            details.append(f"تفاصيل المعاملة: {request_description}")
        action_note = (audit_log.note or "").strip()
        if action_note:
            details.append(f"ملاحظة الإجراء: {action_note}")
        title = "\n".join([f"{action_label}: {request_label}", *details])
        if existing:
            if existing.title != title or existing.description is not None:
                existing.title = title
                existing.description = None
                synchronized += 1
            continue
        db.session.add(EmployeeFollowupItem(
            report_id=report.id,
            source_type="WORKFLOW_AUDIT",
            source_id=audit_log.id,
            title=title,
            description=None,
            completed_on=audit_log.created_at.date(),
            status="COMPLETED",
            is_included=True,
        ))
        synchronized += 1
    return synchronized


def _report_docx_filename(report: EmployeeFollowupReport) -> str:
    return (
        f"تقرير_انجاز_من_{report.period_start.isoformat()}"
        f"_الى_{report.period_end.isoformat()}.docx"
    )


def _save_attachment(report: EmployeeFollowupReport, upload, kind: str) -> EmployeeFollowupAttachment | None:
    original_name = clean_original_filename(getattr(upload, "filename", None))
    if not original_name:
        return None
    if kind in {"LETTERHEAD", "REPORT_DOCX"} and not original_name.lower().endswith(".docx"):
        raise ValueError("docx_required")

    stored_name = random_storage_name(uuid.uuid4().hex, original_name)
    saved_path = _report_storage_dir(report.id) / stored_name
    upload.save(str(saved_path))
    try:
        if kind in {"LETTERHEAD", "REPORT_DOCX"} and not is_valid_docx(saved_path):
            raise ValueError("invalid_docx")
        mime_type = (getattr(upload, "mimetype", None) or "").strip()
        if not mime_type:
            mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        attachment = EmployeeFollowupAttachment(
            report_id=report.id,
            kind=kind,
            original_name=original_name[:255],
            stored_name=stored_name,
            mime_type=mime_type[:120],
            file_size=saved_path.stat().st_size,
            uploaded_by_user_id=current_user.id,
            uploaded_at=datetime.utcnow(),
        )
        db.session.add(attachment)
        return attachment
    except Exception:
        try:
            saved_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _notify(user_ids, message: str, level: str, report: EmployeeFollowupReport) -> None:
    link_url = notification_target_path("EMPLOYEE_FOLLOWUP_REPORT", report.id)
    now = datetime.utcnow()
    try:
        actor_id = int(getattr(current_user, "id", 0) or 0)
    except Exception:
        actor_id = 0
    recipient_ids = set()
    for value in user_ids or []:
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            continue
        if user_id > 0 and user_id != actor_id:
            recipient_ids.add(user_id)
    for user_id in recipient_ids:
        db.session.add(Notification(
            user_id=user_id,
            message=message[:255],
            type=level,
            source="portal",
            link_url=link_url,
            is_read=False,
            is_mirror=False,
            created_at=now,
        ))


def _notify_approved_report_observers(
    report: EmployeeFollowupReport,
    approver_label: str,
) -> None:
    """Notify Secretary-General and super-admin accounts after approval."""
    recipient_ids = set()
    try:
        recipient_ids.update(secretary_general_user_ids())
    except Exception:
        current_app.logger.exception(
            "Failed to resolve Secretary-General recipients for followup %s",
            report.id,
        )
    recipient_ids.update(_followup_super_admin_user_ids())
    _notify(
        recipient_ids,
        (
            f"تم اعتماد تقرير إنجاز الموظف {_display_user(report.employee)} "
            f"بواسطة {approver_label}، وأصبح متاحاً للاطلاع."
        ),
        "FOLLOWUP_REVIEWED",
        report,
    )


def _apply_employee_changes(report: EmployeeFollowupReport) -> None:
    report.employee_summary = (request.form.get("employee_summary") or "").strip() or None
    report.challenges = (request.form.get("challenges") or "").strip() or None
    report.manager_request = (request.form.get("manager_request") or "").strip() or None

    for item in report.items or []:
        item.title = (request.form.get(f"title_{item.id}") or "").strip() or item.title
        suggestion_field = f"ai_suggestion_{item.id}"
        if suggestion_field in request.form:
            item.ai_suggestion = (request.form.get(suggestion_field) or "").strip() or None
        description_field = f"description_{item.id}"
        if description_field in request.form:
            item.description = (request.form.get(description_field) or "").strip() or None
        item_date = _parse_date(request.form.get(f"completed_on_{item.id}"), fallback=item.completed_on)
        if request.form.get(f"completed_on_{item.id}") and not item_date:
            raise ValueError("invalid_item_date")
        item.completed_on = item_date
        requested_status = (request.form.get(f"status_{item.id}") or item.status).strip().upper()
        if requested_status in ITEM_STATUS_LABELS:
            item.status = requested_status
        item.is_included = request.form.get(f"included_{item.id}") == "1"


def _run_followup_assistant(report: EmployeeFollowupReport) -> None:
    """Prepare review-first rewrites without changing the original titles."""
    analysis = build_followup_analysis(report.items, user=current_user)
    report.ai_summary = str(analysis["summary"])
    report.ai_notes = str(analysis["notes"])
    suggestions = analysis["suggestions"]
    duplicate_ids = analysis["duplicate_ids"]
    for item in report.items or []:
        item.ai_suggestion = suggestions.get(item.id)
        item.duplicate_hint = "قد يكون هذا البند مكرراً." if item.id in duplicate_ids else None


def _apply_assistant_suggestions(report: EmployeeFollowupReport) -> int:
    applied = 0
    for item in report.items or []:
        suggestion = (item.ai_suggestion or "").strip()
        if not suggestion:
            continue
        item.title = suggestion
        item.ai_suggestion = None
        item.duplicate_hint = None
        applied += 1
    return applied


def _followup_attachment_for_kind(report: EmployeeFollowupReport, kind: str) -> EmployeeFollowupAttachment | None:
    return next((attachment for attachment in (report.attachments or []) if attachment.kind == kind), None)


def send_followup_reminders(today: date | None = None) -> int:
    """Send one pre-deadline employee reminder and one manager review reminder."""
    current_day = today or date.today()
    now = datetime.utcnow()
    sent = 0
    draft_reports = EmployeeFollowupReport.query.filter(
        EmployeeFollowupReport.status.in_(("DRAFT", "NEEDS_REVISION")),
        EmployeeFollowupReport.period_end >= current_day,
        EmployeeFollowupReport.period_end <= current_day + timedelta(days=3),
    ).all()
    for report in draft_reports:
        if report.last_employee_reminder_at and report.last_employee_reminder_at.date() == current_day:
            continue
        _notify([report.employee_user_id], "تذكير: تقرير الإنجاز يقترب موعده، يرجى مراجعته وإرساله للمدير.", "REMINDER", report)
        report.last_employee_reminder_at = now
        sent += 1

    pending_reports = EmployeeFollowupReport.query.filter(
        EmployeeFollowupReport.status == "SUBMITTED",
        EmployeeFollowupReport.submitted_at.isnot(None),
        EmployeeFollowupReport.submitted_at <= now - timedelta(days=3),
    ).all()
    for report in pending_reports:
        manager_ids = _followup_manager_ids(report)
        if not manager_ids:
            continue
        if report.last_manager_reminder_at and report.last_manager_reminder_at.date() == current_day:
            continue
        _notify(manager_ids, "تذكير: يوجد تقرير إنجاز بانتظار مراجعتك.", "REMINDER", report)
        report.last_manager_reminder_at = now
        sent += 1

    if sent:
        db.session.commit()
    return sent


@portal_bp.route("/followups")
@login_required
def followups_dashboard():
    _require_followups_access()
    is_super_admin = _is_super_admin_account()
    is_secretary_general = _is_secretary_general_account()
    can_view_secretary_reports = is_super_admin or is_secretary_general

    # Secretary-General visibility is read-only. Keep the existing manager
    # queue separate so the two roles do not accidentally expose each other's
    # actions in the dashboard.
    manager_can_review = _can_review() and not is_super_admin and not is_secretary_general
    current_user_id = int(current_user.id)
    own_reports = (
        EmployeeFollowupReport.query
        .filter(EmployeeFollowupReport.employee_user_id == current_user.id)
        .order_by(EmployeeFollowupReport.period_end.desc(), EmployeeFollowupReport.id.desc())
        .all()
    )
    review_reports = []
    if manager_can_review:
        review_candidates = (
            EmployeeFollowupReport.query
            .filter(
                or_(
                    EmployeeFollowupReport.manager_user_id == current_user_id,
                    _manager_snapshot_contains(
                        EmployeeFollowupReport.manager_user_ids,
                        current_user_id,
                    ),
                )
            )
            .filter(EmployeeFollowupReport.status != "DRAFT")
            .order_by(
                EmployeeFollowupReport.submitted_at.desc(),
                EmployeeFollowupReport.id.desc(),
            )
            .all()
        )
        review_reports = [
            report for report in review_candidates
            if current_user_id in _followup_manager_ids(report)
        ]

    current_start, current_end = _month_bounds()
    secretary_reports = []
    secretary_page = secretary_pages = secretary_total = 0
    admin_reports = []
    admin_page = admin_pages = admin_total = 0

    if can_view_secretary_reports:
        # This is intentionally a lightweight, paginated tuple query. The
        # report's item/attachment relationships contain large text and are
        # only loaded on the detail page.
        (
            secretary_reports,
            secretary_page,
            secretary_pages,
            secretary_total,
        ) = _secretary_followup_reports_page()
        if is_super_admin:
            (
                admin_reports,
                admin_page,
                admin_pages,
                admin_total,
            ) = _super_admin_followup_reports_page()
        completed_count, total_items = _approved_followup_item_counts()
        incomplete_count = max(total_items - completed_count, 0)
        overdue_count = incomplete_count
        metric_scope_label = "الموظفين"
    else:
        metric_reports = review_reports if manager_can_review else own_reports
        metric_items = [
            item
            for report in metric_reports
            for item in (report.items or [])
            if item.is_included
        ]
        completed_count = sum(item.status == "COMPLETED" for item in metric_items)
        incomplete_count = sum(item.status != "COMPLETED" for item in metric_items)
        total_items = completed_count + incomplete_count
        if manager_can_review:
            overdue_count = incomplete_count
            metric_scope_label = "فريقك"
        else:
            overdue_count = PortalMeetingTask.query.filter(
                PortalMeetingTask.assignee_user_id == current_user.id,
                PortalMeetingTask.status.in_(("OPEN", "IN_PROGRESS")),
                PortalMeetingTask.due_date.isnot(None),
                PortalMeetingTask.due_date < date.today(),
            ).count()
            metric_scope_label = "مهامك"

    completion_rate = round((completed_count / total_items) * 100) if total_items else 0

    missing_reports = 0
    if manager_can_review:
        direct_employee_ids = {
            int(user_id) for (user_id,) in (
                EmployeeFile.query
                .filter(EmployeeFile.direct_manager_user_id == current_user.id)
                .with_entities(EmployeeFile.user_id)
                .all()
            ) if user_id
        }
        direct_employee_ids.update(
            int(user_id) for (user_id,) in (
                EmployeeResponsibleAssignment.query
                .filter_by(responsible_user_id=current_user.id, is_active=True)
                .with_entities(EmployeeResponsibleAssignment.employee_user_id)
                .all()
            ) if user_id
        )
        submitted_candidates = (
            EmployeeFollowupReport.query
            .filter(
                or_(
                    EmployeeFollowupReport.manager_user_id == current_user_id,
                    _manager_snapshot_contains(
                        EmployeeFollowupReport.manager_user_ids,
                        current_user_id,
                    ),
                )
            )
            .filter(EmployeeFollowupReport.period_start <= current_end)
            .filter(EmployeeFollowupReport.period_end >= current_start)
            .filter(EmployeeFollowupReport.status.in_((
                "SUBMITTED", "NEEDS_REVISION", "REVIEWED"
            )))
            .with_entities(
                EmployeeFollowupReport.employee_user_id,
                EmployeeFollowupReport.manager_user_id,
                EmployeeFollowupReport.manager_user_ids,
            )
            .all()
        )
        submitted_ids = {
            int(report.employee_user_id)
            for report in submitted_candidates
            if report.employee_user_id
            and current_user_id in _followup_manager_ids(report)
        }
        missing_reports = len(direct_employee_ids - submitted_ids)

    return render_template(
        "portal/followups/dashboard.html",
        own_reports=own_reports,
        review_reports=review_reports,
        report_status_labels=REPORT_STATUS_LABELS,
        completed_count=completed_count,
        incomplete_count=incomplete_count,
        completion_rate=completion_rate,
        overdue_count=overdue_count,
        missing_reports=missing_reports,
        metric_scope_label=metric_scope_label,
        can_create=_can_create(),
        can_review=manager_can_review,
        can_admin_review=is_super_admin,
        admin_reports=admin_reports,
        admin_page=admin_page,
        admin_pages=admin_pages,
        admin_total=admin_total,
        can_secretary_view=can_view_secretary_reports,
        secretary_reports=secretary_reports,
        secretary_page=secretary_page,
        secretary_pages=secretary_pages,
        secretary_total=secretary_total,
        can_delete_followup_reports=_can_delete_followup_reports(
            "secretary_general"
            if is_secretary_general and not is_super_admin
            else None
        ),
    )


@portal_bp.route("/followups/new", methods=["GET", "POST"])
@login_required
def followups_new():
    _require_followups_access()
    if not _can_create():
        abort(403)
    default_start, default_end = _month_bounds()
    form = {
        "period_start": request.form.get("period_start") or default_start.isoformat(),
        "period_end": request.form.get("period_end") or default_end.isoformat(),
    }
    manager_options = resolve_responsible_managers(current_user.id)
    has_explicit_responsibles = EmployeeResponsibleAssignment.query.filter_by(
        employee_user_id=int(current_user.id),
        is_active=True,
    ).first() is not None
    manager_ids = [
        int(manager.id)
        for manager in manager_options
        if manager and int(manager.id) != int(current_user.id)
    ]
    form["manager_user_id"] = (
        request.form.get("manager_user_id")
        or (str(manager_ids[0]) if manager_ids else "")
    )
    manager = _selected_direct_manager(form["manager_user_id"], manager_options)
    if request.method == "POST":
        period_start = _parse_date(form["period_start"])
        period_end = _parse_date(form["period_end"])
        if not period_start or not period_end or period_end < period_start:
            flash("يرجى تحديد فترة تقرير صحيحة.", "warning")
        elif manager_options and not manager:
            flash("يرجى اختيار المدير المباشر لتوجيه التقرير عند إرساله للاعتماد.", "warning")
        else:
            report_manager_ids = (
                manager_ids
                if has_explicit_responsibles
                else ([int(manager.id)] if manager else [])
            )
            report = EmployeeFollowupReport(
                employee_user_id=current_user.id,
                manager_user_id=(manager.id if manager else (manager_ids[0] if manager_ids else None)),
                manager_user_ids=(
                    json.dumps(report_manager_ids, separators=(",", ":"))
                    if report_manager_ids else None
                ),
                period_start=period_start,
                period_end=period_end,
                status="DRAFT",
            )
            db.session.add(report)
            db.session.flush()
            extracted_count = (
                _extract_completed_meeting_tasks(report)
                + _extract_workflow_accomplishments(report)
            )
            db.session.commit()
            flash(f"تم إنشاء التقرير وإضافة {extracted_count} بند تلقائي من أعمال النظام.", "success")
            return redirect(url_for("portal.followups_view", report_id=report.id))

    return render_template(
        "portal/followups/new.html",
        form=form,
        manager=manager,
        manager_options=manager_options,
    )


@portal_bp.route("/followups/<int:report_id>")
@login_required
def followups_view(report_id: int):
    _require_followups_access()
    report, access_level = _get_report_or_abort(report_id)
    # Keep the super-admin decision form visible even if the super admin is
    # also the employee who owns this particular report.
    can_edit = _employee_can_edit(report) and access_level != "super_admin"
    can_review = _can_review() and (
        access_level == "super_admin"
        or (access_level == "manager" and report.status == "SUBMITTED")
    )
    return render_template(
        "portal/followups/view.html",
        report=report,
        access_level=access_level,
        can_edit=can_edit,
        can_review=can_review,
        has_ai_suggestions=any(
            (item.ai_suggestion or "").strip()
            for item in (report.items or [])
        ),
        can_delete_followup_reports=_can_delete_followup_reports(access_level),
        is_super_admin_review=access_level == "super_admin",
        report_status_labels=REPORT_STATUS_LABELS,
        item_status_labels=ITEM_STATUS_LABELS,
        rating_labels=RATING_LABELS,
    )


@portal_bp.route("/followups/<int:report_id>/delete", methods=["POST"])
@login_required
def followups_delete_report(report_id: int):
    _require_followups_access()
    if _is_secretary_general_account() and not _is_super_admin_account():
        abort(403)
    if not _can_delete_followup_reports():
        abort(403)
    report = EmployeeFollowupReport.query.get_or_404(report_id)

    report_id_value = int(report.id)
    try:
        Notification.query.filter_by(
            link_url=notification_target_path("EMPLOYEE_FOLLOWUP_REPORT", report_id_value)
        ).delete(synchronize_session=False)
        db.session.delete(report)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to delete followup report %s", report_id_value)
        flash("تعذر حذف تقرير الإنجاز حالياً.", "danger")
    else:
        _remove_report_storage(report_id_value)
        flash("تم حذف تقرير الإنجاز.", "success")

    return redirect(url_for("portal.followups_dashboard"))


@portal_bp.route("/followups/<int:report_id>/update", methods=["POST"])
@login_required
def followups_update(report_id: int):
    _require_followups_access()
    report, _ = _get_report_or_abort(report_id)
    if not _employee_can_edit(report):
        abort(403)
    action = (request.form.get("action") or "save").strip().lower()
    try:
        _apply_employee_changes(report)
        if action == "ai":
            _run_followup_assistant(report)
            db.session.commit()
            flash("تمت إعادة صياغة الإنجازات باختصار ووضوح. راجعها وعدّلها إن رغبت، ثم اضغط اعتماد الصياغات على الإنجازات.", "success")
        elif action == "apply_ai":
            applied_count = _apply_assistant_suggestions(report)
            db.session.commit()
            if applied_count:
                flash(f"تم تفريغ {applied_count} صياغة معتمدة على المهام وحفظها.", "success")
            else:
                flash("لا توجد صياغات مقترحة لتفريغها.", "info")
        elif action == "submit":
            manager_options = resolve_responsible_managers(current_user.id)
            has_explicit_responsibles = EmployeeResponsibleAssignment.query.filter_by(
                employee_user_id=int(current_user.id),
                is_active=True,
            ).first() is not None
            manager = _selected_direct_manager(
                report.manager_user_id,
                manager_options,
            )
            if not manager_options:
                raise ValueError("manager_not_found")
            if not manager:
                manager = manager_options[0]
            manager_ids = [
                int(candidate.id)
                for candidate in manager_options
                if candidate and int(candidate.id) != int(current_user.id)
            ]
            report_manager_ids = (
                manager_ids
                if has_explicit_responsibles
                else [int(manager.id)]
            )
            report.manager_user_id = manager.id
            report.manager_user_ids = (
                json.dumps(report_manager_ids, separators=(",", ":"))
                if report_manager_ids else None
            )
            report.status = "SUBMITTED"
            report.submitted_at = datetime.utcnow()
            report.reviewed_at = None
            recipients = set(report_manager_ids or [manager.id])
            _notify(recipients, f"تم إرسال تقرير إنجاز من {_display_user(report.employee)} للمراجعة.", "FOLLOWUP_SUBMITTED", report)
            db.session.commit()
            flash("تم إرسال التقرير إلى المدير المباشر.", "success")
        else:
            db.session.commit()
            flash("تم حفظ المسودة.", "success")
    except ValueError as exc:
        db.session.rollback()
        messages = {
            "invalid_item_date": "يرجى إدخال تاريخ صحيح لبند الإنجاز.",
            "manager_not_found": "لا يوجد مدير مباشر محدد في الهيكل التنظيمي؛ حدّث بيانات المدير أولاً.",
        }
        flash(messages.get(str(exc), "تعذر حفظ التقرير."), "warning")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to update followup report %s", report_id)
        flash("تعذر حفظ التقرير حالياً.", "danger")
    return redirect(url_for("portal.followups_view", report_id=report_id))


@portal_bp.route("/followups/<int:report_id>/import-workflow", methods=["POST"])
@login_required
def followups_import_workflow_accomplishments(report_id: int):
    _require_followups_access()
    report, _ = _get_report_or_abort(report_id)
    if not _employee_can_edit(report):
        abort(403)
    try:
        imported_count = _extract_workflow_accomplishments(report)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to import Masar accomplishments for report %s", report_id)
        flash("تعذر استيراد أعمال مسار حالياً.", "danger")
    else:
        if imported_count:
            flash(f"تمت إضافة {imported_count} بند من أعمال مسار.", "success")
        else:
            flash("لا توجد أعمال جديدة من مسار ضمن فترة التقرير.", "info")
    return redirect(url_for("portal.followups_view", report_id=report.id))


@portal_bp.route("/followups/<int:report_id>/items", methods=["POST"])
@login_required
def followups_add_item(report_id: int):
    _require_followups_access()
    report, _ = _get_report_or_abort(report_id)
    if not _employee_can_edit(report):
        abort(403)
    title = (request.form.get("title") or "").strip()
    item_date = _parse_date(request.form.get("completed_on"))
    status = (request.form.get("status") or "COMPLETED").strip().upper()
    if not title:
        flash("عنوان الإنجاز مطلوب.", "warning")
    elif request.form.get("completed_on") and not item_date:
        flash("يرجى إدخال تاريخ صحيح.", "warning")
    else:
        db.session.add(EmployeeFollowupItem(
            report_id=report.id,
            source_type="MANUAL",
            title=title,
            description=None,
            completed_on=item_date,
            status=status if status in ITEM_STATUS_LABELS else "COMPLETED",
            is_included=True,
        ))
        db.session.commit()
        flash("تمت إضافة البند.", "success")
    return redirect(url_for("portal.followups_view", report_id=report.id))


@portal_bp.route("/followups/<int:report_id>/items/<int:item_id>/delete", methods=["POST"])
@login_required
def followups_delete_item(report_id: int, item_id: int):
    _require_followups_access()
    report, _ = _get_report_or_abort(report_id)
    if not _employee_can_edit(report):
        abort(403)
    item = EmployeeFollowupItem.query.filter_by(id=item_id, report_id=report.id).first_or_404()
    db.session.delete(item)
    db.session.commit()
    flash("تم حذف البند.", "success")
    return redirect(url_for("portal.followups_view", report_id=report.id))


@portal_bp.route("/followups/<int:report_id>/attachments", methods=["POST"])
@login_required
def followups_upload_attachments(report_id: int):
    _require_followups_access()
    report, _ = _get_report_or_abort(report_id)
    if not _employee_can_edit(report):
        abort(403)
    saved_paths: list[Path] = []
    try:
        uploads = (
            (request.files.get("letterhead_docx"), "LETTERHEAD"),
            (request.files.get("report_docx"), "REPORT_DOCX"),
        )
        added = 0
        for upload, kind in uploads:
            if upload and getattr(upload, "filename", ""):
                attachment = _save_attachment(report, upload, kind)
                if attachment:
                    saved_paths.append(_report_storage_dir(report.id) / attachment.stored_name)
                    added += 1
        for upload in request.files.getlist("supporting_files"):
            if upload and getattr(upload, "filename", ""):
                attachment = _save_attachment(report, upload, "SUPPORTING")
                if attachment:
                    saved_paths.append(_report_storage_dir(report.id) / attachment.stored_name)
                    added += 1
        if not added:
            flash("اختر ملفاً واحداً على الأقل للرفع.", "warning")
        else:
            db.session.commit()
            flash(f"تم رفع {added} مرفق.", "success")
    except ValueError:
        db.session.rollback()
        for saved_path in saved_paths:
            try:
                saved_path.unlink(missing_ok=True)
            except OSError:
                pass
        flash("ملف الترويسة أو التقرير يجب أن يكون Word بصيغة DOCX صالحة.", "warning")
    except Exception:
        db.session.rollback()
        for saved_path in saved_paths:
            try:
                saved_path.unlink(missing_ok=True)
            except OSError:
                pass
        current_app.logger.exception("Failed to upload followup attachments for %s", report_id)
        flash("تعذر رفع المرفق.", "danger")
    return redirect(url_for("portal.followups_view", report_id=report.id))


@portal_bp.route("/followups/<int:report_id>/attachments/<int:attachment_id>/download")
@login_required
def followups_download_attachment(report_id: int, attachment_id: int):
    _require_followups_access()
    report, _ = _get_report_or_abort(report_id)
    attachment = EmployeeFollowupAttachment.query.filter_by(id=attachment_id, report_id=report.id).first_or_404()
    return send_from_directory(
        str(_report_storage_dir(report.id)),
        attachment.stored_name,
        mimetype=attachment.mime_type or None,
        as_attachment=True,
        download_name=attachment.original_name,
    )


@portal_bp.route("/followups/<int:report_id>/export.docx")
@login_required
def followups_export_docx(report_id: int):
    _require_followups_access()
    report, _ = _get_report_or_abort(report_id)
    letterhead = _followup_attachment_for_kind(report, "LETTERHEAD")
    template_path = _report_storage_dir(report.id) / letterhead.stored_name if letterhead else None
    try:
        document_bytes = build_followup_docx(report, template_path=template_path)
    except Exception:
        current_app.logger.exception("Failed to create followup docx for %s", report.id)
        flash("تعذر إنشاء ملف Word للتقرير.", "danger")
        return redirect(url_for("portal.followups_view", report_id=report.id))
    filename = _report_docx_filename(report)
    from io import BytesIO
    from flask import send_file
    return send_file(
        BytesIO(document_bytes),
        mimetype=DOCX_MIME,
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )


@portal_bp.route("/followups/<int:report_id>/review", methods=["POST"])
@login_required
def followups_review(report_id: int):
    _require_followups_access()
    report, level = _get_report_or_abort(report_id)
    is_super_admin_review = level == "super_admin" and _is_super_admin_account()
    if not _can_review() or (
        not is_super_admin_review
        and (level != "manager" or report.status != "SUBMITTED")
    ):
        abort(403)
    action = (request.form.get("action") or "review").strip().lower()
    if "manager_comment" in request.form:
        report.manager_comment = (request.form.get("manager_comment") or "").strip() or None
    if "manager_rating" in request.form:
        rating = (request.form.get("manager_rating") or "").strip().upper()
        report.manager_rating = rating if rating in RATING_LABELS else None
    for item in report.items or []:
        comment_field = f"manager_comment_{item.id}"
        if comment_field in request.form:
            item.manager_comment = (request.form.get(comment_field) or "").strip() or None
        rating_field = f"manager_rating_{item.id}"
        if rating_field in request.form:
            item_rating = (request.form.get(rating_field) or "").strip().upper()
            item.manager_rating = item_rating if item_rating in RATING_LABELS else None

    old_status = report.status
    now = datetime.utcnow()
    report.reviewed_at = now
    if action == "return":
        report.status = "NEEDS_REVISION"
        actor_label = "السوبر أدمن" if is_super_admin_review else "المدير"
        message = f"تمت إعادة تقرير الإنجاز للتعديل مع ملاحظات {actor_label}."
        flash_message = "تمت إعادة التقرير للموظف للتعديل."
        notification_type = "FOLLOWUP_REVISION"
    else:
        report.status = "REVIEWED"
        actor_label = "السوبر أدمن" if is_super_admin_review else "المدير"
        message = f"تمت مراجعة تقرير الإنجاز واعتماده إلكترونياً من {actor_label}."
        flash_message = "تمت مراجعة التقرير واعتماده إلكترونياً."
        notification_type = "FOLLOWUP_REVIEWED"
    _notify([report.employee_user_id], message, notification_type, report)
    if action != "return":
        _notify_approved_report_observers(report, actor_label)
    db.session.add(AuditLog(
        action=(
            "FOLLOWUP_SUPER_ADMIN_APPROVE"
            if is_super_admin_review and action != "return"
            else "FOLLOWUP_SUPER_ADMIN_RETURN"
            if is_super_admin_review
            else "FOLLOWUP_MANAGER_APPROVE"
            if action != "return"
            else "FOLLOWUP_MANAGER_RETURN"
        ),
        user_id=current_user.id,
        note=(
            f"Follow-up report decision by {actor_label}; "
            f"manager approval was {'present' if old_status == 'REVIEWED' else 'not required'}"
        ),
        old_status=old_status,
        new_status=report.status,
        target_type="EMPLOYEE_FOLLOWUP_REPORT",
        target_id=report.id,
        created_at=now,
    ))
    db.session.commit()
    flash(flash_message, "success")
    return redirect(url_for("portal.followups_view", report_id=report.id))
