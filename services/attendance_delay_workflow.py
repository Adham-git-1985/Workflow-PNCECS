"""Workflow helpers for official attendance-delay notices.

The attendance summary is kept as the measured source of truth.  This module
stores the request-specific snapshot, resolves the approval audience, and
contains the small background-job operation that raises the two-hour alert.
"""

from __future__ import annotations

import json
import mimetypes
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable

from extensions import db
from models import (
    ArchivedFile,
    AuditLog,
    Department,
    Directorate,
    HRAttendanceDelayRequest,
    Notification,
    OrgNodeManager,
    OrgUnitManager,
    RequestAttachment,
    RequestEscalation,
    SystemSetting,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
)
from services.hr_request_workflow import (
    administrative_affairs_manager_user_ids,
    hr_notification_user_ids,
    secretary_general_user_ids,
)
from utils.file_uploads import clean_original_filename, random_storage_name
from utils.notification_links import notification_target_path


ATTENDANCE_DELAY_WORKFLOW_LABEL = "نموذج تأخير عن العمل"
ATTENDANCE_DELAY_RESPONSE_LABEL = "نموذج تبرير غياب / تأخير"
ATTENDANCE_DELAY_OVERDUE_TYPE = "ATTENDANCE_DELAY_OVERDUE"


def _compact_attendance_label(value: str | None) -> str:
    """Normalize Arabic/English role and organization labels for routing."""
    text = str(value or "").strip().casefold()
    for source, target in (
        ("أ", "ا"),
        ("إ", "ا"),
        ("آ", "ا"),
        ("ى", "ي"),
        ("ة", "ه"),
        ("ؤ", "و"),
        ("ئ", "ي"),
    ):
        text = text.replace(source, target)
    return "".join(character for character in text if character.isalnum())


def _attendance_delay_setting_user_ids(keys: Iterable[str]) -> set[int]:
    """Read optional per-installation overrides without requiring a migration."""
    result: set[int] = set()
    try:
        rows = SystemSetting.query.filter(SystemSetting.key.in_(tuple(keys))).all()
    except Exception:
        return result

    for row in rows:
        raw = (getattr(row, "value", None) or "").strip()
        if not raw:
            continue
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = raw.replace(";", ",").split(",")
        values = decoded if isinstance(decoded, (list, tuple, set)) else [decoded]
        for value in values:
            try:
                user_id = int(value)
            except (TypeError, ValueError):
                continue
            if user_id > 0 and db.session.get(User, user_id) is not None:
                result.add(user_id)
    return result


def _attendance_delay_profile_user_ids(markers: Iterable[str]) -> set[int]:
    normalized_markers = tuple(
        _compact_attendance_label(marker) for marker in markers if marker
    )
    if not normalized_markers:
        return set()
    result: set[int] = set()
    try:
        rows = User.query.with_entities(User.id, User.role, User.job_title).all()
    except Exception:
        return result
    for user_id, role, job_title in rows:
        role_text = _compact_attendance_label(role)
        title_text = _compact_attendance_label(job_title)
        if any(marker in role_text or marker in title_text for marker in normalized_markers):
            result.add(int(user_id))
    return result


def _attendance_delay_node_manager_ids(
    label_matcher,
    *,
    legacy_unit_types: tuple[str, ...] = (),
) -> set[int]:
    """Resolve managers from both the approved dynamic chart and legacy units."""
    result: set[int] = set()
    try:
        for assignment in OrgNodeManager.query.all():
            node = getattr(assignment, "node", None)
            node_type = getattr(node, "type", None)
            labels = (
                getattr(node, "name_ar", None),
                getattr(node, "name_en", None),
                getattr(node, "code", None),
                getattr(node_type, "name_ar", None),
                getattr(node_type, "name_en", None),
                getattr(node_type, "code", None),
            )
            if assignment.manager_user_id and label_matcher(*labels):
                result.add(int(assignment.manager_user_id))
    except Exception:
        pass

    if not legacy_unit_types:
        return result
    try:
        assignments = OrgUnitManager.query.filter(
            OrgUnitManager.unit_type.in_(legacy_unit_types)
        ).all()
        for assignment in assignments:
            unit_type = (assignment.unit_type or "").strip().upper()
            unit = None
            if unit_type == "DIRECTORATE":
                unit = db.session.get(Directorate, assignment.unit_id)
            elif unit_type == "DEPARTMENT":
                unit = db.session.get(Department, assignment.unit_id)
            if assignment.manager_user_id and unit and label_matcher(
                getattr(unit, "name_ar", None),
                getattr(unit, "name_en", None),
                getattr(unit, "code", None),
            ):
                result.add(int(assignment.manager_user_id))
    except Exception:
        pass
    return result


