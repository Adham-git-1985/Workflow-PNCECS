"""Approval routing for HR leave and permission requests.

The HR request tables predate the generic workflow engine.  This module adds a
small, auditable runtime workflow without changing their public status fields.
Approval and read-only CC recipients are deliberately separate concepts.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import func, or_

from extensions import db
from models import (
    AuditLog,
    Delegation,
    Department,
    Directorate,
    EmployeeFile,
    HRLeaveRequest,
    HRPermissionRequest,
    HRRequestApprovalStep,
    HRRequestObserver,
    Notification,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    OrgUnitAssignment,
    OrgUnitManager,
    SystemSetting,
    User,
)
from utils.notification_links import notification_target_path


KIND_LEAVE = "LEAVE"
KIND_PERMISSION = "PERMISSION"

STAGE_DIRECT_MANAGER = "DIRECT_MANAGER"
STAGE_HR = "HR"
STAGE_SECRETARY_GENERAL = "SECRETARY_GENERAL"

SCOPE_USER = "USER"
SCOPE_HR = "HR"
SCOPE_SECRETARY_GENERAL = "SECRETARY_GENERAL"

ACTIVE_STEP_STATUSES = {"PENDING", "WAITING"}


def _normalize(value: str | None) -> str:
    return "".join(ch for ch in (value or "").strip().upper() if ch.isalnum())


def _setting_int(key: str, default: int, minimum: int = 1) -> int:
    try:
        raw = db.session.query(SystemSetting.value).filter(SystemSetting.key == key).scalar()
        value = int((raw or "").strip())
        return value if value >= minimum else default
    except Exception:
        return default


def add_working_days(start: datetime, working_days: int) -> datetime:
    """Add Sunday-Thursday working days while preserving the time of day."""
    current = start
    remaining = max(0, int(working_days or 0))
    while remaining:
        current += timedelta(days=1)
        if current.weekday() not in {4, 5}:  # Friday/Saturday
            remaining -= 1
    return current


def _request_link(kind: str, request_id: int) -> str:
    endpoint = "leaves" if kind == KIND_LEAVE else "permissions"
    return f"/portal/hr/approvals/{endpoint}/{int(request_id)}"


def _notify(user_ids: Iterable[int], message: str, *, kind: str, request_id: int, ntype: str = "HR_APPROVAL") -> None:
    now = datetime.utcnow()
    try:
        link = notification_target_path(
            "HR_LEAVE_REQUEST" if kind == KIND_LEAVE else "HR_PERMISSION_REQUEST",
            request_id,
        ) or _request_link(kind, request_id)
    except Exception:
        link = _request_link(kind, request_id)
    recipient_ids = {int(value) for value in user_ids if value}
    if (kind or "").upper() == KIND_PERMISSION:
        recipient_ids.intersection_update(_permission_notification_recipient_ids(request_id))
    for user_id in sorted(recipient_ids):
        db.session.add(Notification(
            user_id=user_id,
            type=ntype,
            message=message,
            source="portal",
            link_url=link,
            is_read=False,
            created_at=now,
        ))


def _request(kind: str, request_id: int):
    if kind == KIND_LEAVE:
        return db.session.get(HRLeaveRequest, int(request_id))
    if kind == KIND_PERMISSION:
        return db.session.get(HRPermissionRequest, int(request_id))
    return None


def _request_employee_name(row) -> str:
    try:
        return row.user.full_name
    except Exception:
        return f"#{getattr(row, 'user_id', '')}"


def _request_label(kind: str) -> str:
    return "إجازة" if kind == KIND_LEAVE else "مغادرة"


def _active_delegation_for(user_id: int, now: datetime | None = None) -> Delegation | None:
    now = now or datetime.utcnow()
    return (
        Delegation.query
        .filter(
            Delegation.from_user_id == int(user_id),
            Delegation.is_active.is_(True),
            Delegation.starts_at <= now,
            Delegation.expires_at >= now,
        )
        .order_by(Delegation.expires_at.desc(), Delegation.id.desc())
        .first()
    )


def _dynamic_node_chain(user_id: int):
    assignment = (
        OrgNodeAssignment.query.filter_by(user_id=int(user_id), is_primary=True).first()
        or OrgNodeAssignment.query.filter_by(user_id=int(user_id)).order_by(OrgNodeAssignment.id.desc()).first()
    )
    node = assignment.node if assignment else None
    seen: set[int] = set()
    while node is not None and node.id not in seen:
        seen.add(int(node.id))
        yield node
        node = node.parent


def _unit_parent(unit_type: str, unit_id: int):
    unit_type = (unit_type or "").upper()
    try:
        if unit_type == "SECTION":
            from models import Section
            row = db.session.get(Section, unit_id)
            return ("DEPARTMENT", row.department_id) if row and row.department_id else (None, None)
        if unit_type == "DIVISION":
            from models import Division
            row = db.session.get(Division, unit_id)
            if row and getattr(row, "section_id", None):
                return "SECTION", row.section_id
            if row and getattr(row, "department_id", None):
                return "DEPARTMENT", row.department_id
        if unit_type == "DEPARTMENT":
            row = db.session.get(Department, unit_id)
            return ("DIRECTORATE", row.directorate_id) if row and row.directorate_id else (None, None)
        if unit_type == "DIRECTORATE":
            row = db.session.get(Directorate, unit_id)
            return ("ORGANIZATION", row.organization_id) if row and row.organization_id else (None, None)
    except Exception:
        pass
    return None, None


def _legacy_unit_chain(user_id: int):
    user = db.session.get(User, int(user_id))
    if not user:
        return
    assignment = (
        OrgUnitAssignment.query.filter_by(user_id=user.id, is_primary=True).first()
        or OrgUnitAssignment.query.filter_by(user_id=user.id).order_by(OrgUnitAssignment.id.desc()).first()
    )
    candidates: list[tuple[str, int]] = []
    if assignment and assignment.unit_type and assignment.unit_id:
        candidates.append((assignment.unit_type.upper(), int(assignment.unit_id)))
    else:
        for unit_type, field in (
            ("DIVISION", "division_id"),
            ("SECTION", "section_id"),
            ("DEPARTMENT", "department_id"),
            ("DIRECTORATE", "directorate_id"),
        ):
            value = getattr(user, field, None)
            if value:
                candidates.append((unit_type, int(value)))
                break

    seen: set[tuple[str, int]] = set()
    while candidates:
        unit_type, unit_id = candidates.pop(0)
        key = (unit_type, unit_id)
        if key in seen:
            continue
        seen.add(key)
        yield key
        parent_type, parent_id = _unit_parent(unit_type, unit_id)
        if parent_type and parent_id:
            candidates.append((parent_type, int(parent_id)))

    # A number of legacy users have only the explicit directorate field.
    if getattr(user, "directorate_id", None):
        key = ("DIRECTORATE", int(user.directorate_id))
        if key not in seen:
            yield key


def _manager_from_row(row, employee_user_id: int) -> User | None:
    if row and row.manager_user_id and int(row.manager_user_id) != int(employee_user_id):
        return row.manager_user
    if row and row.deputy_user_id and int(row.deputy_user_id) != int(employee_user_id):
        return row.deputy_user
    return None


def resolve_direct_manager(user_id: int) -> User | None:
    """Resolve the responsible manager from employee data and both org models."""
    employee_file = db.session.get(EmployeeFile, int(user_id))
    if employee_file and employee_file.direct_manager_user_id:
        manager_id = int(employee_file.direct_manager_user_id)
        if manager_id != int(user_id):
            manager = db.session.get(User, manager_id)
            if manager:
                return manager

    for node in _dynamic_node_chain(user_id):
        manager = _manager_from_row(
            OrgNodeManager.query.filter_by(node_id=node.id).first(),
            user_id,
        )
        if manager:
            return manager

    for unit_type, unit_id in _legacy_unit_chain(user_id):
        manager = _manager_from_row(
            OrgUnitManager.query.filter_by(unit_type=unit_type, unit_id=unit_id).first(),
            user_id,
        )
        if manager:
            return manager
    return None


def resolve_responsible_managers(user_id: int) -> list[User]:
    """Resolve one responsible manager from every active org placement.

    Leave approvals use all returned managers in parallel. Older employee data
    that has no dynamic placement continues to use the existing direct-manager
    resolution as a fallback.
    """
    user = db.session.get(User, int(user_id))
    manager_ids: list[int] = []
    if user:
        try:
            from workflow.dynamic_paths import requester_dynamic_manager_options

            manager_ids = [
                int(option["user_id"])
                for option in requester_dynamic_manager_options(user)
                if option.get("user_id") and int(option["user_id"]) != int(user_id)
            ]
        except Exception:
            manager_ids = []

    if not manager_ids:
        manager = resolve_direct_manager(user_id)
        if manager:
            manager_ids = [int(manager.id)]

    seen: set[int] = set()
    managers: list[User] = []
    for manager_id in manager_ids:
        if manager_id in seen:
            continue
        manager = db.session.get(User, manager_id)
        if manager:
            seen.add(manager_id)
            managers.append(manager)
    return managers


def _serialize_approver_ids(user_ids: Iterable[int]) -> str | None:
    normalized: list[int] = []
    seen: set[int] = set()
    for value in user_ids:
        if not value:
            continue
        user_id = int(value)
        if user_id not in seen:
            seen.add(user_id)
            normalized.append(user_id)
    return json.dumps(normalized, separators=(",", ":")) if normalized else None


def _step_approver_ids(step: HRRequestApprovalStep | None) -> list[int]:
    if not step:
        return []
    values: list[int] = []
    raw = (getattr(step, "approver_user_ids", None) or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            parsed_values = parsed if isinstance(parsed, list) else [parsed]
            values.extend(int(value) for value in parsed_values if value)
        except (TypeError, ValueError, json.JSONDecodeError):
            values.extend(int(value.strip()) for value in raw.split(",") if value.strip().isdigit())
    if not values and step.approver_user_id:
        values.append(int(step.approver_user_id))
    return list(dict.fromkeys(values))


def approval_candidate_names_map(steps: Iterable[HRRequestApprovalStep]) -> dict[int, list[str]]:
    """Return ordered parallel-approver names keyed by approval-step id."""
    step_rows = [step for step in steps if step and step.id]
    ids_by_step = {int(step.id): _step_approver_ids(step) for step in step_rows}
    all_ids = {user_id for user_ids in ids_by_step.values() for user_id in user_ids}
    users_by_id = {
        int(user.id): user
        for user in User.query.filter(User.id.in_(all_ids)).all()
    } if all_ids else {}
    return {
        step_id: [
            users_by_id[user_id].full_name or users_by_id[user_id].email or f"#{user_id}"
            for user_id in user_ids
            if user_id in users_by_id
        ]
        for step_id, user_ids in ids_by_step.items()
    }


def direct_approver_names_for_requests(kind: str, request_ids: Iterable[int]) -> dict[int, list[str]]:
    """Return the original direct-stage approver names for request lists."""
    normalized_ids = {int(request_id) for request_id in request_ids if request_id}
    if not normalized_ids:
        return {}
    steps = HRRequestApprovalStep.query.filter(
        HRRequestApprovalStep.request_kind == (kind or "").upper(),
        HRRequestApprovalStep.request_id.in_(normalized_ids),
        HRRequestApprovalStep.stage_code == STAGE_DIRECT_MANAGER,
    ).all()
    names_by_step = approval_candidate_names_map(steps)
    return {int(step.request_id): names_by_step.get(int(step.id), []) for step in steps}


def request_ids_user_participated_in(user: User, kind: str) -> list[int]:
    """Requests the user can still act on or previously received/decided."""
    if not user:
        return []
    request_ids: set[int] = set()
    for step in HRRequestApprovalStep.query.filter_by(request_kind=(kind or "").upper()).all():
        if (
            int(user.id) in _step_approver_ids(step)
            or step.decided_by_id == user.id
            or step.escalated_from_user_id == user.id
        ):
            request_ids.add(int(step.request_id))
    return sorted(request_ids)


def _deputy_for_manager(manager_user_id: int, employee_user_id: int) -> User | None:
    for row in OrgNodeManager.query.filter_by(manager_user_id=int(manager_user_id)).all():
        if row.deputy_user_id and int(row.deputy_user_id) not in {int(manager_user_id), int(employee_user_id)}:
            return row.deputy_user
    for row in OrgUnitManager.query.filter_by(manager_user_id=int(manager_user_id)).all():
        if row.deputy_user_id and int(row.deputy_user_id) not in {int(manager_user_id), int(employee_user_id)}:
            return row.deputy_user
    return None


def resolve_general_director(user_id: int, exclude_ids: Iterable[int] = ()) -> User | None:
    excluded = {int(value) for value in exclude_ids if value}
    excluded.add(int(user_id))

    for node in _dynamic_node_chain(user_id):
        type_code = _normalize(getattr(getattr(node, "type", None), "code", None))
        if type_code not in {"DIRECTORATE", "GENERALDIRECTOR"}:
            continue
        row = OrgNodeManager.query.filter_by(node_id=node.id).first()
        for candidate in (getattr(row, "manager_user", None), getattr(row, "deputy_user", None)):
            if candidate and candidate.id not in excluded:
                return candidate

    for unit_type, unit_id in _legacy_unit_chain(user_id):
        if unit_type != "DIRECTORATE":
            continue
        row = OrgUnitManager.query.filter_by(unit_type="DIRECTORATE", unit_id=unit_id).first()
        for candidate in (getattr(row, "manager_user", None), getattr(row, "deputy_user", None)):
            if candidate and candidate.id not in excluded:
                return candidate
    return None


def secretary_general_user_ids() -> list[int]:
    ids: set[int] = set()
    for user in User.query.all():
        role = _normalize(user.role)
        if role in {"GENERALSECRETARY", "SECRETARYGENERAL"}:
            ids.add(int(user.id))
    try:
        rows = (
            OrgNodeManager.query
            .join(OrgNode, OrgNode.id == OrgNodeManager.node_id)
            .join(OrgNodeType, OrgNodeType.id == OrgNode.type_id)
            .filter(func.upper(OrgNodeType.code) == "SECRETARY_GENERAL")
            .all()
        )
        for row in rows:
            if row.manager_user_id:
                ids.add(int(row.manager_user_id))
    except Exception:
        # Relationship joins differ between SQLAlchemy versions; the role is
        # the stable fallback used by existing deployments.
        pass
    return sorted(ids)


def _is_secretary_general(user: User) -> bool:
    return bool(user and (int(user.id) in secretary_general_user_ids() or _normalize(user.role) in {"GENERALSECRETARY", "SECRETARYGENERAL"}))


def _is_secretariat(user: User) -> bool:
    role = _normalize(getattr(user, "role", None))
    return role in {"GENERALSECRETARY", "SECRETARYGENERAL", "ASSISTANTSECRETARYGENERAL", "SECGENASSIST"}


def _is_hr_approver(user: User) -> bool:
    if not user:
        return False
    role = _normalize(user.role)
    if role in {"HR", "HRMANAGER", "HRADMIN", "ADMIN", "SUPERADMIN"}:
        return True
    try:
        return bool(user.has_perm("HR_EMPLOYEE_MANAGE") or user.has_perm("HR_MASTERDATA_MANAGE"))
    except Exception:
        return False


def hr_observer_user_ids() -> list[int]:
    return sorted({int(user.id) for user in User.query.all() if _is_hr_approver(user)})


def hr_notification_user_ids() -> list[int]:
    """Return HR staff who may receive employee-request notifications.

    Approval permissions are intentionally not enough here: those permissions
    are also assigned to directors and department heads.  Notifications for an
    employee's leave or departure must stay within the HR department.
    """
    hr_department_ids: set[int] = set()
    for department in Department.query.filter_by(is_active=True).all():
        label = " ".join((
            getattr(department, "name_ar", "") or "",
            getattr(department, "name_en", "") or "",
            getattr(department, "code", "") or "",
        )).casefold()
        if "الموارد البشرية" in label or "human resources" in label:
            hr_department_ids.add(int(department.id))

    recipient_ids: set[int] = set()
    for user in User.query.all():
        role = _normalize(getattr(user, "role", None))
        if role in {"HR", "HRMANAGER", "HRADMIN"}:
            recipient_ids.add(int(user.id))
            continue
        if getattr(user, "department_id", None) in hr_department_ids:
            recipient_ids.add(int(user.id))

    if hr_department_ids:
        for employee_file in EmployeeFile.query.filter(EmployeeFile.department_id.in_(hr_department_ids)).all():
            if employee_file.user_id:
                recipient_ids.add(int(employee_file.user_id))
    return sorted(recipient_ids)


def _permission_notification_recipient_ids(request_id: int) -> set[int]:
    """Allowed recipients for an employee departure notification only."""
    row = _request(KIND_PERMISSION, request_id)
    if not row:
        return set()

    recipient_ids: set[int] = {int(row.user_id)}
    for manager in resolve_responsible_managers(int(row.user_id)):
        recipient_ids.add(int(manager.id))
        delegation = _active_delegation_for(manager.id)
        if delegation and delegation.to_user_id:
            recipient_ids.add(int(delegation.to_user_id))
    recipient_ids.update(hr_notification_user_ids())
    return recipient_ids


def _stage_due_at(kind: str, stage_code: str, assigned_at: datetime) -> datetime:
    if kind == KIND_PERMISSION and stage_code == STAGE_DIRECT_MANAGER:
        minutes = _setting_int("HR_PERMISSION_APPROVAL_ESCALATION_MINUTES", 60)
        return assigned_at + timedelta(minutes=minutes)
    days = _setting_int("HR_APPROVAL_ESCALATION_WORKDAYS", 2)
    return add_working_days(assigned_at, days)


def _stage_reminder_at(step: HRRequestApprovalStep) -> datetime:
    assigned = step.assigned_at or step.created_at or datetime.utcnow()
    if step.request_kind == KIND_PERMISSION and step.stage_code == STAGE_DIRECT_MANAGER:
        return assigned + timedelta(minutes=_setting_int("HR_PERMISSION_APPROVAL_REMINDER_MINUTES", 30))
    return add_working_days(assigned, _setting_int("HR_APPROVAL_REMINDER_WORKDAYS", 1))


def is_special_leave(row: HRLeaveRequest) -> bool:
    leave_type = getattr(row, "leave_type", None)
    if leave_type and bool(getattr(leave_type, "is_external", False)):
        return True
    if (getattr(row, "leave_place", None) or "").upper() == "EXTERNAL":
        return True
    try:
        return bool(leave_type and leave_type.max_days and row.days and int(row.days) > int(leave_type.max_days))
    except Exception:
        return False


def _step_specs(kind: str, row) -> list[tuple[str, str]]:
    specs = [(STAGE_DIRECT_MANAGER, SCOPE_USER)]
    if kind == KIND_LEAVE and is_special_leave(row):
        specs.extend(((STAGE_HR, SCOPE_HR), (STAGE_SECRETARY_GENERAL, SCOPE_SECRETARY_GENERAL)))
    return specs


def start_request_flow(kind: str, row, *, now: datetime | None = None) -> list[HRRequestApprovalStep]:
    """Create approval steps and notify the first approval stage.

    The function is idempotent so legacy/admin routes can safely call it after
    flushing a request.
    """
    kind = (kind or "").upper()
    now = now or datetime.utcnow()
    existing = (
        HRRequestApprovalStep.query
        .filter_by(request_kind=kind, request_id=int(row.id))
        .order_by(HRRequestApprovalStep.step_order.asc())
        .all()
    )
    if existing:
        return existing

    managers = resolve_responsible_managers(int(row.user_id))
    original_manager_id = managers[0].id if managers else None
    first_manager_was_delegated = False
    effective_approver_ids: list[int] = []
    for index, manager in enumerate(managers):
        delegation = _active_delegation_for(manager.id, now)
        effective_id = int(delegation.to_user_id) if delegation else int(manager.id)
        if index == 0 and delegation:
            first_manager_was_delegated = True
        if effective_id not in effective_approver_ids:
            effective_approver_ids.append(effective_id)
    initial_escalation_reason = "ACTIVE_DELEGATION" if first_manager_was_delegated else None

    if not effective_approver_ids:
        first_approver = resolve_general_director(int(row.user_id))
        if first_approver:
            effective_approver_ids = [int(first_approver.id)]
        initial_escalation_reason = "NO_DIRECT_MANAGER" if first_approver else None
    if not effective_approver_ids:
        secretary_ids = secretary_general_user_ids()
        if secretary_ids:
            effective_approver_ids = [int(secretary_ids[0])]
        initial_escalation_reason = "NO_ORG_MANAGER" if effective_approver_ids else None

    first_approver_id = effective_approver_ids[0] if effective_approver_ids else None

    steps: list[HRRequestApprovalStep] = []
    for order, (stage_code, approver_scope) in enumerate(_step_specs(kind, row), start=1):
        approver_user_id = None
        approver_user_ids = None
        if stage_code == STAGE_DIRECT_MANAGER:
            approver_user_id = first_approver_id
            approver_user_ids = _serialize_approver_ids(effective_approver_ids)
        elif stage_code == STAGE_SECRETARY_GENERAL:
            secretary_ids = secretary_general_user_ids()
            approver_user_id = secretary_ids[0] if len(secretary_ids) == 1 else None

        active = order == 1
        step = HRRequestApprovalStep(
            request_kind=kind,
            request_id=int(row.id),
            step_order=order,
            stage_code=stage_code,
            approver_scope=approver_scope,
            approver_user_id=approver_user_id,
            approver_user_ids=approver_user_ids,
            status="PENDING" if active else "WAITING",
            assigned_at=now if active else None,
            due_at=_stage_due_at(kind, stage_code, now) if active else None,
            escalated_at=now if active and initial_escalation_reason else None,
            escalated_from_user_id=original_manager_id if first_manager_was_delegated else None,
            escalation_count=1 if active and initial_escalation_reason else 0,
            escalation_reason=initial_escalation_reason,
        )
        db.session.add(step)
        steps.append(step)

    row.status = "SUBMITTED"
    row.approver_user_id = first_approver_id
    row.updated_at = now

    if effective_approver_ids:
        _notify(
            effective_approver_ids,
            f"طلب {_request_label(kind)} رقم #{row.id} للموظف {_request_employee_name(row)} بانتظار اعتمادك.",
            kind=kind,
            request_id=row.id,
        )
    else:
        _notify(
            hr_observer_user_ids() + secretary_general_user_ids(),
            f"تعذر تحديد معتمد لطلب {_request_label(kind)} رقم #{row.id} للموظف {_request_employee_name(row)}. يلزم ضبط المسؤول التنظيمي.",
            kind=kind,
            request_id=row.id,
            ntype="HR_APPROVAL_ROUTING_ERROR",
        )
    return steps


def approval_steps(kind: str, request_id: int) -> list[HRRequestApprovalStep]:
    return (
        HRRequestApprovalStep.query
        .filter_by(request_kind=(kind or "").upper(), request_id=int(request_id))
        .order_by(HRRequestApprovalStep.step_order.asc())
        .all()
    )


def current_step(kind: str, request_id: int) -> HRRequestApprovalStep | None:
    return (
        HRRequestApprovalStep.query
        .filter_by(request_kind=(kind or "").upper(), request_id=int(request_id), status="PENDING")
        .order_by(HRRequestApprovalStep.step_order.asc())
        .first()
    )


def can_user_act(user: User, step: HRRequestApprovalStep | None, *, now: datetime | None = None) -> bool:
    if not user or not step or (step.status or "").upper() != "PENDING":
        return False
    try:
        if user.has_role("SUPER_ADMIN"):
            return True
    except Exception:
        pass
    scope = (step.approver_scope or SCOPE_USER).upper()
    if scope == SCOPE_HR:
        return _is_hr_approver(user)
    if scope == SCOPE_SECRETARY_GENERAL:
        return _is_secretary_general(user)
    for approver_user_id in _step_approver_ids(step):
        if approver_user_id == user.id:
            return True
        delegation = _active_delegation_for(approver_user_id, now)
        if delegation and delegation.to_user_id == user.id:
            return True
    return False


def _scope_approver_ids(step: HRRequestApprovalStep) -> list[int]:
    candidate_ids = _step_approver_ids(step)
    if candidate_ids:
        return candidate_ids
    if step.approver_scope == SCOPE_HR:
        return hr_observer_user_ids()
    if step.approver_scope == SCOPE_SECRETARY_GENERAL:
        return secretary_general_user_ids()
    return []


def _activate_next_step(kind: str, row, step: HRRequestApprovalStep, now: datetime) -> HRRequestApprovalStep | None:
    next_step = (
        HRRequestApprovalStep.query
        .filter(
            HRRequestApprovalStep.request_kind == kind,
            HRRequestApprovalStep.request_id == row.id,
            HRRequestApprovalStep.step_order > step.step_order,
            HRRequestApprovalStep.status == "WAITING",
        )
        .order_by(HRRequestApprovalStep.step_order.asc())
        .first()
    )
    if not next_step:
        return None
    next_step.status = "PENDING"
    next_step.assigned_at = now
    next_step.due_at = _stage_due_at(kind, next_step.stage_code, now)
    row.approver_user_id = next_step.approver_user_id
    recipients = _scope_approver_ids(next_step)
    if recipients:
        _notify(
            recipients,
            f"طلب {_request_label(kind)} رقم #{row.id} للموظف {_request_employee_name(row)} وصل إلى مرحلة {stage_label(next_step.stage_code)}.",
            kind=kind,
            request_id=row.id,
        )
    else:
        _notify(
            hr_observer_user_ids(),
            f"تعذر تحديد معتمد لطلب {_request_label(kind)} رقم #{row.id} في مرحلة {stage_label(next_step.stage_code)}. يلزم ضبط الصلاحيات التنظيمية.",
            kind=kind,
            request_id=row.id,
            ntype="HR_APPROVAL_ROUTING_ERROR",
        )
    return next_step


def stage_label(stage_code: str | None) -> str:
    return {
        STAGE_DIRECT_MANAGER: "المسؤولون على الهيكلية",
        STAGE_HR: "الموارد البشرية",
        STAGE_SECRETARY_GENERAL: "الأمين العام",
    }.get((stage_code or "").upper(), stage_code or "-")


def _observer_groups(kind: str, row) -> dict[str, set[int]]:
    if kind == KIND_PERMISSION:
        return {
            "DIRECT_MANAGER": {
                int(manager.id)
                for manager in resolve_responsible_managers(int(row.user_id))
            },
            "HR": set(hr_notification_user_ids()),
        }

    groups: dict[str, set[int]] = {
        "HR": set(hr_observer_user_ids()),
        "GENERAL_DIRECTOR": set(),
        "SECRETARY_GENERAL": set(secretary_general_user_ids()),
        "SECRETARIAT": set(),
    }
    general_director = resolve_general_director(int(row.user_id))
    if general_director:
        groups["GENERAL_DIRECTOR"].add(int(general_director.id))
    for user in User.query.all():
        if _is_secretariat(user):
            groups["SECRETARIAT"].add(int(user.id))
    return groups


def _record_final_observers(kind: str, row, now: datetime) -> None:
    seen: set[int] = set()
    cc_ids: set[int] = set()
    for scope, user_ids in _observer_groups(kind, row).items():
        if scope == "HR" or (kind == KIND_PERMISSION and scope == "DIRECT_MANAGER"):
            cc_ids.update(user_ids)
        for user_id in sorted(user_ids):
            if user_id in seen:
                continue
            seen.add(user_id)
            observer = HRRequestObserver.query.filter_by(
                request_kind=kind,
                request_id=row.id,
                user_id=user_id,
            ).first()
            if not observer:
                observer = HRRequestObserver(
                    request_kind=kind,
                    request_id=row.id,
                    user_id=user_id,
                    observer_scope=scope,
                    created_at=now,
                )
                db.session.add(observer)
            observer.notified_at = now
    _notify(
        cc_ids,
        f"للاطلاع: تم اعتماد طلب {_request_label(kind)} رقم #{row.id} للموظف {_request_employee_name(row)}.",
        kind=kind,
        request_id=row.id,
        ntype="HR_REQUEST_CC",
    )


def decide_request(kind: str, row, actor: User, action: str, note: str | None = None, *, now: datetime | None = None) -> str:
    kind = (kind or "").upper()
    action = (action or "").upper()
    now = now or datetime.utcnow()
    step = current_step(kind, row.id)
    if action not in {"APPROVE", "REJECT"}:
        raise ValueError("INVALID_ACTION")
    if not can_user_act(actor, step, now=now):
        raise PermissionError("NOT_CURRENT_APPROVER")
    if (row.status or "").upper() != "SUBMITTED":
        raise ValueError("REQUEST_NOT_PENDING")

    step.status = "APPROVED" if action == "APPROVE" else "REJECTED"
    step.decided_at = now
    step.decided_by_id = actor.id
    step.decision_note = (note or "").strip() or None
    step.updated_at = now

    if action == "REJECT":
        for waiting in approval_steps(kind, row.id):
            if waiting.status == "WAITING":
                waiting.status = "SKIPPED"
        row.status = "REJECTED"
        row.decided_at = now
        row.decided_by_id = actor.id
        row.decision_note = step.decision_note
        row.approver_user_id = None
        row.updated_at = now
        _notify(
            [row.user_id],
            f"تم رفض طلب {_request_label(kind)} رقم #{row.id} في مرحلة {stage_label(step.stage_code)}.",
            kind=kind,
            request_id=row.id,
        )
        result = "REJECTED"
    else:
        next_step = _activate_next_step(kind, row, step, now)
        if next_step:
            row.updated_at = now
            result = "NEXT"
        else:
            row.status = "APPROVED"
            row.decided_at = now
            row.decided_by_id = actor.id
            row.decision_note = step.decision_note
            row.approver_user_id = None
            row.updated_at = now
            _record_final_observers(kind, row, now)
            _notify(
                [row.user_id],
                f"تم اعتماد طلب {_request_label(kind)} رقم #{row.id} اعتمادًا نهائيًا.",
                kind=kind,
                request_id=row.id,
            )
            result = "APPROVED"

    db.session.add(AuditLog(
        user_id=actor.id,
        action=f"HR_{kind}_{action}",
        old_status="SUBMITTED",
        new_status=row.status,
        target_type=f"HR_{kind}_REQUEST",
        target_id=row.id,
        note=f"stage={step.stage_code}; {step.decision_note or ''}".strip(),
        created_at=now,
    ))
    return result


def cancel_request_flow(kind: str, request_id: int, *, now: datetime | None = None) -> None:
    now = now or datetime.utcnow()
    kind = (kind or "").upper()
    for step in approval_steps(kind, request_id):
        if step.status in ACTIVE_STEP_STATUSES:
            step.status = "CANCELLED"
            step.updated_at = now
    observer_ids = [
        row.user_id
        for row in HRRequestObserver.query.filter_by(
            request_kind=kind,
            request_id=int(request_id),
            observer_scope="HR",
        ).all()
    ]
    if observer_ids:
        _notify(
            observer_ids,
            f"للاطلاع: تم إلغاء طلب {_request_label(kind)} رقم #{request_id} بعد اعتماده.",
            kind=kind,
            request_id=request_id,
            ntype="HR_REQUEST_CC",
        )


def _escalation_target(step: HRRequestApprovalStep, row, now: datetime) -> tuple[User | None, str | None]:
    current_id = step.approver_user_id
    if current_id:
        delegation = _active_delegation_for(current_id, now)
        if delegation and delegation.to_user_id != current_id:
            return delegation.to_user, "ACTIVE_DELEGATION"
        deputy = _deputy_for_manager(current_id, row.user_id)
        if deputy:
            return deputy, "DEPUTY"
    excluded = {current_id, step.escalated_from_user_id}
    general_director = resolve_general_director(row.user_id, exclude_ids=excluded)
    if general_director:
        return general_director, "GENERAL_DIRECTOR"
    for user_id in secretary_general_user_ids():
        if user_id not in excluded:
            return db.session.get(User, user_id), "SECRETARY_GENERAL_FALLBACK"
    return None, None


def _initialize_legacy_pending_flows(now: datetime) -> int:
    """Attach the new workflow to requests submitted before this feature existed."""
    initialized = 0
    existing = {
        (str(kind).upper(), int(request_id))
        for kind, request_id in db.session.query(
            HRRequestApprovalStep.request_kind,
            HRRequestApprovalStep.request_id,
        ).distinct().all()
    }
    for kind, model in ((KIND_LEAVE, HRLeaveRequest), (KIND_PERMISSION, HRPermissionRequest)):
        for row in model.query.filter(func.upper(model.status) == "SUBMITTED").all():
            key = (kind, int(row.id))
            if key in existing:
                continue
            submitted_at = getattr(row, "submitted_at", None)
            created_at = getattr(row, "created_at", None)
            assigned_at = submitted_at if isinstance(submitted_at, datetime) else created_at
            start_request_flow(kind, row, now=assigned_at if isinstance(assigned_at, datetime) else now)
            existing.add(key)
            initialized += 1
    return initialized


def process_pending_approvals(*, now: datetime | None = None, send_notifications: bool = True) -> dict[str, int]:
    """Send reminders and reassign overdue manager steps without auto-approval."""
    now = now or datetime.utcnow()
    initialized = _initialize_legacy_pending_flows(now)
    reminded = 0
    escalated = 0
    unresolved = 0
    pending_steps = HRRequestApprovalStep.query.filter_by(status="PENDING").order_by(HRRequestApprovalStep.id.asc()).all()

    for step in pending_steps:
        row = _request(step.request_kind, step.request_id)
        if not row or (row.status or "").upper() != "SUBMITTED":
            step.status = "CANCELLED"
            continue

        reminder_at = _stage_reminder_at(step)
        cooldown_ok = not step.reminder_sent_at or (now - step.reminder_sent_at) >= timedelta(hours=24)
        if now >= reminder_at and cooldown_ok:
            if send_notifications:
                _notify(
                    _scope_approver_ids(step),
                    f"تذكير: طلب {_request_label(step.request_kind)} رقم #{row.id} للموظف {_request_employee_name(row)} ما زال بانتظار قرارك.",
                    kind=step.request_kind,
                    request_id=row.id,
                    ntype="HR_APPROVAL_REMINDER",
                )
            step.reminder_sent_at = now
            step.reminder_count = int(step.reminder_count or 0) + 1
            if step.request_kind == KIND_LEAVE:
                row.reminder_sent_at = now
                row.reminder_count = int(row.reminder_count or 0) + 1
            reminded += 1

        if step.stage_code != STAGE_DIRECT_MANAGER or not step.due_at or now < step.due_at:
            continue

        candidate_ids = _step_approver_ids(step)
        previous_candidate_ids = list(candidate_ids)
        target, reason = _escalation_target(step, row, now)
        if len(candidate_ids) > 1 and (not target or target.id in candidate_ids):
            # The request is already available to every configured hierarchy
            # manager. Keep the shared stage open instead of collapsing it to
            # one person or reporting a routing error.
            step.due_at = add_working_days(now, 1)
            continue
        if target and target.id not in candidate_ids:
            old_id = step.approver_user_id
            if len(candidate_ids) > 1:
                # Parallel hierarchy approvers remain eligible; escalation
                # only adds another responsible person to the same stage.
                candidate_ids.append(int(target.id))
                step.approver_user_ids = _serialize_approver_ids(candidate_ids)
            else:
                step.approver_user_id = target.id
                step.approver_user_ids = _serialize_approver_ids([target.id])
                row.approver_user_id = target.id
            step.escalated_from_user_id = old_id
            step.escalated_at = now
            step.escalation_count = int(step.escalation_count or 0) + 1
            step.escalation_reason = reason
            step.assigned_at = now
            step.due_at = _stage_due_at(step.request_kind, step.stage_code, now)
            step.reminder_sent_at = None
            row.updated_at = now
            if send_notifications:
                _notify(
                    [target.id],
                    f"تم تصعيد طلب {_request_label(step.request_kind)} رقم #{row.id} إليك لعدم اتخاذ إجراء ضمن المهلة. الاعتماد ليس تلقائيًا.",
                    kind=step.request_kind,
                    request_id=row.id,
                    ntype="HR_APPROVAL_ESCALATED",
                )
                _notify(
                    previous_candidate_ids,
                    f"تم تصعيد طلب {_request_label(step.request_kind)} رقم #{row.id} بعد انتهاء مهلة الاعتماد.",
                    kind=step.request_kind,
                    request_id=row.id,
                    ntype="HR_APPROVAL_ESCALATED",
                )
            escalated += 1
        else:
            # Keep it pending and raise a routing alert; never approve it.
            step.due_at = add_working_days(now, 1)
            if send_notifications:
                _notify(
                    hr_observer_user_ids() + secretary_general_user_ids(),
                    f"تعذر تصعيد طلب {_request_label(step.request_kind)} رقم #{row.id}: لا يوجد نائب أو مسؤول أعلى مضبوط.",
                    kind=step.request_kind,
                    request_id=row.id,
                    ntype="HR_APPROVAL_ROUTING_ERROR",
                )
            unresolved += 1

    return {
        "initialized": initialized,
        "reminded": reminded,
        "escalated": escalated,
        "unresolved": unresolved,
    }


def request_ids_user_can_act_on(user: User, kind: str) -> list[int]:
    ids: list[int] = []
    for step in HRRequestApprovalStep.query.filter_by(request_kind=kind, status="PENDING").all():
        if can_user_act(user, step):
            ids.append(int(step.request_id))
    return sorted(set(ids))


def can_view_request(user: User, kind: str, request_id: int) -> bool:
    if current := current_step(kind, request_id):
        if can_user_act(user, current):
            return True
    if HRRequestObserver.query.filter_by(request_kind=kind, request_id=request_id, user_id=user.id).first():
        return True
    for step in approval_steps(kind, request_id):
        if (
            int(user.id) in _step_approver_ids(step)
            or step.decided_by_id == user.id
            or step.escalated_from_user_id == user.id
        ):
            return True
    try:
        return bool(user.has_perm("HR_REQUESTS_VIEW_ALL") or user.has_role("SUPER_ADMIN"))
    except Exception:
        return False


def can_view_absence_board(user: User) -> bool:
    if not user:
        return False
    if _is_hr_approver(user) or _is_secretariat(user):
        return True
    try:
        if user.has_perm("HR_ABSENCE_BOARD_VIEW") or user.has_perm("HR_REQUESTS_VIEW_ALL"):
            return True
    except Exception:
        pass
    dynamic_general_manager = (
        OrgNodeManager.query
        .join(OrgNode, OrgNode.id == OrgNodeManager.node_id)
        .join(OrgNodeType, OrgNodeType.id == OrgNode.type_id)
        .filter(
            OrgNodeManager.manager_user_id == user.id,
            func.upper(OrgNodeType.code).in_(("DIRECTORATE", "GENERAL_DIRECTOR", "SECRETARY_GENERAL")),
        )
        .first()
    )
    return bool(
        OrgUnitManager.query.filter_by(manager_user_id=user.id, unit_type="DIRECTORATE").first()
        or dynamic_general_manager
    )


def _descendant_node_ids(root_ids: Iterable[int]) -> set[int]:
    all_ids = {int(value) for value in root_ids if value}
    frontier = set(all_ids)
    while frontier:
        children = {int(row.id) for row in OrgNode.query.filter(OrgNode.parent_id.in_(frontier)).all()}
        children -= all_ids
        if not children:
            break
        all_ids.update(children)
        frontier = children
    return all_ids


def board_visible_user_ids(user: User) -> set[int] | None:
    """Return None for global visibility, otherwise the manager's org scope."""
    if _is_hr_approver(user) or _is_secretariat(user):
        return None
    try:
        if user.has_perm("HR_REQUESTS_VIEW_ALL"):
            return None
    except Exception:
        pass

    visible: set[int] = set()
    directorate_ids = {
        int(row.unit_id)
        for row in OrgUnitManager.query.filter_by(manager_user_id=user.id, unit_type="DIRECTORATE").all()
    }
    if directorate_ids:
        department_ids = [row.id for row in Department.query.filter(Department.directorate_id.in_(directorate_ids)).all()]
        for row in User.query.filter(or_(User.directorate_id.in_(directorate_ids), User.department_id.in_(department_ids))).all():
            visible.add(int(row.id))
        for row in EmployeeFile.query.filter(or_(EmployeeFile.directorate_id.in_(directorate_ids), EmployeeFile.department_id.in_(department_ids))).all():
            visible.add(int(row.user_id))

    node_ids = [
        row.node_id
        for row in (
            OrgNodeManager.query
            .join(OrgNode, OrgNode.id == OrgNodeManager.node_id)
            .join(OrgNodeType, OrgNodeType.id == OrgNode.type_id)
            .filter(
                OrgNodeManager.manager_user_id == user.id,
                func.upper(OrgNodeType.code).in_(("DIRECTORATE", "GENERAL_DIRECTOR", "SECRETARY_GENERAL")),
            )
            .all()
        )
    ]
    if node_ids:
        descendants = _descendant_node_ids(node_ids)
        for row in OrgNodeAssignment.query.filter(OrgNodeAssignment.node_id.in_(descendants)).all():
            visible.add(int(row.user_id))

    if not visible:
        observer_rows = HRRequestObserver.query.filter_by(user_id=user.id).all()
        for observer in observer_rows:
            row = _request(observer.request_kind, observer.request_id)
            if row:
                visible.add(int(row.user_id))
    return visible
