"""Read models for the employee workflow report."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import OperationalError, ProgrammingError

from extensions import db
from models import (
    Department,
    Notification,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowRequestView,
    WorkflowStepTask,
)
from services.workflow_confidentiality import can_user_pass_confidential_workflow_gate
from workflow.engine import resolve_step_approver_user_ids


WORKFLOW_REPORT_PERMISSION = "WORKFLOW_REPORTS_READ"
REPORT_STATUS_OPTIONS = (
    "IN_PROGRESS",
    "APPROVED",
    "REJECTED",
    "CLOSED",
    "DRAFT",
    "CANCELLED",
)


def _id_chunks(values, size: int = 400):
    normalized = sorted({int(value) for value in values if value})
    for offset in range(0, len(normalized), size):
        yield normalized[offset:offset + size]


def _text(value) -> str:
    return str(value or "").strip().casefold()


def _is_mention_task(task: WorkflowStepTask) -> bool:
    note = (getattr(task, "note", None) or "").strip()
    return note == "MENTION_TASK" or note.startswith("MENTION_TASK") or note == "تمت الإضافة عبر المنشن"


def _step_activation_at(
    instance: WorkflowInstance,
    steps_by_instance: dict[int, list[WorkflowInstanceStep]],
    step: WorkflowInstanceStep,
    request: WorkflowRequest,
) -> datetime | None:
    """Best-effort activation time for sequential runtime steps.

    Newer assignments have a task-assignment notification or a concrete
    parallel task timestamp.  Older sequential rows do not have a dedicated
    assigned_at column, so use the previous decision, the frozen SLA start,
    or the workflow start as a safe historical fallback.
    """
    previous_steps = [
        previous
        for previous in steps_by_instance.get(int(instance.id), [])
        if int(previous.step_order or 0) < int(step.step_order or 0)
        and getattr(previous, "decided_at", None)
    ]
    if previous_steps:
        return max(previous.decided_at for previous in previous_steps)

    due_at = getattr(step, "due_at", None)
    sla_days = getattr(step, "sla_days", None)
    if due_at and sla_days and int(sla_days) > 0:
        return due_at - timedelta(days=int(sla_days))

    return (
        getattr(instance, "created_at", None)
        or getattr(request, "created_at", None)
    )


def _load_views(request_ids: set[int]) -> dict[tuple[int, int], datetime]:
    """Load last-opened timestamps, tolerating an un-migrated legacy DB."""
    result: dict[tuple[int, int], datetime] = {}
    try:
        for request_id_chunk in _id_chunks(request_ids):
            rows = (
                WorkflowRequestView.query
                .filter(WorkflowRequestView.request_id.in_(request_id_chunk))
                .all()
            )
            for row in rows:
                if row.user_id and row.request_id and row.viewed_at:
                    result[(int(row.request_id), int(row.user_id))] = row.viewed_at
    except (OperationalError, ProgrammingError):
        # The migration is already part of the source tree, but this keeps the
        # report usable while an older deployment is being upgraded.
        db.session.rollback()
    return result


def _load_assignment_notifications(
    request_ids: set[int],
) -> dict[tuple[int, int], datetime]:
    """Return the latest task-assignment notification per employee/path."""
    result: dict[tuple[int, int], datetime] = {}
    try:
        for request_id_chunk in _id_chunks(request_ids):
            rows = (
                db.session.query(
                    Notification.user_id,
                    Notification.target_id,
                    func.max(Notification.created_at),
                )
                .filter(
                    Notification.type == "WORKFLOW",
                    Notification.source == "workflow",
                    Notification.is_mirror.is_(False),
                    Notification.target_type == "WorkflowRequest",
                    Notification.target_id.in_(request_id_chunk),
                    Notification.email_delivery_mode == "TASK_ASSIGNMENT",
                )
                .group_by(Notification.user_id, Notification.target_id)
                .all()
            )
            for user_id, request_id, created_at in rows:
                if user_id and request_id and created_at:
                    result[(int(request_id), int(user_id))] = created_at
    except (OperationalError, ProgrammingError):
        # Assignment timestamps still fall back to runtime step/task dates.
        db.session.rollback()
    return result


def build_employee_workflow_report(
    viewer: User,
    *,
    search: str = "",
    status: str = "",
    selected_user_id: int | None = None,
    page: int = 1,
    per_page: int | None = 50,
    include_all_details: bool = False,
) -> dict:
    """Build the first Masar report: workflow assignments by employee.

    A path is associated with an employee when the employee is a candidate for
    a runtime sequential step or has a concrete parallel/mention task.  The
    summary counts each path once per employee, while ``active_paths`` only
    counts a task that is currently waiting for that employee.
    """
    search = (search or "").strip()
    normalized_status = (status or "").strip().upper()
    if normalized_status not in REPORT_STATUS_OPTIONS:
        normalized_status = ""

    requests_with_instances = (
        db.session.query(WorkflowRequest, WorkflowInstance)
        .join(WorkflowInstance, WorkflowInstance.request_id == WorkflowRequest.id)
        .order_by(WorkflowRequest.created_at.desc(), WorkflowRequest.id.desc())
        .all()
    )

    visible_rows = []
    for request_row, instance in requests_with_instances:
        if not can_user_pass_confidential_workflow_gate(viewer, request_row):
            continue
        visible_rows.append((request_row, instance))

    users = (
        User.query
        .order_by(User.name.asc(), User.username.asc(), User.email.asc(), User.id.asc())
        .all()
    )
    user_map = {int(user.id): user for user in users if user.id}
    department_map = {
        int(department.id): department
        for department in Department.query.all()
        if department.id
    }

    request_ids = {int(request_row.id) for request_row, _instance in visible_rows}
    instance_ids = {int(instance.id) for _request_row, instance in visible_rows}
    views = _load_views(request_ids)
    assignment_notifications = _load_assignment_notifications(request_ids)

    steps_by_instance: dict[int, list[WorkflowInstanceStep]] = defaultdict(list)
    for instance_id_chunk in _id_chunks(instance_ids):
        for step in (
            WorkflowInstanceStep.query
            .filter(WorkflowInstanceStep.instance_id.in_(instance_id_chunk))
            .order_by(WorkflowInstanceStep.instance_id.asc(), WorkflowInstanceStep.step_order.asc())
            .all()
        ):
            steps_by_instance[int(step.instance_id)].append(step)

    tasks_by_step: dict[tuple[int, int], list[WorkflowStepTask]] = defaultdict(list)
    for instance_id_chunk in _id_chunks(instance_ids):
        for task in (
            WorkflowStepTask.query
            .filter(WorkflowStepTask.instance_id.in_(instance_id_chunk))
            .order_by(WorkflowStepTask.created_at.asc(), WorkflowStepTask.id.asc())
            .all()
        ):
            tasks_by_step[(int(task.instance_id), int(task.step_order))].append(task)

    step_assignee_cache: dict[tuple, list[int]] = {}

    def regular_assignees(step: WorkflowInstanceStep) -> list[int]:
        signature = (
            (step.approver_kind or "").strip().upper(),
            step.approver_user_id,
            (step.approver_role or "").strip().casefold(),
            step.approver_department_id,
            step.approver_directorate_id,
            step.approver_unit_id,
            step.approver_section_id,
            step.approver_division_id,
            step.approver_org_node_id,
            step.approver_committee_id,
            (step.committee_delivery_mode or "").strip().upper(),
        )
        if signature not in step_assignee_cache:
            try:
                step_assignee_cache[signature] = resolve_step_approver_user_ids(step)
            except Exception:
                step_assignee_cache[signature] = []
        return step_assignee_cache[signature]

    associations: dict[tuple[int, int], dict] = {}
    for request_row, instance in visible_rows:
        request_id = int(request_row.id)
        current_order = int(instance.current_step_order or 0)
        request_status = (request_row.status or "UNKNOWN").strip().upper()
        for step in steps_by_instance.get(int(instance.id), []):
            step_order = int(step.step_order or 0)
            step_tasks = tasks_by_step.get((int(instance.id), step_order), [])
            activation_at = _step_activation_at(
                instance,
                steps_by_instance,
                step,
                request_row,
            )

            if (step.mode or "").strip().upper() == "PARALLEL_SYNC":
                entries = [
                    (
                        int(task.assignee_user_id),
                        getattr(task, "created_at", None) or activation_at,
                        (task.status or "").strip().upper(),
                        True,
                    )
                    for task in step_tasks
                    if task.assignee_user_id
                ]
            else:
                entries = [
                    (
                        int(user_id),
                        assignment_notifications.get((request_id, int(user_id))) or activation_at,
                        "PENDING",
                        False,
                    )
                    for user_id in regular_assignees(step)
                    if user_id
                ]
                # A mention is an explicit assignment even when the runtime
                # step itself is sequential.
                entries.extend(
                    (
                        int(task.assignee_user_id),
                        getattr(task, "created_at", None) or activation_at,
                        (task.status or "").strip().upper(),
                        True,
                    )
                    for task in step_tasks
                    if task.assignee_user_id and _is_mention_task(task)
                )

            for user_id, assignment_at, task_status, concrete_task in entries:
                if user_id not in user_map:
                    continue
                key = (user_id, request_id)
                association = associations.setdefault(
                    key,
                    {
                        "user_id": user_id,
                        "request": request_row,
                        "instance": instance,
                        "step_orders": set(),
                        "active_step_orders": set(),
                        "assignment_dates": [],
                        "opened_at": views.get((request_id, user_id)),
                        "current_step_order": None,
                    },
                )
                association["step_orders"].add(step_order)
                if assignment_at:
                    association["assignment_dates"].append(assignment_at)

                is_current_pending = bool(
                    not instance.is_completed
                    and step_order == current_order
                    and (step.status or "").strip().upper() == "PENDING"
                )
                if concrete_task:
                    is_current_pending = is_current_pending and task_status == "PENDING"
                if is_current_pending:
                    association["active_step_orders"].add(step_order)
                    association["current_step_order"] = step_order

    def association_text(association: dict) -> str:
        request_row = association["request"]
        requester = getattr(request_row, "requester", None)
        return " ".join(
            (
                getattr(request_row, "title", "") or "",
                getattr(request_row, "description", "") or "",
                getattr(requester, "full_name", "") if requester else "",
            )
        ).casefold()

    def user_text(user: User) -> str:
        return " ".join(
            (
                user.full_name or "",
                user.email or "",
                user.username or "",
                user.job_title or "",
                user.role or "",
                getattr(department_map.get(int(user.department_id or 0)), "name_ar", "")
                if user.department_id
                else "",
            )
        ).casefold()

    all_associations = list(associations.values())
    filtered_by_user: dict[int, list[dict]] = defaultdict(list)
    for association in all_associations:
        request_status = (association["request"].status or "UNKNOWN").strip().upper()
        if normalized_status and request_status != normalized_status:
            continue
        user = user_map.get(int(association["user_id"]))
        if not user:
            continue
        employee_matches = bool(search and search.casefold() in user_text(user))
        path_matches = bool(search and search.casefold() in association_text(association))
        if search and not employee_matches and not path_matches:
            continue
        filtered_by_user[int(user.id)].append(association)

    summary_rows = []
    for user in users:
        user_associations = filtered_by_user.get(int(user.id), [])
        employee_matches = bool(search and search.casefold() in user_text(user))
        if search and not employee_matches and not user_associations:
            continue
        if normalized_status and not user_associations:
            continue

        status_counts: dict[str, int] = defaultdict(int)
        last_opened_at = None
        last_assigned_at = None
        active_paths = 0
        for association in user_associations:
            request_status = (association["request"].status or "UNKNOWN").strip().upper()
            status_counts[request_status] += 1
            if association["active_step_orders"]:
                active_paths += 1
            opened_at = association.get("opened_at")
            if opened_at and (last_opened_at is None or opened_at > last_opened_at):
                last_opened_at = opened_at
            assignment_dates = association.get("assignment_dates") or []
            if assignment_dates:
                latest_assignment = max(assignment_dates)
                if last_assigned_at is None or latest_assignment > last_assigned_at:
                    last_assigned_at = latest_assignment

        summary_rows.append({
            "user": user,
            "total_paths": len(user_associations),
            "active_paths": active_paths,
            "status_counts": dict(status_counts),
            "last_opened_at": last_opened_at,
            "last_assigned_at": last_assigned_at,
            "department": department_map.get(int(user.department_id or 0)),
        })

    summary_rows.sort(
        key=lambda row: (
            -int(row["active_paths"] or 0),
            -int(row["total_paths"] or 0),
            _text(row["user"].full_name),
            int(row["user"].id),
        )
    )

    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    total_employees = len(summary_rows)
    if per_page is None:
        page = 1
        pages = 1
        page_rows = summary_rows
    else:
        try:
            per_page = max(10, min(int(per_page or 50), 100))
        except (TypeError, ValueError):
            per_page = 50
        pages = max(1, (total_employees + per_page - 1) // per_page)
        page = min(page, pages)
        page_rows = summary_rows[(page - 1) * per_page:page * per_page]

    def detail_row(association: dict) -> dict:
        detail = dict(association)
        detail["user"] = user_map.get(int(association["user_id"]))
        assignment_dates = association.get("assignment_dates") or []
        detail["assignment_at"] = max(assignment_dates) if assignment_dates else None
        detail["step_orders_label"] = ", ".join(
            str(step_order)
            for step_order in sorted(association.get("step_orders") or [])
        )
        detail["active"] = bool(association.get("active_step_orders"))
        return detail

    selected_user = user_map.get(int(selected_user_id or 0)) if selected_user_id else None
    selected_details = []
    if selected_user:
        selected_details = [
            detail_row(association)
            for association in filtered_by_user.get(int(selected_user.id), [])
        ]
        selected_details.sort(
            key=lambda association: (
                not association["active"],
                -(
                    association["assignment_at"].timestamp()
                    if association.get("assignment_at")
                    else 0
                ),
                -int(association["request"].id or 0),
            )
        )

    report_details = []
    if include_all_details:
        report_details = [
            detail_row(association)
            for user_associations in filtered_by_user.values()
            for association in user_associations
        ]
        report_details.sort(
            key=lambda association: (
                _text(user_map[int(association["user_id"])].full_name),
                not association["active"],
                -(
                    association["assignment_at"].timestamp()
                    if association.get("assignment_at")
                    else 0
                ),
                -int(association["request"].id or 0),
            )
        )

    filtered_associations = [
        association
        for user_associations in filtered_by_user.values()
        for association in user_associations
    ]
    status_totals: dict[str, int] = defaultdict(int)
    for association in filtered_associations:
        request_status = (association["request"].status or "UNKNOWN").strip().upper()
        status_totals[request_status] += 1

    return {
        "summary_rows": page_rows,
        "selected_user": selected_user,
        "selected_details": selected_details,
        "report_details": report_details,
        "status_options": tuple(dict.fromkeys((*REPORT_STATUS_OPTIONS, *status_totals.keys()))),
        "status_totals": dict(status_totals),
        "total_employees": sum(1 for row in summary_rows if row["total_paths"]),
        "total_paths": len({int(association["request"].id) for association in filtered_associations}),
        "total_assignments": len(filtered_associations),
        "active_assignments": sum(
            1 for association in filtered_associations if association["active_step_orders"]
        ),
        "opened_assignments": sum(
            1 for association in filtered_associations if association.get("opened_at")
        ),
        "page": page,
        "pages": pages,
        "total_summary_rows": total_employees,
    }