def _attendance_delay_hr_department_label(*values: str | None) -> bool:
    text = _compact_attendance_label(" ".join(str(value or "") for value in values))
    return any(
        _compact_attendance_label(marker) in text
        for marker in (
            "دائرة الموارد البشرية",
            "دائرة الشؤون البشرية",
            "human resources department",
            "hr department",
        )
    )


def _attendance_delay_hr_general_directorate_label(*values: str | None) -> bool:
    text = _compact_attendance_label(" ".join(str(value or "") for value in values))
    is_general = any(
        _compact_attendance_label(marker) in text
        for marker in (
            "الإدارة العامة",
            "general administration",
            "general directorate",
            "general director",
            "DIR_PLAN_HR_FIN",
        )
    )
    has_resources = "موارد" in text or "resources" in text
    has_people_or_finance = any(
        _compact_attendance_label(marker) in text
        for marker in (
            "البشرية",
            "الإدارية",
            "المالية",
            "humanresources",
            "administrative",
            "financial",
        )
    )
    return bool(
        is_general
        and (
            (has_resources and has_people_or_finance)
            or ("administrative" in text and "financial" in text)
            or "dirplanhrfin" in text
        )
    )


def _attendance_delay_hr_affairs_label(*values: str | None) -> bool:
    text = _compact_attendance_label(" ".join(str(value or "") for value in values))
    return any(
        _compact_attendance_label(marker) in text
        for marker in (
            "الشؤون البشرية",
            "شؤون بشرية",
            "human resources affairs",
            "personnel affairs",
        )
    )


def attendance_delay_hr_department_manager_user_ids() -> list[int]:
    """Return the manager of the Human Resources Department."""
    ids = _attendance_delay_setting_user_ids((
        "HR_ATTENDANCE_DELAY_HR_DEPARTMENT_MANAGER_USER_IDS",
        "HR_DEPARTMENT_MANAGER_USER_IDS",
    ))
    ids.update(_attendance_delay_node_manager_ids(
        _attendance_delay_hr_department_label,
        legacy_unit_types=("DEPARTMENT",),
    ))
    ids.update(_attendance_delay_profile_user_ids((
        "مدير دائرة الموارد البشرية",
        "مدير دائرة الشؤون البشرية",
        "HR_DEPARTMENT_MANAGER",
        "HUMAN_RESOURCES_DEPARTMENT_MANAGER",
        "HR_DIRECTOR",
        "HUMAN_RESOURCES_DIRECTOR",
        "PERSONNEL_DIRECTOR",
    )))
    return sorted(ids)


def attendance_delay_hr_general_director_user_ids() -> list[int]:
    """Return the general director responsible for administrative/financial HR resources."""
    ids = _attendance_delay_setting_user_ids((
        "HR_ATTENDANCE_DELAY_HR_GENERAL_DIRECTOR_USER_IDS",
        "HR_GENERAL_DIRECTOR_USER_IDS",
    ))
    ids.update(_attendance_delay_node_manager_ids(
        _attendance_delay_hr_general_directorate_label,
        legacy_unit_types=("DIRECTORATE",),
    ))
    ids.update(_attendance_delay_profile_user_ids((
        "مدير عام الإدارة العامة للموارد الإدارية والمالية",
        "مدير عام الإدارة العامة للتخطيط والموارد البشرية والمالية",
        "HR_GENERAL_DIRECTOR",
        "GENERAL_DIRECTOR_HR",
        "ADMINISTRATIVE_FINANCIAL_GENERAL_DIRECTOR",
        "GENERAL_DIRECTOR_ADMINISTRATIVE_FINANCIAL",
    )))
    return sorted(ids)


