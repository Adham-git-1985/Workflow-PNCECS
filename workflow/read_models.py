"""Set-based read models for high-traffic Workflow screens."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from sqlalchemy import func, or_

from extensions import db
from models import (
    AuditLog,
    CommitteeAssignee,
    Department,
    InboundMail,
    OrgNodeManager,
    OrgUnitManager,
    OutboundMail,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowStepTask,
)


_IN_QUERY_CHUNK_SIZE = 400


def _id_chunks(values: Iterable[int], size: int = _IN_QUERY_CHUNK_SIZE):
    normalized = sorted({int(value) for value in values if value})
    for offset in range(0, len(normalized), size):
        yield normalized[offset:offset + size]


def _role_variants(role: str | None) -> set[str]:
    raw = (role or "").strip()
    if not raw:
        return set()
    normalized = raw.upper().replace("-", "_").replace(" ", "_")
    variants = {
        raw,
        raw.lower(),
        raw.upper(),
        normalized,
        normalized.replace("_", " "),
        normalized.replace("_", "-"),
        normalized.replace("_", ""),
    }
    return {value.strip().casefold() for value in variants if value.strip()}


def _is_mention_note(note: str | None, notes: frozenset[str], prefix: str) -> bool:
    value = (note or "").strip()
    return bool(value and (value in notes or value.startswith(prefix)))


@dataclass
class WorkflowAccessSnapshot:
    """All workflow participation state needed by one list-page request."""

    instances_by_request: dict[int, WorkflowInstance] = field(default_factory=dict)
    steps_by_instance: dict[int, list[WorkflowInstanceStep]] = field(default_factory=dict)
    current_steps_by_instance: dict[int, WorkflowInstanceStep] = field(default_factory=dict)
    pending_task_users_by_step: dict[tuple[int, int], set[int]] = field(default_factory=dict)
    pending_mention_users_by_instance: dict[int, set[int]] = field(default_factory=dict)
    task_participants_by_instance: dict[int, set[int]] = field(default_factory=dict)
    responded_task_users_by_instance: dict[int, set[int]] = field(default_factory=dict)
    decided_users_by_instance: dict[int, set[int]] = field(default_factory=dict)
    retained_followers_by_request: dict[int, set[int]] = field(default_factory=dict)
    active_mentions_by_request: dict[int, set[int]] = field(default_factory=dict)
    org_unit_manager_users: dict[tuple[str, int], set[int]] = field(default_factory=dict)
    org_node_manager_users: dict[int, set[int]] = field(default_factory=dict)
    committee_assignees: dict[int, list[tuple[str, int | None, str | None, str | None]]] = field(
        default_factory=dict
    )
    actor_directorates: dict[int, int | None] = field(default_factory=dict)

    def instance_for(self, request_id: int | None) -> WorkflowInstance | None:
        return self.instances_by_request.get(int(request_id or 0))

    def current_step_for(self, instance: WorkflowInstance | None) -> WorkflowInstanceStep | None:
        if not instance:
            return None
        return self.current_steps_by_instance.get(int(instance.id))

    def pending_mentions(self, instance_id: int | None) -> set[int]:
        return self.pending_mention_users_by_instance.get(int(instance_id or 0), set())

    def can_act(self, user, step: WorkflowInstanceStep | None) -> bool:
        if not user or not step:
            return False
        user_id = int(getattr(user, "id", 0) or 0)
        if not user_id:
            return False

        if (getattr(step, "mode", "") or "").strip().upper() == "PARALLEL_SYNC":
            return user_id in self.pending_task_users_by_step.get(
                (int(step.instance_id), int(step.step_order)),
                set(),
            )

        role = (getattr(user, "role", "") or "").strip()
        if role.upper() in {"ADMIN", "SUPER_ADMIN"}:
            return True

        kind = (getattr(step, "approver_kind", "") or "").strip().upper()
        if kind == "USER" and step.approver_user_id:
            return user_id == int(step.approver_user_id)
        if kind == "ROLE" and step.approver_role:
            return role.casefold() == (step.approver_role or "").strip().casefold()

        target_by_kind = {
            "DEPARTMENT": getattr(step, "approver_department_id", None),
            "DIRECTORATE": getattr(step, "approver_directorate_id", None),
            "UNIT": getattr(step, "approver_unit_id", None),
            "SECTION": getattr(step, "approver_section_id", None),
            "DIVISION": getattr(step, "approver_division_id", None),
        }
        target_id = target_by_kind.get(kind)
        if target_id:
            manager_ids = self.org_unit_manager_users.get((kind, int(target_id)), set())
            if user_id in manager_ids:
                return True
            if kind == "DEPARTMENT":
                return bool(
                    int(getattr(user, "department_id", 0) or 0) == int(target_id)
                    and role.casefold() == "dept_head"
                )
            if kind == "DIRECTORATE":
                return bool(
                    self.actor_directorates.get(user_id) == int(target_id)
                    and role.casefold() in {"directorate_head", "directorate_deputy"}
                )
            return False

        if kind == "ORG_NODE" and step.approver_org_node_id:
            return user_id in self.org_node_manager_users.get(
                int(step.approver_org_node_id),
                set(),
            )

        if kind != "COMMITTEE" or not step.approver_committee_id:
            return False

        delivery_mode = (step.committee_delivery_mode or "Committee_ALL").strip().upper()
        role_matches = _role_variants(role)
        for assignee_kind, assignee_user_id, assignee_role, member_role in self.committee_assignees.get(
            int(step.approver_committee_id),
            [],
        ):
            is_match = (
                assignee_kind == "USER" and int(assignee_user_id or 0) == user_id
            ) or (
                assignee_kind == "ROLE"
                and (assignee_role or "").strip().casefold() in role_matches
            )
            if not is_match:
                continue
            normalized_member_role = (member_role or "").strip().upper()
            if "CHAIR" in delivery_mode and normalized_member_role != "CHAIR":
                continue
            if "SECRETARY" in delivery_mode and normalized_member_role != "SECRETARY":
                continue
            if "MEMBERS" in delivery_mode and normalized_member_role == "CHAIR":
                continue
            return True
        return False

    def can_view(self, user, req: WorkflowRequest, *, is_super_admin: bool = False) -> bool:
        user_id = int(getattr(user, "id", 0) or 0)
        if not user_id:
            return False
        if int(getattr(req, "requester_id", 0) or 0) == user_id:
            return True
        if is_super_admin:
            return True

        instance = self.instance_for(req.id)
        if not instance:
            return False
        instance_id = int(instance.id)
        if any(self.can_act(user, step) for step in self.steps_by_instance.get(instance_id, [])):
            return True
        if user_id in self.decided_users_by_instance.get(instance_id, set()):
            return True
        if user_id in self.retained_followers_by_request.get(int(req.id), set()):
            return True
        if user_id in self.task_participants_by_instance.get(instance_id, set()):
            return True
        return user_id in self.active_mentions_by_request.get(int(req.id), set())

    def follower_ids(self, request_id: int | None) -> set[int]:
        instance = self.instance_for(request_id)
        if not instance:
            return set()
        instance_id = int(instance.id)
        return set().union(
            self.decided_users_by_instance.get(instance_id, set()),
            self.responded_task_users_by_instance.get(instance_id, set()),
            self.retained_followers_by_request.get(int(request_id or 0), set()),
            self.active_mentions_by_request.get(int(request_id or 0), set()),
        )


def load_workflow_access_snapshot(
    requests: Iterable[WorkflowRequest],
    actor_users: Iterable,
    *,
    mention_access_action: str,
    mention_access_revoked_action: str,
    retained_follower_actions: Iterable[str],
    mention_task_notes: Iterable[str],
    mention_task_prefix: str,
) -> WorkflowAccessSnapshot:
    """Load list-page workflow access state with a bounded number of queries."""
    request_rows = list(requests or [])
    request_ids = {int(row.id) for row in request_rows if getattr(row, "id", None)}
    snapshot = WorkflowAccessSnapshot()
    if not request_ids:
        return snapshot

    instances: list[WorkflowInstance] = []
    for request_id_chunk in _id_chunks(request_ids):
        instances.extend(
            WorkflowInstance.query
            .filter(WorkflowInstance.request_id.in_(request_id_chunk))
            .all()
        )
    snapshot.instances_by_request = {
        int(instance.request_id): instance for instance in instances
    }
    instance_ids = {int(instance.id) for instance in instances}
    if not instance_ids:
        return snapshot

    steps: list[WorkflowInstanceStep] = []
    for instance_id_chunk in _id_chunks(instance_ids):
        steps.extend(
            WorkflowInstanceStep.query
            .filter(WorkflowInstanceStep.instance_id.in_(instance_id_chunk))
            .order_by(
                WorkflowInstanceStep.instance_id.asc(),
                WorkflowInstanceStep.step_order.asc(),
            )
            .all()
        )

    steps_by_instance: dict[int, list[WorkflowInstanceStep]] = defaultdict(list)
    decided_users_by_instance: dict[int, set[int]] = defaultdict(set)
    instance_by_id = {int(instance.id): instance for instance in instances}
    for step in steps:
        instance_id = int(step.instance_id)
        steps_by_instance[instance_id].append(step)
        if step.decided_by_id:
            decided_users_by_instance[instance_id].add(int(step.decided_by_id))
        instance = instance_by_id.get(instance_id)
        if instance and int(step.step_order) == int(instance.current_step_order or 0):
            snapshot.current_steps_by_instance[instance_id] = step
    snapshot.steps_by_instance = dict(steps_by_instance)
    snapshot.decided_users_by_instance = dict(decided_users_by_instance)

    task_rows = []
    for instance_id_chunk in _id_chunks(instance_ids):
        task_rows.extend(
            db.session.query(
                WorkflowStepTask.instance_id,
                WorkflowStepTask.step_order,
                WorkflowStepTask.assignee_user_id,
                WorkflowStepTask.status,
                WorkflowStepTask.note,
            )
            .filter(WorkflowStepTask.instance_id.in_(instance_id_chunk))
            .all()
        )

    pending_task_users_by_step: dict[tuple[int, int], set[int]] = defaultdict(set)
    pending_mentions_by_instance: dict[int, set[int]] = defaultdict(set)
    task_participants_by_instance: dict[int, set[int]] = defaultdict(set)
    responded_task_users_by_instance: dict[int, set[int]] = defaultdict(set)
    normalized_mention_notes = frozenset(
        (value or "").strip() for value in mention_task_notes if (value or "").strip()
    )
    for task_row in task_rows:
        instance_id = int(task_row.instance_id)
        step_order = int(task_row.step_order)
        user_id = int(task_row.assignee_user_id)
        status = (task_row.status or "").strip().upper()
        is_mention = _is_mention_note(
            task_row.note,
            normalized_mention_notes,
            mention_task_prefix,
        )
        if status == "PENDING":
            pending_task_users_by_step[(instance_id, step_order)].add(user_id)
            if is_mention:
                pending_mentions_by_instance[instance_id].add(user_id)
        if not is_mention:
            task_participants_by_instance[instance_id].add(user_id)
            if status in {"RESPONDED", "BYPASSED"}:
                responded_task_users_by_instance[instance_id].add(user_id)
    snapshot.pending_task_users_by_step = dict(pending_task_users_by_step)
    snapshot.pending_mention_users_by_instance = dict(pending_mentions_by_instance)
    snapshot.task_participants_by_instance = dict(task_participants_by_instance)
    snapshot.responded_task_users_by_instance = dict(responded_task_users_by_instance)

    relevant_actions = {
        mention_access_action,
        mention_access_revoked_action,
        *(str(action) for action in retained_follower_actions if action),
    }
    audit_rows = []
    for request_id_chunk in _id_chunks(request_ids):
        audit_rows.extend(
            db.session.query(
                AuditLog.id,
                AuditLog.request_id,
                AuditLog.action,
                AuditLog.target_id,
                AuditLog.created_at,
            )
            .filter(
                AuditLog.request_id.in_(request_id_chunk),
                AuditLog.action.in_(relevant_actions),
                AuditLog.target_type == "USER",
                AuditLog.target_id.isnot(None),
            )
            .all()
        )
    audit_rows.sort(
        key=lambda row: (
            int(row.request_id or 0),
            row.created_at or datetime.min,
            int(row.id or 0),
        )
    )

    active_mentions: dict[int, set[int]] = defaultdict(set)
    retained_followers: dict[int, set[int]] = defaultdict(set)
    retained_action_set = {str(action) for action in retained_follower_actions if action}
    for audit_row in audit_rows:
        request_id = int(audit_row.request_id)
        user_id = int(audit_row.target_id)
        if audit_row.action == mention_access_action:
            active_mentions[request_id].add(user_id)
        elif audit_row.action == mention_access_revoked_action:
            active_mentions[request_id].discard(user_id)
        elif audit_row.action in retained_action_set:
            retained_followers[request_id].add(user_id)
    snapshot.active_mentions_by_request = dict(active_mentions)
    snapshot.retained_followers_by_request = dict(retained_followers)

    target_ids_by_kind: dict[str, set[int]] = defaultdict(set)
    org_node_ids: set[int] = set()
    committee_ids: set[int] = set()
    for step in steps:
        for kind, value in (
            ("DEPARTMENT", step.approver_department_id),
            ("DIRECTORATE", step.approver_directorate_id),
            ("UNIT", step.approver_unit_id),
            ("SECTION", step.approver_section_id),
            ("DIVISION", step.approver_division_id),
        ):
            if value:
                target_ids_by_kind[kind].add(int(value))
        if step.approver_org_node_id:
            org_node_ids.add(int(step.approver_org_node_id))
        if step.approver_committee_id:
            committee_ids.add(int(step.approver_committee_id))

    org_unit_manager_users: dict[tuple[str, int], set[int]] = defaultdict(set)
    for kind, target_ids in target_ids_by_kind.items():
        for target_id_chunk in _id_chunks(target_ids):
            manager_rows = (
                db.session.query(
                    OrgUnitManager.unit_type,
                    OrgUnitManager.unit_id,
                    OrgUnitManager.manager_user_id,
                    OrgUnitManager.deputy_user_id,
                )
                .filter(
                    func.upper(OrgUnitManager.unit_type) == kind,
                    OrgUnitManager.unit_id.in_(target_id_chunk),
                )
                .all()
            )
            for manager_row in manager_rows:
                key = (kind, int(manager_row.unit_id))
                org_unit_manager_users[key].update(
                    int(user_id)
                    for user_id in (manager_row.manager_user_id, manager_row.deputy_user_id)
                    if user_id
                )
    snapshot.org_unit_manager_users = dict(org_unit_manager_users)

    org_node_manager_users: dict[int, set[int]] = defaultdict(set)
    for org_node_id_chunk in _id_chunks(org_node_ids):
        manager_rows = (
            db.session.query(
                OrgNodeManager.node_id,
                OrgNodeManager.manager_user_id,
                OrgNodeManager.deputy_user_id,
            )
            .filter(OrgNodeManager.node_id.in_(org_node_id_chunk))
            .all()
        )
        for manager_row in manager_rows:
            org_node_manager_users[int(manager_row.node_id)].update(
                int(user_id)
                for user_id in (manager_row.manager_user_id, manager_row.deputy_user_id)
                if user_id
            )
    snapshot.org_node_manager_users = dict(org_node_manager_users)

    committee_assignees: dict[int, list[tuple[str, int | None, str | None, str | None]]] = defaultdict(list)
    for committee_id_chunk in _id_chunks(committee_ids):
        assignee_rows = (
            db.session.query(
                CommitteeAssignee.committee_id,
                CommitteeAssignee.kind,
                CommitteeAssignee.user_id,
                CommitteeAssignee.role,
                CommitteeAssignee.member_role,
            )
            .filter(
                CommitteeAssignee.committee_id.in_(committee_id_chunk),
                CommitteeAssignee.is_active.is_(True),
            )
            .all()
        )
        for assignee_row in assignee_rows:
            committee_assignees[int(assignee_row.committee_id)].append((
                (assignee_row.kind or "").strip().upper(),
                int(assignee_row.user_id) if assignee_row.user_id else None,
                assignee_row.role,
                assignee_row.member_role,
            ))
    snapshot.committee_assignees = dict(committee_assignees)

    actor_rows = [user for user in actor_users or [] if getattr(user, "id", None)]
    actor_department_ids = {
        int(user.department_id) for user in actor_rows if getattr(user, "department_id", None)
    }
    department_directorates = {}
    for department_id_chunk in _id_chunks(actor_department_ids):
        department_directorates.update({
            int(department_id): int(directorate_id) if directorate_id else None
            for department_id, directorate_id in (
                db.session.query(Department.id, Department.directorate_id)
                .filter(Department.id.in_(department_id_chunk))
                .all()
            )
        })
    snapshot.actor_directorates = {
        int(user.id): (
            int(user.directorate_id)
            if getattr(user, "directorate_id", None)
            else department_directorates.get(int(getattr(user, "department_id", 0) or 0))
        )
        for user in actor_rows
    }
    return snapshot


def load_dashboard_correspondence_contexts(
    requests: Iterable[WorkflowRequest],
) -> dict[int, dict]:
    """Load the compact correspondence badges needed by the dashboard."""
    request_rows = list(requests or [])
    inbound_ids = {
        int(row.source_corr_id)
        for row in request_rows
        if row.source_corr_id and (row.source_corr_kind or "").strip().upper() == "IN"
    }
    outbound_ids = {
        int(row.source_corr_id)
        for row in request_rows
        if row.source_corr_id and (row.source_corr_kind or "").strip().upper() == "OUT"
    }
    inbound_refs = {}
    for source_id_chunk in _id_chunks(inbound_ids):
        inbound_refs.update({
            int(source_id): reference
            for source_id, reference in (
                db.session.query(InboundMail.id, InboundMail.ref_no)
                .filter(InboundMail.id.in_(source_id_chunk))
                .all()
            )
        })
    outbound_refs = {}
    for source_id_chunk in _id_chunks(outbound_ids):
        outbound_refs.update({
            int(source_id): reference
            for source_id, reference in (
                db.session.query(OutboundMail.id, OutboundMail.ref_no)
                .filter(OutboundMail.id.in_(source_id_chunk))
                .all()
            )
        })

    contexts = {}
    for row in request_rows:
        source_id = int(row.source_corr_id or 0)
        source_kind = (row.source_corr_kind or "").strip().upper()
        if source_kind not in {"IN", "OUT"}:
            continue
        references = inbound_refs if source_kind == "IN" else outbound_refs
        if not source_id or source_id not in references:
            continue
        contexts[int(row.id)] = {
            "kind": source_kind,
            "kind_label": "وارد" if source_kind == "IN" else "صادر",
            "ref_no": references[source_id] or source_id,
        }
    return contexts
