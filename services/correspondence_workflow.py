"""Bridge correspondence records and workflow requests.

The correspondence module owns the official inbound/outbound record while
Workflow owns routing and approvals.  This module keeps both sides linked
without importing either blueprint (which would create circular imports).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, or_

from extensions import db
from utils.corr_refs import correspondence_reference_label
from models import (
    CorrMovement,
    Department,
    Directorate,
    Division,
    InboundMail,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgUnitAssignment,
    OrgUnitManager,
    Organization,
    OutboundMail,
    Section,
    Unit,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
)
from services.correspondence_procedure import can_access_correspondence


_CORRESPONDENCE_TARGET_QUERY_CHUNK_SIZE = 400


def _chunked_correspondence_target_keys(target_keys):
    target_keys = tuple(target_keys)
    for offset in range(0, len(target_keys), _CORRESPONDENCE_TARGET_QUERY_CHUNK_SIZE):
        yield target_keys[offset:offset + _CORRESPONDENCE_TARGET_QUERY_CHUNK_SIZE]


def _correspondence_target_key(target: dict | None) -> tuple[str, int] | None:
    """Return the normalized identity used by correspondence target resolvers."""
    target = target or {}
    kind = (target.get("kind") or "").strip().upper()
    try:
        target_id = int(target.get("id"))
    except (TypeError, ValueError):
        return None
    return kind, target_id


def filter_correspondence_notification_user_ids(
    user_ids: Iterable[int] | None,
    *,
    confidentiality: str | None = None,
    created_by_user_id: int | None = None,
    current_assignee_user_id: int | None = None,
    authorized_user_ids: Iterable[int] | None = None,
) -> set[int]:
    """Keep only users who can actually open a correspondence notification.

    Routing targets describe an audience, but membership in that audience is
    not itself a correspondence permission. Apply the same access policy as
    the correspondence views before creating a notification, so a user does
    not receive a link that will immediately return 403.
    """
    candidate_ids = {
        int(user_id)
        for user_id in (user_ids or ())
        if user_id
    }
    if not candidate_ids:
        return set()

    authorized_ids = {
        int(user_id)
        for user_id in (authorized_user_ids or ())
        if user_id
    }
    try:
        users = User.query.filter(User.id.in_(candidate_ids)).all()
    except Exception:
        # Do not emit a notification when the authorization lookup is not
        # available. Sending an unverified document link is the unsafe path.
        return set()

    allowed_ids: set[int] = set()
    for user in users:
        try:
            if can_access_correspondence(
                confidentiality=confidentiality,
                user_id=user.id,
                has_regular_read=bool(user.has_perm("CORR_READ")),
                has_confidential_read=bool(user.has_perm("CORR_CONFIDENTIAL_READ")),
                has_confidential_manage=bool(user.has_perm("CORR_CONFIDENTIAL_MANAGE")),
                created_by_user_id=created_by_user_id,
                current_assignee_user_id=current_assignee_user_id,
                authorized_user_ids=authorized_ids,
            ):
                allowed_ids.add(int(user.id))
        except Exception:
            # Permission evaluation is deliberately fail-closed per recipient.
            continue
    return allowed_ids


def correspondence_target_user_ids(target: dict | None) -> list[int]:
    """Resolve every user represented by a correspondence routing target."""
    target_key = _correspondence_target_key(target)
    if target_key is None:
        return []
    kind, target_id = target_key

    user_ids: set[int] = set()
    if kind == "USER":
        return [target_id]

    if kind == "ORG_NODE":
        try:
            user_ids.update(
                int(uid) for (uid,) in (
                    db.session.query(OrgNodeAssignment.user_id)
                    .filter(OrgNodeAssignment.node_id == target_id)
                    .all()
                ) if uid
            )
            managers = OrgNodeManager.query.filter_by(node_id=target_id).all()
            for manager in managers:
                if manager.manager_user_id:
                    user_ids.add(int(manager.manager_user_id))
                if manager.deputy_user_id:
                    user_ids.add(int(manager.deputy_user_id))
            user_ids.update(
                int(uid) for (uid,) in (
                    db.session.query(User.id)
                    .filter(User.org_node_id == target_id)
                    .all()
                ) if uid
            )
        except Exception:
            pass
        return sorted(user_ids)

    unit_type = "ORGANIZATION" if kind == "ORG" else kind
    try:
        user_ids.update(
            int(uid) for (uid,) in (
                db.session.query(OrgUnitAssignment.user_id)
                .filter(
                    func.upper(OrgUnitAssignment.unit_type) == unit_type,
                    OrgUnitAssignment.unit_id == target_id,
                )
                .all()
            ) if uid
        )
    except Exception:
        pass

    try:
        for manager in OrgUnitManager.query.filter(
            func.upper(OrgUnitManager.unit_type) == unit_type,
            OrgUnitManager.unit_id == target_id,
        ).all():
            if manager.manager_user_id:
                user_ids.add(int(manager.manager_user_id))
            if manager.deputy_user_id:
                user_ids.add(int(manager.deputy_user_id))
    except Exception:
        pass

    direct_column = {
        "DIRECTORATE": User.directorate_id,
        "DEPARTMENT": User.department_id,
        "UNIT": User.unit_id,
        "SECTION": User.section_id,
        "DIVISION": User.division_id,
    }.get(unit_type)
    if direct_column is not None:
        try:
            user_ids.update(
                int(uid) for (uid,) in (
                    db.session.query(User.id)
                    .filter(direct_column == target_id)
                    .all()
                ) if uid
            )
        except Exception:
            pass

    try:
        if unit_type == "DIRECTORATE":
            department_ids = [
                int(did) for (did,) in (
                    db.session.query(Department.id)
                    .filter(Department.directorate_id == target_id)
                    .all()
                ) if did
            ]
            if department_ids:
                user_ids.update(
                    int(uid) for (uid,) in (
                        db.session.query(User.id)
                        .filter(User.department_id.in_(department_ids))
                        .all()
                    ) if uid
                )
        elif unit_type == "UNIT":
            department_ids = [
                int(did) for (did,) in (
                    db.session.query(Department.id)
                    .filter(Department.unit_id == target_id)
                    .all()
                ) if did
            ]
            if department_ids:
                user_ids.update(
                    int(uid) for (uid,) in (
                        db.session.query(User.id)
                        .filter(User.department_id.in_(department_ids))
                        .all()
                    ) if uid
                )
    except Exception:
        pass

    return sorted(user_ids)


def bulk_correspondence_target_user_ids(
    targets: Iterable[dict | None] | None,
) -> dict[tuple[str, int], list[int]]:
    """Resolve many distinct correspondence targets with bounded queries.

    Results are keyed by the same normalized ``(kind, id)`` identity used by
    :func:`correspondence_target_user_ids`. Invalid targets are omitted and
    duplicate identities are coalesced. The number of queries is bounded by
    the target source types rather than growing with the number of targets.
    """
    target_keys: dict[tuple[str, int], None] = {}
    for target in targets or ():
        target_key = _correspondence_target_key(target)
        if target_key is not None:
            target_keys.setdefault(target_key, None)

    user_ids_by_key: dict[tuple[str, int], set[int]] = {
        target_key: set() for target_key in target_keys
    }
    if not user_ids_by_key:
        return {}

    node_keys_by_id: dict[int, tuple[str, int]] = {}
    unit_keys_by_identity: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for target_key in target_keys:
        kind, target_id = target_key
        if kind == "USER":
            user_ids_by_key[target_key].add(target_id)
        elif kind == "ORG_NODE":
            node_keys_by_id[target_id] = target_key
        else:
            unit_type = "ORGANIZATION" if kind == "ORG" else kind
            unit_keys_by_identity.setdefault((unit_type, target_id), []).append(target_key)

    for node_ids in _chunked_correspondence_target_keys(node_keys_by_id):
        try:
            rows = (
                db.session.query(OrgNodeAssignment.node_id, OrgNodeAssignment.user_id)
                .filter(OrgNodeAssignment.node_id.in_(node_ids))
                .all()
            )
            for node_id, user_id in rows:
                if user_id:
                    target_key = node_keys_by_id.get(int(node_id))
                    if target_key is not None:
                        user_ids_by_key[target_key].add(int(user_id))

            rows = (
                db.session.query(
                    OrgNodeManager.node_id,
                    OrgNodeManager.manager_user_id,
                    OrgNodeManager.deputy_user_id,
                )
                .filter(OrgNodeManager.node_id.in_(node_ids))
                .all()
            )
            for node_id, manager_user_id, deputy_user_id in rows:
                target_key = node_keys_by_id.get(int(node_id))
                if target_key is None:
                    continue
                if manager_user_id:
                    user_ids_by_key[target_key].add(int(manager_user_id))
                if deputy_user_id:
                    user_ids_by_key[target_key].add(int(deputy_user_id))

            rows = (
                db.session.query(User.org_node_id, User.id)
                .filter(User.org_node_id.in_(node_ids))
                .all()
            )
            for node_id, user_id in rows:
                if user_id:
                    target_key = node_keys_by_id.get(int(node_id))
                    if target_key is not None:
                        user_ids_by_key[target_key].add(int(user_id))
        except Exception:
            pass

    if unit_keys_by_identity:
        def add_unit_user(unit_type, unit_id, user_id) -> None:
            if not user_id:
                return
            identity = ((unit_type or "").upper(), int(unit_id))
            for target_key in unit_keys_by_identity.get(identity, ()):
                user_ids_by_key[target_key].add(int(user_id))

        unit_identity_chunks = tuple(
            _chunked_correspondence_target_keys(unit_keys_by_identity)
        )
        for identity_chunk in unit_identity_chunks:
            unit_types = tuple({unit_type for unit_type, _unit_id in identity_chunk})
            unit_ids = tuple({unit_id for _unit_type, unit_id in identity_chunk})
            try:
                rows = (
                    db.session.query(
                        OrgUnitAssignment.unit_type,
                        OrgUnitAssignment.unit_id,
                        OrgUnitAssignment.user_id,
                    )
                    .filter(
                        func.upper(OrgUnitAssignment.unit_type).in_(unit_types),
                        OrgUnitAssignment.unit_id.in_(unit_ids),
                    )
                    .all()
                )
                for unit_type, unit_id, user_id in rows:
                    add_unit_user(unit_type, unit_id, user_id)
            except Exception:
                pass

        for identity_chunk in unit_identity_chunks:
            unit_types = tuple({unit_type for unit_type, _unit_id in identity_chunk})
            unit_ids = tuple({unit_id for _unit_type, unit_id in identity_chunk})
            try:
                rows = (
                    db.session.query(
                        OrgUnitManager.unit_type,
                        OrgUnitManager.unit_id,
                        OrgUnitManager.manager_user_id,
                        OrgUnitManager.deputy_user_id,
                    )
                    .filter(
                        func.upper(OrgUnitManager.unit_type).in_(unit_types),
                        OrgUnitManager.unit_id.in_(unit_ids),
                    )
                    .all()
                )
                for unit_type, unit_id, manager_user_id, deputy_user_id in rows:
                    add_unit_user(unit_type, unit_id, manager_user_id)
                    add_unit_user(unit_type, unit_id, deputy_user_id)
            except Exception:
                pass

        direct_columns = {
            "DIRECTORATE": User.directorate_id,
            "DEPARTMENT": User.department_id,
            "UNIT": User.unit_id,
            "SECTION": User.section_id,
            "DIVISION": User.division_id,
        }
        direct_ids_by_type = {
            unit_type: {
                unit_id
                for candidate_type, unit_id in unit_keys_by_identity
                if candidate_type == unit_type
            }
            for unit_type in direct_columns
        }
        direct_identities = (
            (unit_type, unit_id)
            for unit_type, unit_ids_for_type in direct_ids_by_type.items()
            for unit_id in unit_ids_for_type
        )
        for identity_chunk in _chunked_correspondence_target_keys(direct_identities):
            chunk_ids_by_type = {
                unit_type: {
                    unit_id
                    for candidate_type, unit_id in identity_chunk
                    if candidate_type == unit_type
                }
                for unit_type in direct_columns
            }
            direct_filters = [
                direct_columns[unit_type].in_(unit_ids_for_type)
                for unit_type, unit_ids_for_type in chunk_ids_by_type.items()
                if unit_ids_for_type
            ]
            try:
                rows = (
                    db.session.query(
                        User.id,
                        User.directorate_id,
                        User.department_id,
                        User.unit_id,
                        User.section_id,
                        User.division_id,
                    )
                    .filter(or_(*direct_filters))
                    .all()
                )
                direct_types = tuple(direct_columns)
                for row in rows:
                    user_id, *direct_unit_ids = row
                    if not user_id:
                        continue
                    for unit_type, unit_id in zip(direct_types, direct_unit_ids):
                        if unit_id is not None:
                            add_unit_user(unit_type, unit_id, user_id)
            except Exception:
                pass

        directorate_ids = direct_ids_by_type["DIRECTORATE"]
        top_level_unit_ids = direct_ids_by_type["UNIT"]
        descendant_identities = (
            (unit_type, unit_id)
            for unit_type, unit_ids_for_type in (
                ("DIRECTORATE", directorate_ids),
                ("UNIT", top_level_unit_ids),
            )
            for unit_id in unit_ids_for_type
        )
        for identity_chunk in _chunked_correspondence_target_keys(descendant_identities):
            chunk_directorate_ids = {
                unit_id for unit_type, unit_id in identity_chunk
                if unit_type == "DIRECTORATE"
            }
            chunk_unit_ids = {
                unit_id for unit_type, unit_id in identity_chunk
                if unit_type == "UNIT"
            }
            descendant_filters = []
            if chunk_directorate_ids:
                descendant_filters.append(
                    Department.directorate_id.in_(chunk_directorate_ids)
                )
            if chunk_unit_ids:
                descendant_filters.append(Department.unit_id.in_(chunk_unit_ids))
            try:
                rows = (
                    db.session.query(
                        User.id,
                        Department.directorate_id,
                        Department.unit_id,
                    )
                    .join(Department, User.department_id == Department.id)
                    .filter(or_(*descendant_filters))
                    .all()
                )
                for user_id, directorate_id, unit_id in rows:
                    if not user_id:
                        continue
                    if directorate_id is not None:
                        add_unit_user("DIRECTORATE", directorate_id, user_id)
                    if unit_id is not None:
                        add_unit_user("UNIT", unit_id, user_id)
            except Exception:
                pass

    return {
        target_key: sorted(user_ids)
        for target_key, user_ids in user_ids_by_key.items()
    }


def correspondence_actor_target_keys(
    user_ids: Iterable[int] | None,
) -> set[tuple[str, int]]:
    """Return every correspondence target identity represented by users.

    This is the inverse, actor-scoped form of
    :func:`correspondence_target_user_ids`.  It lets inbox queries discard
    unrelated correspondence rows in SQL without resolving every candidate
    target separately.  The query count depends on the number of actor-id
    chunks, not on the number of correspondence records.
    """
    actor_user_ids: set[int] = set()
    for user_id in user_ids or ():
        try:
            normalized_user_id = int(user_id)
        except (TypeError, ValueError):
            continue
        if normalized_user_id:
            actor_user_ids.add(normalized_user_id)

    if not actor_user_ids:
        return set()

    target_keys: set[tuple[str, int]] = {
        ("USER", user_id) for user_id in actor_user_ids
    }
    department_ids: set[int] = set()

    def add_target(kind, target_id) -> None:
        if target_id is None:
            return
        try:
            target_keys.add((str(kind or "").upper(), int(target_id)))
        except (TypeError, ValueError):
            return

    def add_unit_target(unit_type, unit_id) -> None:
        if unit_type is None or unit_id is None:
            return
        normalized_type = str(unit_type).upper()
        # USER and ORG_NODE are terminal cases in the scalar resolver and
        # never consult legacy unit membership.  ORG is an input alias for
        # ORGANIZATION, not a valid stored unit type on its own.
        if normalized_type in {"USER", "ORG_NODE", "ORG"}:
            return
        add_target(normalized_type, unit_id)
        if normalized_type == "ORGANIZATION":
            add_target("ORG", unit_id)

    for actor_id_chunk in _chunked_correspondence_target_keys(actor_user_ids):
        try:
            actor_rows = (
                db.session.query(
                    User.org_node_id,
                    User.directorate_id,
                    User.department_id,
                    User.unit_id,
                    User.section_id,
                    User.division_id,
                )
                .filter(User.id.in_(actor_id_chunk))
                .all()
            )
            for (
                org_node_id,
                directorate_id,
                department_id,
                unit_id,
                section_id,
                division_id,
            ) in actor_rows:
                add_target("ORG_NODE", org_node_id)
                add_unit_target("DIRECTORATE", directorate_id)
                add_unit_target("DEPARTMENT", department_id)
                add_unit_target("UNIT", unit_id)
                add_unit_target("SECTION", section_id)
                add_unit_target("DIVISION", division_id)
                if department_id is not None:
                    department_ids.add(int(department_id))
        except Exception:
            pass

        try:
            rows = (
                db.session.query(OrgNodeAssignment.node_id)
                .filter(OrgNodeAssignment.user_id.in_(actor_id_chunk))
                .all()
            )
            for (node_id,) in rows:
                add_target("ORG_NODE", node_id)
        except Exception:
            pass

        try:
            rows = (
                db.session.query(OrgNodeManager.node_id)
                .filter(or_(
                    OrgNodeManager.manager_user_id.in_(actor_id_chunk),
                    OrgNodeManager.deputy_user_id.in_(actor_id_chunk),
                ))
                .all()
            )
            for (node_id,) in rows:
                add_target("ORG_NODE", node_id)
        except Exception:
            pass

        try:
            rows = (
                db.session.query(
                    OrgUnitAssignment.unit_type,
                    OrgUnitAssignment.unit_id,
                )
                .filter(OrgUnitAssignment.user_id.in_(actor_id_chunk))
                .all()
            )
            for unit_type, unit_id in rows:
                add_unit_target(unit_type, unit_id)
        except Exception:
            pass

        try:
            rows = (
                db.session.query(
                    OrgUnitManager.unit_type,
                    OrgUnitManager.unit_id,
                )
                .filter(or_(
                    OrgUnitManager.manager_user_id.in_(actor_id_chunk),
                    OrgUnitManager.deputy_user_id.in_(actor_id_chunk),
                ))
                .all()
            )
            for unit_type, unit_id in rows:
                add_unit_target(unit_type, unit_id)
        except Exception:
            pass

    for department_id_chunk in _chunked_correspondence_target_keys(department_ids):
        try:
            rows = (
                db.session.query(
                    Department.directorate_id,
                    Department.unit_id,
                )
                .filter(Department.id.in_(department_id_chunk))
                .all()
            )
            for directorate_id, unit_id in rows:
                add_unit_target("DIRECTORATE", directorate_id)
                add_unit_target("UNIT", unit_id)
        except Exception:
            pass

    return target_keys


def source_correspondence(req: WorkflowRequest):
    """Return ``(kind, item)`` for a correspondence-backed workflow."""
    source_id = getattr(req, "source_corr_id", None)
    source_kind = (getattr(req, "source_corr_kind", None) or "").strip().upper()
    if not source_id:
        return None, None
    if source_kind == "IN":
        return "IN", db.session.get(InboundMail, int(source_id))
    if source_kind == "OUT":
        return "OUT", db.session.get(OutboundMail, int(source_id))
    return None, None


def correspondence_context(req: WorkflowRequest) -> dict | None:
    """Build the source card displayed on a Workflow request page."""
    kind, item = source_correspondence(req)
    if not item:
        return None

    is_inbound = kind == "IN"
    movements = (
        CorrMovement.query
        .filter_by(
            inbound_id=item.id if is_inbound else None,
            outbound_id=item.id if not is_inbound else None,
        )
        .order_by(CorrMovement.created_at.desc(), CorrMovement.id.desc())
        .limit(8)
        .all()
    )
    return {
        "kind": kind,
        "kind_label": "وارد" if is_inbound else "صادر",
        "item": item,
        "ref_no": item.ref_no or item.id,
        "date": item.received_date if is_inbound else item.sent_date,
        "party_label": "الجهة المرسلة" if is_inbound else "الجهة المستلمة",
        "party": item.sender if is_inbound else item.recipient,
        "status": (item.status or ("RECEIVED" if is_inbound else "DRAFT")).upper(),
        "subject": item.subject or "",
        "body": item.body or "",
        "category": item.category or "",
        "competence": item.competence_label or item.current_target_label or "",
        "target": item.current_target_label or item.competence_label or "",
        "priority": item.priority or "NORMAL",
        "confidentiality": item.confidentiality or "NORMAL",
        "due_date": item.due_date,
        "movements": movements,
        "source_inbound_id": getattr(item, "source_inbound_id", None),
    }


def _target_from_step(step: WorkflowInstanceStep | None) -> dict:
    if not step:
        return {"kind": None, "id": None, "label": None, "user_id": None}

    kind = (getattr(step, "approver_kind", None) or "").strip().upper()
    model_and_id = {
        "USER": (User, getattr(step, "approver_user_id", None)),
        "ORGANIZATION": (Organization, getattr(step, "approver_organization_id", None)),
        "DIRECTORATE": (Directorate, getattr(step, "approver_directorate_id", None)),
        "UNIT": (Unit, getattr(step, "approver_unit_id", None)),
        "DEPARTMENT": (Department, getattr(step, "approver_department_id", None)),
        "SECTION": (Section, getattr(step, "approver_section_id", None)),
        "DIVISION": (Division, getattr(step, "approver_division_id", None)),
        "ORG_NODE": (OrgNode, getattr(step, "approver_org_node_id", None)),
    }.get(kind)

    target_id = None
    label = None
    if model_and_id:
        model, target_id = model_and_id
        if target_id:
            row = db.session.get(model, int(target_id))
            if row:
                label = (
                    getattr(row, "full_name", None)
                    or getattr(row, "name_ar", None)
                    or getattr(row, "name", None)
                    or getattr(row, "email", None)
                )
    elif kind == "ROLE":
        label = getattr(step, "approver_role", None)

    if not label and kind:
        label = kind
    return {
        "kind": kind or None,
        "id": int(target_id) if target_id else None,
        "label": label,
        "user_id": int(target_id) if kind == "USER" and target_id else None,
    }


def _workflow_target(req: WorkflowRequest) -> dict:
    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    if not inst or getattr(inst, "is_completed", False):
        return {"kind": None, "id": None, "label": None, "user_id": None}
    step = WorkflowInstanceStep.query.filter_by(
        instance_id=inst.id,
        step_order=inst.current_step_order,
    ).first()
    return _target_from_step(step)


def _mapped_status(req_status: str | None, current_status: str | None) -> str:
    status = (req_status or "").strip().upper()
    if status == "IN_PROGRESS":
        current = (current_status or "RECEIVED").strip().upper()
        # A direct procedural task may be waiting for a named user while the
        # linked template workflow is still active.  Advancing that workflow
        # must not silently erase the more specific correspondence state.
        if current in {
            "WAITING_APPROVAL", "WAITING_INFO", "APPROVED",
            "COMPLETED", "CLOSED", "ARCHIVED",
        }:
            return current
        return "IN_PROGRESS"
    if status == "APPROVED":
        return "APPROVED"
    if status == "REJECTED":
        return "RETURNED"
    return (current_status or "RECEIVED").strip().upper()


def _movement_action(req_status: str | None) -> str:
    return {
        "IN_PROGRESS": "WORKFLOW_SYNC",
        "APPROVED": "WORKFLOW_APPROVED",
        "REJECTED": "WORKFLOW_REJECTED",
    }.get((req_status or "").strip().upper(), "WORKFLOW_SYNC")


def _append_movement(
    *,
    kind: str,
    item,
    actor_user_id: int,
    action: str,
    old_status: str | None,
    new_status: str | None,
    target: dict,
    note: str | None,
) -> None:
    db.session.add(CorrMovement(
        inbound_id=item.id if kind == "IN" else None,
        outbound_id=item.id if kind == "OUT" else None,
        actor_user_id=int(actor_user_id),
        action=action,
        from_status=old_status,
        to_status=new_status,
        target_kind=target.get("kind"),
        target_id=target.get("id"),
        target_label=target.get("label"),
        target_user_id=target.get("user_id"),
        note=(note or "").strip() or None,
        is_internal=False,
        created_at=datetime.utcnow(),
    ))


def sync_correspondence_from_workflow(
    req: WorkflowRequest,
    *,
    actor_user_id: int | None,
    note: str | None = None,
) -> object | None:
    """Synchronize source status/current target from a Workflow request.

    The function only stages database changes; the caller owns the transaction.
    """
    kind, item = source_correspondence(req)
    if not item or not actor_user_id:
        return item

    old_status = (getattr(item, "status", None) or ("RECEIVED" if kind == "IN" else "DRAFT")).upper()
    new_status = _mapped_status(getattr(req, "status", None), old_status)
    target = _workflow_target(req)
    old_target = (
        getattr(item, "current_target_kind", None),
        getattr(item, "current_target_id", None),
        getattr(item, "current_target_label", None),
        getattr(item, "current_assignee_id", None),
    )
    new_target = (
        target.get("kind"),
        target.get("id"),
        target.get("label"),
        target.get("user_id"),
    )

    item.status = new_status
    if target.get("kind"):
        item.current_target_kind = target.get("kind")
        item.current_target_id = target.get("id")
        item.current_target_label = target.get("label")
        item.current_assignee_id = target.get("user_id")

    if old_status != new_status or (target.get("kind") and old_target != new_target):
        _append_movement(
            kind=kind,
            item=item,
            actor_user_id=int(actor_user_id),
            action=_movement_action(getattr(req, "status", None)),
            old_status=old_status,
            new_status=new_status,
            target=target,
            note=note or f"مزامنة تلقائية من مسار #{req.id}.",
        )

    # An approved/rejected official outbound reply closes or returns its source
    # inbound record as part of the same transaction.
    if kind == "OUT" and getattr(item, "source_inbound_id", None):
        inbound = db.session.get(InboundMail, int(item.source_inbound_id))
        if inbound and new_status in {"APPROVED", "RETURNED"}:
            inbound_old = (inbound.status or "RECEIVED").upper()
            inbound_new = "COMPLETED" if new_status == "APPROVED" else "RETURNED"
            inbound.status = inbound_new
            if inbound_old != inbound_new:
                outbound_ref_label = correspondence_reference_label(
                    "OUT", item.ref_no or item.id, include_number_word=True
                )
                _append_movement(
                    kind="IN",
                    item=inbound,
                    actor_user_id=int(actor_user_id),
                    action="FINAL_REPLY" if inbound_new == "COMPLETED" else "WORKFLOW_REJECTED",
                    old_status=inbound_old,
                    new_status=inbound_new,
                    target={"kind": "OUTBOUND", "id": item.id, "label": outbound_ref_label, "user_id": None},
                    note=(
                        f"اكتمل الرد الرسمي عبر {outbound_ref_label} ومسار #{req.id}."
                        if inbound_new == "COMPLETED"
                        else f"أُعيد الرد الرسمي المرتبط بـ {outbound_ref_label} من مسار #{req.id}."
                    ),
                )

    return item