def attendance_delay_hr_affairs_manager_user_ids() -> list[int]:
    """Return the final HR Affairs manager for attendance-delay requests."""
    ids = _attendance_delay_setting_user_ids((
        "HR_ATTENDANCE_DELAY_HR_AFFAIRS_MANAGER_USER_IDS",
        "HR_AFFAIRS_MANAGER_USER_IDS",
    ))
    ids.update(_attendance_delay_node_manager_ids(
        _attendance_delay_hr_affairs_label,
        legacy_unit_types=("DEPARTMENT", "DIRECTORATE"),
    ))
    ids.update(_attendance_delay_profile_user_ids((
        "مدير الشؤون البشرية",
        "مدير شؤون الموظفين",
        "مدير الموارد البشرية",
        "HR_AFFAIRS_MANAGER",
        "HUMAN_RESOURCES_AFFAIRS_MANAGER",
        "PERSONNEL_MANAGER",
        "HUMAN_RESOURCES_MANAGER",
        "HR_MANAGER",
    )))
    # In the approved organization chart this responsibility is currently
    # represented by the manager of the Human Resources Department, without a
    # separate "HR Affairs Manager" user/role. Keep the final stage usable in
    # that configuration; a separately configured HR-affairs manager above
    # always takes precedence.
    if not ids:
        ids.update(attendance_delay_hr_department_manager_user_ids())
    return sorted(ids)


def _as_ids(values: Iterable[int | None]) -> set[int]:
    result: set[int] = set()
    for value in values or ():
        try:
            if value:
                result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def get_delay_case_for_request(request_id: int | None) -> HRAttendanceDelayRequest | None:
    if not request_id:
        return None
    return HRAttendanceDelayRequest.query.filter_by(
        workflow_request_id=int(request_id),
    ).first()


def purge_orphan_delay_cases() -> int:
    """Remove delay rows whose generic workflow request no longer exists.

    A failed/aborted first attempt can leave an attendance-delay row behind
    while its ``WorkflowRequest`` is rolled back or removed. SQLite may then
    reuse that request id, and the next attempt fails on the unique
    ``workflow_request_id`` constraint before the workflow can start. Such a
    row cannot be opened or progressed, so it is safe to remove it as part of
    the next start attempt. The caller's transaction controls the commit.
    """
    orphan_ids = [
        row_id
        for (row_id,) in (
            db.session.query(HRAttendanceDelayRequest.id)
            .outerjoin(
                WorkflowRequest,
                WorkflowRequest.id == HRAttendanceDelayRequest.workflow_request_id,
            )
            .filter(WorkflowRequest.id.is_(None))
            .all()
        )
        if row_id
    ]
    if not orphan_ids:
        return 0

    deleted = (
        db.session.query(HRAttendanceDelayRequest)
        .filter(HRAttendanceDelayRequest.id.in_(orphan_ids))
        .delete(synchronize_session="fetch")
    )
    db.session.flush()
    return int(deleted or 0)


def is_attendance_delay_workflow(req: WorkflowRequest | None) -> bool:
    return bool(req and get_delay_case_for_request(getattr(req, "id", None)))


def attendance_delay_parallel_candidate_user_ids(
    req: WorkflowRequest | None,
    step: WorkflowInstanceStep | None = None,
) -> list[int]:
    """Return the fixed candidates for an attendance-delay parallel step.

    New requests use this hook for the two HR approvals.  The Secretary-General
    fallback is retained for older in-flight requests created before the HR
    stage was inserted into the route.
    """
    if not is_attendance_delay_workflow(req):
        return []
    if (
        step
        and (getattr(step, "approver_role", None) or "").strip().upper()
        == "ATTENDANCE_DELAY_HR_PARALLEL"
    ):
        department_ids = attendance_delay_hr_department_manager_user_ids()
        general_director_ids = attendance_delay_hr_general_director_user_ids()
        # The runtime step stores the selected department manager. Resolve the
        # selected general director in the same deterministic order used by
        # the start route, so a later directory change cannot add extra tasks.
        selected_ids = set()
        if getattr(step, "approver_user_id", None):
            selected_ids.add(int(step.approver_user_id))
        if general_director_ids:
            selected_ids.add(int(general_director_ids[0]))
        if selected_ids:
            return sorted(selected_ids)
        return sorted(set(department_ids[:1] + general_director_ids[:1]))
    return sorted(_as_ids(secretary_general_user_ids()))


def attendance_delay_secretary_user_ids() -> set[int]:
    return _as_ids(secretary_general_user_ids())


