"""Employee accomplishment reports and direct-manager review workflow."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import mimetypes
from pathlib import Path
import shutil
import uuid

from flask import abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, func, or_

from extensions import db
from models import (
    AuditLog,
    EmployeeFile,
    EmployeeFollowupAttachment,
    EmployeeFollowupCopyRecipient,
    EmployeeFollowupItem,
    EmployeeFollowupReport,
    Notification,
    PortalMeetingTask,
    User,
)
from services.followup_assistant import build_followup_analysis
from services.followup_docx import DOCX_MIME, build_followup_docx, is_valid_docx
from services.hr_request_workflow import resolve_direct_manager
from utils.file_uploads import clean_original_filename, random_storage_name
from utils.notification_links import notification_target_path

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


def _require_followups_access() -> None:
    if not (_has_permission(FOLLOWUPS_READ) or _can_create() or _can_review()):
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


def _access_level(report: EmployeeFollowupReport) -> str | None:
    user_id = int(current_user.id)
    if _can_manage_all():
        return "manager"
    if int(report.employee_user_id) == user_id:
        return "employee"
    if (
        _can_review()
        and report.status != "DRAFT"
        and report.manager_user_id
        and int(report.manager_user_id) == user_id
    ):
        return "manager"
    if report.status != "DRAFT" and any(
        int(recipient.user_id) == user_id for recipient in (report.copy_recipients or [])
    ):
        return "copy"
    return None


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


def _can_delete_own_report(report: EmployeeFollowupReport, access_level: str) -> bool:
    return (
        access_level == "employee"
        and int(report.employee_user_id) == int(current_user.id)
    )


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
            title=(task.title or "مهمة منجزة")[:255],
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

    added = 0
    for audit_log in audit_logs:
        existing = EmployeeFollowupItem.query.filter_by(
            report_id=report.id,
            source_type="WORKFLOW_AUDIT",
            source_id=audit_log.id,
        ).first()
        if existing:
            continue

        request_title = (getattr(audit_log.request, "title", None) or "").strip()
        request_label = request_title or f"معاملة #{audit_log.request_id}"
        action_label = WORKFLOW_ACCOMPLISHMENT_ACTIONS[audit_log.action]
        db.session.add(EmployeeFollowupItem(
            report_id=report.id,
            source_type="WORKFLOW_AUDIT",
            source_id=audit_log.id,
            title=f"{action_label}: {request_label}"[:255],
            description=f"سجل مسار للمعاملة #{audit_log.request_id}.",
            completed_on=audit_log.created_at.date(),
            status="COMPLETED",
            is_included=True,
        ))
        added += 1
    return added


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


def _copy_recipient_ids(report: EmployeeFollowupReport) -> set[int]:
    return {int(row.user_id) for row in (report.copy_recipients or []) if row.user_id}


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


def _update_copy_recipients(report: EmployeeFollowupReport, raw_ids) -> None:
    requested_ids: set[int] = set()
    for raw_id in raw_ids or []:
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if user_id > 0 and user_id not in {report.employee_user_id, report.manager_user_id}:
            requested_ids.add(user_id)

    valid_ids = {
        int(user_id)
        for (user_id,) in User.query.filter(User.id.in_(requested_ids)).with_entities(User.id).all()
    } if requested_ids else set()
    current = {int(row.user_id): row for row in (report.copy_recipients or [])}
    for user_id, row in current.items():
        if user_id not in valid_ids:
            db.session.delete(row)
    for user_id in valid_ids - current.keys():
        db.session.add(EmployeeFollowupCopyRecipient(report_id=report.id, user_id=user_id))


def _apply_employee_changes(report: EmployeeFollowupReport) -> None:
    report.employee_summary = (request.form.get("employee_summary") or "").strip() or None
    report.challenges = (request.form.get("challenges") or "").strip() or None
    report.manager_request = (request.form.get("manager_request") or "").strip() or None
    _update_copy_recipients(report, request.form.getlist("copy_user_ids"))

    for item in report.items or []:
        item.title = (request.form.get(f"title_{item.id}") or "").strip()[:255] or item.title
        item.description = (request.form.get(f"description_{item.id}") or "").strip() or None
        item_date = _parse_date(request.form.get(f"completed_on_{item.id}"), fallback=item.completed_on)
        if request.form.get(f"completed_on_{item.id}") and not item_date:
            raise ValueError("invalid_item_date")
        item.completed_on = item_date
        requested_status = (request.form.get(f"status_{item.id}") or item.status).strip().upper()
        if requested_status in ITEM_STATUS_LABELS:
            item.status = requested_status
        item.is_included = request.form.get(f"included_{item.id}") == "1"


def _run_local_assistant(report: EmployeeFollowupReport) -> None:
    analysis = build_followup_analysis(report.items)
    report.ai_summary = str(analysis["summary"])
    report.ai_notes = str(analysis["notes"])
    suggestions = analysis["suggestions"]
    duplicate_ids = analysis["duplicate_ids"]
    for item in report.items or []:
        item.ai_suggestion = suggestions.get(item.id)
        item.duplicate_hint = "قد يكون هذا البند مكرراً." if item.id in duplicate_ids else None


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
        if not report.manager_user_id:
            continue
        if report.last_manager_reminder_at and report.last_manager_reminder_at.date() == current_day:
            continue
        _notify([report.manager_user_id], "تذكير: يوجد تقرير إنجاز بانتظار مراجعتك.", "REMINDER", report)
        report.last_manager_reminder_at = now
        sent += 1

    if sent:
        db.session.commit()
    return sent


@portal_bp.route("/followups")
@login_required
def followups_dashboard():
    _require_followups_access()
    own_reports = (
        EmployeeFollowupReport.query
        .filter(EmployeeFollowupReport.employee_user_id == current_user.id)
        .order_by(EmployeeFollowupReport.period_end.desc(), EmployeeFollowupReport.id.desc())
        .all()
    )
    review_reports = []
    if _can_review():
        review_query = EmployeeFollowupReport.query
        if not _can_manage_all():
            review_query = review_query.filter(EmployeeFollowupReport.manager_user_id == current_user.id)
        review_reports = review_query.order_by(
            EmployeeFollowupReport.submitted_at.desc(),
            EmployeeFollowupReport.id.desc(),
        ).all()

    current_start, current_end = _month_bounds()
    metric_reports = review_reports if _can_review() else own_reports
    metric_items = [item for report in metric_reports for item in (report.items or []) if item.is_included]
    completed_count = sum(item.status == "COMPLETED" for item in metric_items)
    incomplete_count = sum(item.status != "COMPLETED" for item in metric_items)
    total_items = completed_count + incomplete_count
    completion_rate = round((completed_count / total_items) * 100) if total_items else 0
    if _can_review():
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

    missing_reports = 0
    if _can_review():
        direct_employee_ids = {
            int(user_id) for (user_id,) in (
                EmployeeFile.query
                .filter(EmployeeFile.direct_manager_user_id == current_user.id)
                .with_entities(EmployeeFile.user_id)
                .all()
            ) if user_id
        }
        submitted_ids = {
            int(user_id) for (user_id,) in (
                EmployeeFollowupReport.query
                .filter(EmployeeFollowupReport.manager_user_id == current_user.id)
                .filter(EmployeeFollowupReport.period_start <= current_end)
                .filter(EmployeeFollowupReport.period_end >= current_start)
                .filter(EmployeeFollowupReport.status.in_(("SUBMITTED", "NEEDS_REVISION", "REVIEWED")))
                .with_entities(EmployeeFollowupReport.employee_user_id)
                .all()
            ) if user_id
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
        can_review=_can_review(),
        can_delete_own_reports=True,
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
    manager = resolve_direct_manager(current_user.id)
    if request.method == "POST":
        period_start = _parse_date(form["period_start"])
        period_end = _parse_date(form["period_end"])
        if not period_start or not period_end or period_end < period_start:
            flash("يرجى تحديد فترة تقرير صحيحة.", "warning")
        else:
            report = EmployeeFollowupReport(
                employee_user_id=current_user.id,
                manager_user_id=getattr(manager, "id", None),
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
    )


@portal_bp.route("/followups/<int:report_id>")
@login_required
def followups_view(report_id: int):
    _require_followups_access()
    report, access_level = _get_report_or_abort(report_id)
    can_edit = _employee_can_edit(report)
    can_review = access_level == "manager" and _can_review() and report.status == "SUBMITTED"
    return render_template(
        "portal/followups/view.html",
        report=report,
        access_level=access_level,
        can_edit=can_edit,
        can_review=can_review,
        users=User.query.order_by(func.coalesce(User.name, User.email).asc()).all(),
        report_status_labels=REPORT_STATUS_LABELS,
        item_status_labels=ITEM_STATUS_LABELS,
        rating_labels=RATING_LABELS,
    )


@portal_bp.route("/followups/<int:report_id>/delete", methods=["POST"])
@login_required
def followups_delete_report(report_id: int):
    _require_followups_access()
    report, access_level = _get_report_or_abort(report_id)
    if not _can_delete_own_report(report, access_level):
        abort(403)

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
            _run_local_assistant(report)
            db.session.commit()
            flash("تم إعداد اقتراحات محلية للمراجعة؛ لن تُرسل تلقائياً.", "success")
        elif action == "submit":
            manager = resolve_direct_manager(current_user.id)
            if not manager:
                raise ValueError("manager_not_found")
            report.manager_user_id = manager.id
            report.status = "SUBMITTED"
            report.submitted_at = datetime.utcnow()
            report.reviewed_at = None
            recipients = {manager.id, *_copy_recipient_ids(report)}
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
            title=title[:255],
            description=(request.form.get("description") or "").strip() or None,
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
    if level != "manager" or not _can_review() or report.status != "SUBMITTED":
        abort(403)
    action = (request.form.get("action") or "review").strip().lower()
    report.manager_comment = (request.form.get("manager_comment") or "").strip() or None
    rating = (request.form.get("manager_rating") or "").strip().upper()
    report.manager_rating = rating if rating in RATING_LABELS else None
    for item in report.items or []:
        item.manager_comment = (request.form.get(f"manager_comment_{item.id}") or "").strip() or None
        item_rating = (request.form.get(f"manager_rating_{item.id}") or "").strip().upper()
        item.manager_rating = item_rating if item_rating in RATING_LABELS else None
    report.reviewed_at = datetime.utcnow()
    if action == "return":
        report.status = "NEEDS_REVISION"
        message = "تمت إعادة تقرير الإنجاز للتعديل مع ملاحظات المدير."
        flash_message = "تمت إعادة التقرير للموظف للتعديل."
        notification_type = "FOLLOWUP_REVISION"
    else:
        report.status = "REVIEWED"
        message = "تمت مراجعة تقرير الإنجاز واعتماده إلكترونياً من المدير."
        flash_message = "تمت مراجعة التقرير واعتماده إلكترونياً."
        notification_type = "FOLLOWUP_REVIEWED"
    _notify([report.employee_user_id], message, notification_type, report)
    db.session.commit()
    flash(flash_message, "success")
    return redirect(url_for("portal.followups_view", report_id=report.id))
