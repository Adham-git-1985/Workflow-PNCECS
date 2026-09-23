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
    HRAttendanceDelayRequest,
    Notification,
    RequestAttachment,
    RequestEscalation,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
)
from services.hr_request_workflow import (
    administrative_affairs_manager_user_ids,
    hr_approval_user_ids,
    hr_notification_user_ids,
    secretary_general_user_ids,
)
from utils.file_uploads import clean_original_filename, random_storage_name
from utils.notification_links import notification_target_path


ATTENDANCE_DELAY_WORKFLOW_LABEL = "نموذج تأخير عن العمل"
ATTENDANCE_DELAY_RESPONSE_LABEL = "نموذج تبرير غياب / تأخير"
ATTENDANCE_DELAY_OVERDUE_TYPE = "ATTENDANCE_DELAY_OVERDUE"


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


def is_attendance_delay_workflow(req: WorkflowRequest | None) -> bool:
    return bool(req and get_delay_case_for_request(getattr(req, "id", None)))


def attendance_delay_parallel_candidate_user_ids(
    req: WorkflowRequest | None,
    step: WorkflowInstanceStep | None = None,
) -> list[int]:
    """Return HR + Secretary-General candidates for the final review step."""
    if not is_attendance_delay_workflow(req):
        return []
    # The runtime step itself resolves the HR role.  Adding the explicit HR
    # resolver keeps the path stable even when a deployment uses a custom role
    # label, while the Secretary-General is intentionally an extra candidate.
    return sorted(
        _as_ids(hr_approval_user_ids())
        | _as_ids(secretary_general_user_ids())
    )


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
    """Persist a generated PDF and attach it to the workflow request."""
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
        mime_type=mimetypes.guess_type(original_name)[0] or "application/pdf",
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