def mark_delay_case_final(
    req: WorkflowRequest | None,
    *,
    status: str,
    decision_by_id: int | None,
    decided_at: datetime | None = None,
) -> HRAttendanceDelayRequest | None:
    case = get_delay_case_for_request(getattr(req, "id", None))
    if not case:
        return None
    case.final_status = (status or "").strip().upper() or None
    case.final_decision_at = decided_at or datetime.utcnow()
    case.final_decision_by_id = int(decision_by_id) if decision_by_id else None
    db.session.add(case)
    return case


def notify_delay_participants(
    req: WorkflowRequest | None,
    message: str,
    *,
    user_ids: Iterable[int] | None = None,
    notification_type: str = "WORKFLOW",
    actor_id: int | None = None,
) -> None:
    """Add workflow notifications without importing the workflow engine."""
    if not req:
        return
    recipients = _as_ids(user_ids or ())
    if not recipients:
        return
    now = datetime.utcnow()
    link_url = notification_target_path("WorkflowRequest", req.id)
    event_key = uuid.uuid4().hex
    for user_id in sorted(recipients):
        db.session.add(
            Notification(
                user_id=user_id,
                type=notification_type,
                message=(message or "")[:255],
                is_read=False,
                created_at=now,
                actor_id=actor_id,
                event_key=event_key,
                is_mirror=False,
                source="workflow",
                link_url=link_url,
                email_delivery_mode="GENERAL",
            )
        )


def notify_delay_final_decision(req: WorkflowRequest, status: str, actor_id: int | None) -> None:
    case = get_delay_case_for_request(req.id)
    if not case:
        return
    recipients = _as_ids((case.employee_id, case.initiated_by_id))
    recipients.update(hr_notification_user_ids())
    status_label = "اعتماد نهائي" if status == "APPROVED" else "رفض نهائي"
    notify_delay_participants(
        req,
        f"{status_label} لمسار نموذج تأخير عن العمل #{req.id} للموظف {getattr(case.employee, 'full_name', case.employee_id)}.",
        user_ids=recipients,
        actor_id=actor_id,
    )


def archive_generated_file(
    req: WorkflowRequest,
    payload: bytes,
    filename: str,
    *,
    owner_id: int,
    step_order: int | None,
    source: str,
    description: str | None = None,
) -> tuple[ArchivedFile, str]:
    """Persist a generated official file and attach it to the workflow request."""
    original_name = clean_original_filename(filename)
    if not original_name:
        raise ValueError("اسم الملف الرسمي غير صالح")
    archive_dir = Path(os.getcwd()) / "storage" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stored_name = random_storage_name(uuid.uuid4().hex, original_name)
    saved_path = archive_dir / stored_name
    saved_path.write_bytes(payload)
    archived = ArchivedFile(
        original_name=original_name,
        stored_name=stored_name,
        description=description,
        file_path=str(saved_path),
        mime_type=mimetypes.guess_type(original_name)[0] or "application/octet-stream",
        file_size=len(payload),
        owner_id=int(owner_id),
        visibility="workflow",
    )
    db.session.add(archived)
    db.session.flush()
    db.session.add(RequestAttachment(request_id=req.id, archived_file_id=archived.id))
    db.session.add(
        AuditLog(
            request_id=req.id,
            user_id=owner_id,
            action="WORKFLOW_ATTACHMENT_UPLOADED",
            old_status=None,
            new_status=None,
            note=(
                f"Attachment: {original_name} | file_id={archived.id} | "
                f"step={step_order if step_order is not None else ''} | source={source}"
            ),
            target_type="ARCHIVE_FILE",
            target_id=archived.id,
            created_at=datetime.utcnow(),
        )
    )
    return archived, str(saved_path)


def archive_generated_pdf(
    req: WorkflowRequest,
    payload: bytes,
    filename: str,
    *,
    owner_id: int,
    step_order: int | None,
    source: str,
    description: str | None = None,
) -> tuple[ArchivedFile, str]:
    """Backward-compatible wrapper for generated PDF attachments."""
    return archive_generated_file(
        req,
        payload,
        filename,
        owner_id=owner_id,
        step_order=step_order,
        source=source,
        description=description,
    )


