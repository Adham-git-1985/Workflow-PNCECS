"""Confidentiality guard shared by Workflow and Archive.

Workflow participants still need the normal workflow permission checks.  This
module adds the stricter correspondence ACL as a second, mandatory gate when a
request originated from confidential inbound or outbound correspondence.
"""

from __future__ import annotations

from sqlalchemy import or_

from extensions import db
from models import (
    CorrConfidentialAccess,
    InboundMail,
    OutboundMail,
    RequestAttachment,
    Role,
    RolePermission,
    User,
    WorkflowRequest,
)
from services.correspondence_procedure import can_access_correspondence


CONFIDENTIAL_READ_PERMISSION = "CORR_CONFIDENTIAL_READ"
CONFIDENTIAL_MANAGE_PERMISSION = "CORR_CONFIDENTIAL_MANAGE"


def is_confidential_workflow(req: WorkflowRequest | None) -> bool:
    return bool(
        req
        and (getattr(req, "confidentiality", "NORMAL") or "NORMAL").strip().upper()
        == "SECRET"
    )


def _source_correspondence(req: WorkflowRequest):
    source_id = getattr(req, "source_corr_id", None)
    source_kind = (getattr(req, "source_corr_kind", None) or "").strip().upper()
    if not source_id:
        return None
    if source_kind == "IN":
        return InboundMail.query.get(int(source_id))
    if source_kind == "OUT":
        return OutboundMail.query.get(int(source_id))
    return None


def _authorized_ids_for_item(item) -> set[int]:
    if isinstance(item, InboundMail):
        parent_filter = CorrConfidentialAccess.inbound_id == item.id
    elif isinstance(item, OutboundMail):
        parent_filter = CorrConfidentialAccess.outbound_id == item.id
    else:
        return set()

    return {
        int(row[0])
        for row in (
            db.session.query(CorrConfidentialAccess.user_id)
            .filter(parent_filter)
            .all()
        )
        if row[0]
    }


def _has_permission(user: User, permission: str) -> bool:
    # Deliberately evaluate the named user's own grants.  User.has_perm() also
    # ORs active delegation privileges from flask.g, which is correct for most
    # modules but would silently transfer confidential access to a delegatee.
    if isinstance(user, User):
        role = (getattr(user, "role", "") or "").strip()
        normalized_role = role.upper().replace("-", "_").replace(" ", "_")
        if normalized_role == "ADMIN" or normalized_role.startswith("SUPER"):
            return True

        key = (permission or "").strip().upper()
        for grant in (getattr(user, "permissions", None) or []):
            if (
                getattr(grant, "is_allowed", False)
                and (getattr(grant, "key", "") or "").strip().upper() == key
            ):
                return True

        if role:
            role_keys = {role.lower()}
            resolved_role = (
                Role.query
                .filter(or_(
                    db.func.lower(Role.code) == role.lower(),
                    db.func.lower(Role.name_en) == role.lower(),
                    Role.name_ar == role,
                ))
                .first()
            )
            if resolved_role and (resolved_role.code or "").strip():
                role_keys.add(resolved_role.code.strip().lower())
            return (
                RolePermission.query
                .filter(db.func.lower(RolePermission.role).in_(role_keys))
                .filter(db.func.upper(RolePermission.permission) == key)
                .first()
                is not None
            )
        return False

    try:
        return bool(user and user.has_perm(permission))
    except Exception:
        return False


def can_user_access_correspondence_item(user: User | None, item) -> bool:
    """Apply the correspondence ACL to one concrete user and source item."""
    if not user or not item:
        return False

    return can_access_correspondence(
        confidentiality=getattr(item, "confidentiality", None),
        user_id=getattr(user, "id", None),
        # Normal read is irrelevant for secret items but keeps this helper safe
        # if it is called before the classification check.
        has_regular_read=_has_permission(user, "CORR_READ"),
        has_confidential_read=_has_permission(user, CONFIDENTIAL_READ_PERMISSION),
        has_confidential_manage=_has_permission(user, CONFIDENTIAL_MANAGE_PERMISSION),
        created_by_user_id=getattr(item, "created_by_id", None),
        current_assignee_user_id=getattr(item, "current_assignee_id", None),
        authorized_user_ids=_authorized_ids_for_item(item),
    )


def can_user_pass_confidential_workflow_gate(
    user: User | None,
    req: WorkflowRequest | None,
) -> bool:
    """Return whether the request's confidentiality gate permits ``user``.

    A ``True`` result does not itself grant workflow access; callers must also
    apply their usual workflow participant/owner checks.
    """
    if not req or not user:
        return False
    if not is_confidential_workflow(req):
        return True

    source = _source_correspondence(req)
    if source is not None:
        return can_user_access_correspondence_item(user, source)

    # Fail closed if a legacy or damaged request has lost its source link.  The
    # requester and dedicated confidentiality permission holders remain able to
    # recover/manage it without exposing it to ordinary workflow participants.
    if getattr(req, "requester_id", None) == getattr(user, "id", None):
        return True
    return _has_permission(user, CONFIDENTIAL_READ_PERMISSION) or _has_permission(
        user, CONFIDENTIAL_MANAGE_PERMISSION
    )


def filter_confidential_correspondence_user_ids(item, user_ids) -> set[int]:
    """Keep only candidate users allowed by a correspondence item's ACL."""
    candidates = {int(user_id) for user_id in (user_ids or []) if user_id}
    if not candidates:
        return set()
    if (getattr(item, "confidentiality", "NORMAL") or "NORMAL").upper() != "SECRET":
        return candidates

    users = User.query.filter(User.id.in_(candidates)).all()
    return {
        int(user.id)
        for user in users
        if can_user_access_correspondence_item(user, item)
    }


def filter_confidential_workflow_user_ids(req: WorkflowRequest, user_ids) -> set[int]:
    """Keep only notification/task recipients allowed by a request's ACL."""
    candidates = {int(user_id) for user_id in (user_ids or []) if user_id}
    if not candidates or not is_confidential_workflow(req):
        return candidates

    users = User.query.filter(User.id.in_(candidates)).all()
    return {
        int(user.id)
        for user in users
        if can_user_pass_confidential_workflow_gate(user, req)
    }


def can_user_access_archived_file_confidentiality(user: User | None, file_id: int) -> bool:
    """Enforce secret workflow ACLs on an archived workflow attachment.

    A file can be linked to more than one request.  If any linked request is
    confidential, the user must pass every confidential request gate; this
    prevents a second, less restrictive link from downgrading the file.
    """
    if not user or not file_id:
        return False

    requests = (
        WorkflowRequest.query
        .join(RequestAttachment, RequestAttachment.request_id == WorkflowRequest.id)
        .filter(
            RequestAttachment.archived_file_id == int(file_id),
            WorkflowRequest.confidentiality == "SECRET",
        )
        .all()
    )
    return all(can_user_pass_confidential_workflow_gate(user, req) for req in requests)