def archive_generated_docx(
    req: WorkflowRequest,
    payload: bytes,
    filename: str,
    *,
    owner_id: int,
    step_order: int | None,
    source: str,
    description: str | None = None,
) -> tuple[ArchivedFile, str]:
    """Persist an editable Word form and attach it to the workflow request."""
    return archive_generated_file(
        req,
        payload,
        filename,
        owner_id=owner_id,
        step_order=step_order,
        source=source,
        description=description,
    )


def process_pending_delay_response_alerts(*, now: datetime | None = None) -> int:
    """Raise the one-time two-hour warning for unanswered employee requests."""
    now = now or datetime.utcnow()
    cases = (
        HRAttendanceDelayRequest.query
        .filter(HRAttendanceDelayRequest.employee_responded_at.is_(None))
        .filter(HRAttendanceDelayRequest.response_due_at.isnot(None))
        .filter(HRAttendanceDelayRequest.response_due_at <= now)
        .filter(HRAttendanceDelayRequest.overdue_notified_at.is_(None))
        .filter(HRAttendanceDelayRequest.final_status.is_(None))
        .order_by(HRAttendanceDelayRequest.id.asc())
        .all()
    )
    processed = 0
    for case in cases:
        req = case.workflow_request
        if not req or (req.status or "").strip().upper() in {"APPROVED", "REJECTED", "CLOSED"}:
            continue
        inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
        current_step = (
            WorkflowInstanceStep.query.filter_by(
                instance_id=inst.id,
                step_order=inst.current_step_order,
                status="PENDING",
            ).first()
            if inst else None
        )
        if not current_step or int(current_step.step_order or 0) != 1:
            continue

        case.overdue_notified_at = now
        db.session.add(case)
        employee_message = (
            f"تنبيه: لم يتم الرد على نموذج تأخير عن العمل #{req.id} خلال ساعتين. "
            "قد تُتخذ إجراءات إدارية عند عدم تزويد التبرير."
        )
        admin_message = (
            f"تأخر الموظف عن الرد أكثر من ساعتين على نموذج تأخير عن العمل #{req.id}. "
            "يرجى متابعة التبرير والإجراء الإداري عند الحاجة."
        )
        notify_delay_participants(
            req,
            employee_message,
            user_ids={int(case.employee_id)},
            notification_type=ATTENDANCE_DELAY_OVERDUE_TYPE,
        )
        recipients = _as_ids((case.initiated_by_id,))
        recipients.update(hr_notification_user_ids())
        recipients.update(administrative_affairs_manager_user_ids())
        notify_delay_participants(
            req,
            admin_message,
            user_ids=recipients,
            notification_type=ATTENDANCE_DELAY_OVERDUE_TYPE,
        )
        escalation_target_ids = sorted(recipients or {int(case.initiated_by_id)})
        target_id = escalation_target_ids[0]
        db.session.add(
            RequestEscalation(
                request_id=req.id,
                from_user_id=int(case.employee_id),
                to_user_id=int(target_id),
                category="ATTENDANCE_DELAY_RESPONSE",
                description=admin_message,
                alert_level=1,
                step_order=1,
                targets=",".join(str(value) for value in escalation_target_ids),
                created_at=now,
            )
        )
        db.session.add(
            AuditLog(
                request_id=req.id,
                user_id=case.initiated_by_id,
                action="ATTENDANCE_DELAY_RESPONSE_OVERDUE",
                old_status=None,
                new_status=None,
                note=admin_message,
                target_type="WORKFLOW_STEP",
                target_id=current_step.id,
                created_at=now,
            )
        )
        processed += 1
    return processed


def employee_form_data(case: HRAttendanceDelayRequest, employee: User | None = None) -> dict:
    employee = employee or case.employee
    employee_file = getattr(employee, "employee_file", None) if employee else None
    department = getattr(getattr(employee_file, "department", None), "name_ar", None)
    if not department:
        department = getattr(getattr(employee, "department", None), "name_ar", None)
    return {
        "employee_name": getattr(employee, "full_name", None) if employee else "",
        "employee_no": getattr(employee_file, "employee_no", None) if employee_file else "",
        "job_title": getattr(employee, "job_title", None) or "",
        "department": department or "",
        "delay_day": case.delay_day,
        "first_in_time": case.first_in_time or "",
        "late_minutes": int(case.late_minutes or 0),
        "early_leave_minutes": int(case.early_leave_minutes or 0),
    }


def json_payload(value: dict) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))
