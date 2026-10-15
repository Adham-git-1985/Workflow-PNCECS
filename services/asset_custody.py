"""Employee review and traceable fixed-asset custody transactions.

Callers own commit/rollback; notifications and changes share one transaction.
"""
from datetime import datetime

from sqlalchemy.orm import joinedload, selectinload

from extensions import db
from models import (
    InvAssetDocument,
    InvAssetDocumentEvent,
    InvAssetDocumentLine,
    InvAssetDocumentReservation,
    InvFixedAsset,
    InvFixedAssetCommitteeReport,
    InvFixedAssetPreviousLine,
    Notification,
    User,
)
from services.fixed_asset_inventory import open_followup_inventory_cycle

KINDS = {"INVENTORY": "تقرير جرد", "ASSIGN": "إسناد عهدة", "TRANSFER": "نقل عهدة",
         "RETURN": "إرجاع عهدة", "DISPOSE": "إسقاط عهدة"}
STATUSES = {
    "PENDING": "بانتظار الموظف",
    "APPROVED": "وافق الموظف",
    "CHANGE_REQUESTED": "طلب الموظف مراجعة وتعديل",
    "REJECTED": "رفض الموظف",
    "REJECTED_ESCALATED": "رفض مرفوع للأمين العام",
    "ISSUED": "عهدة صادرة",
    "COMPLETED": "منفذ",
    "CANCELLED": "ملغى",
}
COMMITTEE_REPORT_STATUSES = {
    "DRAFT": "مسودة لدى اللجنة",
    "SUBMITTED": "مرفوع للأمين العام",
    "RETURNED": "معاد للجنة للتعديل",
    "APPROVED": "معتمد من الأمين العام",
}
EMPLOYEE_RESPONSE_LABELS = {
    "ACCEPT": "وافق الموظف",
    "REJECT": "رفض الموظف",
    "CHANGE": "طلب الموظف مراجعة وتعديل",
}
STATE_FIELDS = ("custodian_user_id", "warehouse_id", "room_id", "asset_condition", "lifecycle_status", "is_active", "asset_tag", "name", "serial_number")


def asset_state(asset):
    return {key: getattr(asset, key) for key in STATE_FIELDS}


def notify_document(doc, user_id, message, *, link_url=None):
    db.session.add(Notification(user_id=user_id, message=f"{message} — رقم {doc.id}", source="portal",
                                link_url=link_url or f"/portal/inventory/fixed-assets/documents/{doc.id}",
                                event_key=f"asset-doc-{doc.id}-{doc.status}", actor_id=doc.created_by_id))


def record_document_event(doc, action, actor_id=None, note=None, metadata=None):
    db.session.add(InvAssetDocumentEvent(
        document_id=doc.id,
        action=(action or "UPDATE").strip().upper()[:40],
        actor_id=actor_id,
        note=(note or "").strip() or None,
        metadata_json=metadata or None,
        created_at=datetime.utcnow(),
    ))


def _transition(doc, before, after):
    # Compare-and-swap prevents double issue/review from two browser tabs.
    changed = db.session.query(InvAssetDocument).filter_by(id=doc.id, status=before).update(
        {"status": after}, synchronize_session=False)
    if changed != 1:
        raise ValueError("تغيرت حالة المستند؛ حدّث الصفحة وحاول مجدداً.")
    db.session.expire(doc, ["status"])


def pending_for_asset(asset_id):
    return (InvAssetDocumentLine.query.join(InvAssetDocument)
            .filter(InvAssetDocumentLine.asset_id == asset_id,
                    InvAssetDocument.status.in_(["PENDING", "APPROVED", "CHANGE_REQUESTED"])).first())


def _new_document(kind, employee_id, actor_id, assets, *, cycle=None, reason=None, entries=None):
    if kind not in KINDS or not db.session.get(User, employee_id) or not assets:
        raise ValueError("حدد موظفاً وأصولاً صحيحة.")
    for asset in assets:
        if pending_for_asset(asset.id):
            raise ValueError(f"الأصل {asset.asset_tag} مرتبط بمستند لم ينتهِ بعد.")
    doc = InvAssetDocument(kind=kind, employee_id=employee_id, created_by_id=actor_id,
                           cycle_id=cycle.id if cycle else None, reason=reason, status="PENDING")
    db.session.add(doc)
    db.session.flush()
    employee = db.session.get(User, employee_id)
    for asset in assets:
        entry = entries.get(asset.id) if entries else None
        eligible = asset.is_active and asset.lifecycle_status != "DISPOSED" and (not entry or (entry.scanned_at is not None and entry.observed_lifecycle_status not in ("DISPOSED", "LOST")))
        db.session.add(InvAssetDocumentReservation(asset_id=asset.id, document_id=doc.id))
        doc.lines.append(InvAssetDocumentLine(asset_id=asset.id, snapshot={
            "tag": asset.asset_tag, "name": asset.name, "serial": asset.serial_number,
            "state": asset_state(asset), "location": asset.location_label,
            "custodian": employee.full_name if employee else "",
            "condition": entry.observed_condition if entry and entry.scanned_at else asset.asset_condition,
            "result": entry.result_status if entry else None, "inventory_note": entry.note if entry else None,
            "eligible": bool(eligible),
            "target_lifecycle": entry.observed_lifecycle_status if entry and entry.scanned_at else asset.lifecycle_status,
        }))
    db.session.flush()
    record_document_event(doc, "CREATED", actor_id, reason)
    if kind not in {"RETURN", "DISPOSE"}:
        notify_document(doc, employee_id, "يرجى مراجعة بنود " + KINDS[kind])
    return doc


def send_cycle_reports(cycle, actor_id):
    if cycle.status != "CLOSED":
        raise ValueError("أغلق دورة الجرد أولاً لتثبيت نتائج التقرير.")
    if cycle.finalized_at is not None:
        raise ValueError("اعتمد الأمين العام تقرير هذه الدورة؛ استخدم دورة الجرد الجديدة.")
    sent_employee_ids = {row.employee_id for row in InvAssetDocument.query.filter(
        InvAssetDocument.cycle_id == cycle.id,
        InvAssetDocument.kind == "INVENTORY",
        InvAssetDocument.status != "CANCELLED",
    ).all()}
    groups = {}
    for entry in cycle.entries:
        employee_id = entry.observed_custodian_user_id if entry.scanned_at else entry.expected_custodian_user_id
        if employee_id and employee_id not in sent_employee_ids:
            groups.setdefault(employee_id, []).append(entry)
    if not groups:
        raise ValueError("لا توجد تقارير جديدة للإرسال؛ راجع التقارير القائمة أو ربط البنود بالموظفين.")
    return [_new_document("INVENTORY", employee_id, actor_id, [e.asset for e in entries],
                          cycle=cycle, entries={e.asset_id: e for e in entries})
            for employee_id, entries in groups.items()]


def create_movement(asset, kind, employee_id, actor_id, reason):
    reason = (reason or "").strip()
    if kind not in {"ASSIGN", "TRANSFER", "RETURN", "DISPOSE"} or not reason:
        raise ValueError("حدد نوع الحركة وأدخل سببها أو مرجعها.")
    if not asset.is_active or asset.lifecycle_status == "DISPOSED":
        raise ValueError("لا يمكن تحريك أصل مؤرشف أو مسقط.")
    if kind == "ASSIGN" and asset.custodian_user_id:
        raise ValueError("الأصل على عهدة موظف بالفعل؛ استخدم نقل العهدة.")
    if kind in {"TRANSFER", "RETURN"} and not asset.custodian_user_id:
        raise ValueError("الأصل ليس على عهدة موظف.")
    if kind == "TRANSFER" and employee_id == asset.custodian_user_id:
        raise ValueError("اختر موظفاً مختلفاً للنقل.")
    if kind in {"RETURN", "DISPOSE"}:
        employee_id = asset.custodian_user_id or actor_id
    doc = _new_document(kind, employee_id, actor_id, [asset], reason=reason)
    if kind in {"RETURN", "DISPOSE"}:
        # These are administrative actions, not an employee signature.
        _transition(doc, "PENDING", "APPROVED")
        issue_document(doc, actor_id)
    return doc


def review_document(doc, employee_id, responses):
    if doc.employee_id != employee_id or doc.status != "PENDING":
        raise ValueError("هذا المستند غير متاح لاعتمادك.")
    rejected = False
    for line in doc.lines:
        decision, note = responses.get(line.id, (None, None))
        note = (note or "").strip()
        if decision not in {"ACCEPT", "OBJECT"}:
            raise ValueError("راجع كل بند وحدد موافق أو اعتراض.")
        if decision == "OBJECT" and not note:
            raise ValueError("أدخل ملاحظة توضح الاعتراض على البند.")
        if len(note) > 4000:
            raise ValueError("ملاحظة البند يجب ألا تتجاوز 4000 حرف.")
        line.decision, line.employee_note = decision, note or None
        rejected = rejected or decision == "OBJECT"
    _transition(doc, "PENDING", "REJECTED" if rejected else "APPROVED")
    doc.reviewed_at = datetime.utcnow()
    if rejected:
        InvAssetDocumentReservation.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
    doc.employee_response = "REJECT" if rejected else "ACCEPT"
    doc.employee_response_note = next(
        (line.employee_note for line in doc.lines if line.employee_note),
        None,
    )
    doc.response_at = datetime.utcnow()
    doc.response_by_id = employee_id
    record_document_event(
        doc,
        "EMPLOYEE_REJECTED" if rejected else "EMPLOYEE_ACCEPTED",
        employee_id,
        doc.employee_response_note,
    )
    notify_document(doc, doc.created_by_id, "أعاد الموظف التقرير مع ملاحظات" if rejected else "اعتمد الموظف جميع البنود؛ المستند جاهز للإصدار")


def review_inventory_document(doc, employee_id, action, note=None):
    """Apply the fixed-inventory custody decision made by the employee.

    Inventory custody uses three intentionally separate paths.  The older
    ``review_document`` function remains available for ordinary movements and
    for backward compatibility with existing documents.
    """
    if doc.kind != "INVENTORY" or doc.employee_id != employee_id or doc.status != "PENDING":
        raise ValueError("هذه العهدة غير متاحة لاتخاذ قرار الموظف.")

    action = (action or "").strip().upper()
    note = (note or "").strip()
    if action not in {"ACCEPT", "REJECT", "CHANGE"}:
        raise ValueError("اختر الموافقة أو الرفض أو طلب المراجعة والتعديل.")
    if action in {"REJECT", "CHANGE"} and len(note) < 3:
        raise ValueError("يجب توضيح السبب أو التعديل المطلوب بشكل واضح.")
    if len(note) > 4000:
        raise ValueError("الملاحظة يجب ألا تتجاوز 4000 حرف.")

    for line in doc.lines:
        line.decision = "ACCEPT" if action == "ACCEPT" else "OBJECT"
        line.employee_note = note or None

    now = datetime.utcnow()
    doc.employee_response = action
    doc.employee_response_note = note or None
    doc.response_at = now
    doc.response_by_id = employee_id
    _transition(doc, "PENDING", {
        "ACCEPT": "APPROVED",
        "REJECT": "REJECTED",
        "CHANGE": "CHANGE_REQUESTED",
    }[action])
    doc.reviewed_at = now
    if action == "REJECT":
        InvAssetDocumentReservation.query.filter_by(document_id=doc.id).delete(synchronize_session=False)

    record_document_event(
        doc,
        {"ACCEPT": "EMPLOYEE_ACCEPTED", "REJECT": "EMPLOYEE_REJECTED", "CHANGE": "EMPLOYEE_CHANGE_REQUEST"}[action],
        employee_id,
        note,
    )
    notify_document(
        doc,
        doc.created_by_id,
        {
            "ACCEPT": "وافق الموظف على العهدة؛ أصبحت جاهزة للجنة",
            "REJECT": "رفض الموظف العهدة؛ يمكن للجنة تصعيد الرفض",
            "CHANGE": "طلب الموظف مراجعة وتعديل العهدة",
        }[action],
    )


def revise_inventory_document(doc, actor_id, line_updates, note=None):
    """Let the committee revise a requested custody and send it again."""
    if doc.kind != "INVENTORY" or doc.status not in {"CHANGE_REQUESTED", "RETURNED"}:
        raise ValueError("لا يمكن تعديل هذه العهدة في حالتها الحالية.")
    note = (note or "").strip()
    if len(note) > 4000:
        raise ValueError("ملاحظة اللجنة يجب ألا تتجاوز 4000 حرف.")
    updates = line_updates or {}
    if not doc.lines:
        raise ValueError("لا توجد بنود لتعديلها.")

    for line in doc.lines:
        values = updates.get(line.id, {}) or {}
        snapshot = dict(line.snapshot or {})
        for key, max_length in (
            ("tag", 255), ("name", 255), ("serial", 200), ("location", 500),
            ("condition", 40), ("target_lifecycle", 40), ("inventory_note", 4000),
        ):
            if key in values:
                value = str(values.get(key) or "").strip()
                snapshot[key] = value[:max_length] or None
        if "included" in values:
            snapshot["included"] = bool(values["included"])
        snapshot["committee_revision_note"] = note or None
        line.snapshot = snapshot
        line.decision = "PENDING"
        line.employee_note = None

    now = datetime.utcnow()
    doc.status = "PENDING"
    doc.employee_response = None
    doc.employee_response_note = None
    doc.response_at = None
    doc.response_by_id = None
    doc.reviewed_at = None
    doc.revision_number = int(doc.revision_number or 0) + 1
    doc.committee_revision_note = note or None
    doc.committee_revised_at = now
    doc.committee_revised_by_id = actor_id
    record_document_event(doc, "COMMITTEE_REVISED", actor_id, note, {"revision": doc.revision_number})
    notify_document(doc, doc.employee_id, "أعادت اللجنة إرسال العهدة بعد التعديل")


def escalate_inventory_rejection(doc, actor_id, note=None):
    """Escalate a clear employee rejection to the Secretary General."""
    if doc.kind != "INVENTORY" or doc.status != "REJECTED":
        raise ValueError("لا يمكن تصعيد هذه العهدة إلا بعد رفض الموظف.")
    note = (note or doc.employee_response_note or "").strip()
    if len(note) < 3:
        raise ValueError("يجب إدخال سبب واضح للتصعيد.")
    if len(note) > 4000:
        raise ValueError("سبب التصعيد يجب ألا يتجاوز 4000 حرف.")
    doc.status = "REJECTED_ESCALATED"
    doc.rejection_escalated_at = datetime.utcnow()
    doc.rejection_escalated_by_id = actor_id
    doc.rejection_escalation_note = note
    record_document_event(doc, "REJECTION_ESCALATED", actor_id, note)
    notify_document(doc, doc.created_by_id, "تم تصعيد رفض الموظف إلى الأمين العام")


def get_or_create_committee_report(cycle, actor_id=None):
    report = InvFixedAssetCommitteeReport.query.filter_by(cycle_id=cycle.id).first()
    if report is None:
        report = InvFixedAssetCommitteeReport(
            cycle_id=cycle.id,
            status="DRAFT",
            created_by_id=actor_id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(report)
        db.session.flush()
    return report


def inventory_documents_for_cycle(cycle_id):
    return (
        InvAssetDocument.query
        .options(
            joinedload(InvAssetDocument.employee),
            joinedload(InvAssetDocument.cycle),
            selectinload(InvAssetDocument.lines).joinedload(InvAssetDocumentLine.asset),
        )
        .filter(
            InvAssetDocument.cycle_id == cycle_id,
            InvAssetDocument.kind == "INVENTORY",
            InvAssetDocument.status != "CANCELLED",
        )
        .order_by(InvAssetDocument.employee_id.asc(), InvAssetDocument.id.asc())
        .all()
    )


def submit_committee_report(report, actor_id):
    if report.status not in {"DRAFT", "RETURNED"}:
        raise ValueError("هذا التقرير لا يمكن رفعه من حالته الحالية.")
    if not report.cycle or report.cycle.status != "CLOSED":
        raise ValueError("يجب إغلاق دورة الجرد قبل رفع تقرير اللجنة.")
    if report.cycle.finalized_at is not None:
        raise ValueError("اعتمد الأمين العام تقرير هذه الدورة؛ لا يمكن رفعه مرة أخرى.")
    documents = inventory_documents_for_cycle(report.cycle_id)
    if not documents:
        raise ValueError("لا توجد عهد موظفين لإدراجها في التقرير.")
    pending = [doc for doc in documents if doc.status in {"PENDING", "CHANGE_REQUESTED"}]
    if pending:
        raise ValueError("لا يمكن رفع التقرير قبل استكمال قرارات الموظفين على جميع العهد.")
    now = datetime.utcnow()
    report.status = "SUBMITTED"
    report.submitted_by_id = actor_id
    report.submitted_at = now
    report.updated_at = now
    report.secretary_note = None
    _notify_secretary_general(report)
    return report


def _notify_secretary_general(report):
    """Notify configured Secretary General accounts without adding a service dependency."""
    recipients = []
    for user in User.query.order_by(User.id.asc()).all():
        try:
            role = (user.role or "").upper().replace(" ", "_").replace("-", "_")
            if (
                user.has_perm("INVENTORY_FIXED_ASSET_REPORT_APPROVE")
                or user.has_perm("INVENTORY_SECRETARY_GENERAL_APPROVE")
                or "SECRETARY_GENERAL" in role
                or role in {"SECRETARY", "GENERAL_SECRETARY"}
            ):
                recipients.append(user.id)
        except Exception:
            continue
    for user_id in recipients:
        db.session.add(Notification(
            user_id=user_id,
            message=f"تقرير جرد الأصول الثابتة للدورة {report.cycle.code} بانتظار مراجعة الأمين العام",
            source="portal",
            link_url=f"/portal/inventory/fixed-assets/cycles/{report.cycle_id}/committee-report",
            event_key=f"fixed-asset-report-{report.id}-submitted",
            actor_id=report.submitted_by_id,
        ))


def decide_committee_report(report, actor_id, decision, note=None):
    if report.status != "SUBMITTED":
        raise ValueError("هذا التقرير ليس بانتظار قرار الأمين العام.")
    decision = (decision or "").strip().upper()
    note = (note or "").strip()
    if decision not in {"APPROVE", "RETURN"}:
        raise ValueError("قرار التقرير غير صالح.")
    if decision == "RETURN" and len(note) < 3:
        raise ValueError("يجب توضيح سبب إعادة التقرير للجنة.")
    report.status = "APPROVED" if decision == "APPROVE" else "RETURNED"
    report.decided_by_id = actor_id
    report.decided_at = datetime.utcnow()
    report.secretary_note = note or None
    report.updated_at = datetime.utcnow()
    next_cycle = None
    if decision == "APPROVE":
        next_cycle = open_followup_inventory_cycle(report.cycle, created_by_id=actor_id)
        report.cycle.finalized_at = report.decided_at
        report.cycle.finalized_by_id = actor_id
    # The Secretary General approves the consolidated committee report only.
    # Employee custody approval remains the employee's decision, and the
    # operational issue/assignment of an approved custody is a separate action.
    for doc in inventory_documents_for_cycle(report.cycle_id):
        notify_document(
            doc,
            doc.employee_id,
            "تم اعتماد تقرير لجنة الجرد من الأمين العام" if decision == "APPROVE" else "أعاد الأمين العام تقرير لجنة الجرد للجنة",
            link_url=f"/portal/inventory/fixed-assets/documents/{doc.id}",
        )
    if next_cycle is not None:
        report.cycle.successor_cycle_id = next_cycle.id
        report.cycle.successor_cycle = next_cycle
    return report


def save_manual_previous_lines(cycle, employee_id, raw_text, actor_id):
    """Replace one employee's manual previous-year rows from a simple pipe list.

    Format per line: asset tag | name | serial | location | condition | note
    """
    employee = db.session.get(User, employee_id)
    if employee is None:
        raise ValueError("الموظف غير موجود.")
    rows = []
    for line_number, raw_line in enumerate((raw_text or "").splitlines(), start=1):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        parts = [part.strip() for part in value.split("|")]
        if len(parts) < 2:
            raise ValueError(f"السطر {line_number}: استخدم الفاصل | بين بيانات الأصل.")
        tag = parts[0][:255]
        if not tag:
            raise ValueError(f"السطر {line_number}: رقم الأصل مطلوب.")
        asset = InvFixedAsset.query.filter(InvFixedAsset.asset_tag.ilike(tag)).first()
        rows.append(InvFixedAssetPreviousLine(
            cycle_id=cycle.id,
            employee_id=employee_id,
            asset_id=asset.id if asset else None,
            asset_tag=tag,
            snapshot={
                "tag": tag,
                "name": (parts[1] if len(parts) > 1 else "")[:255] or None,
                "serial": (parts[2] if len(parts) > 2 else "")[:200] or None,
                "location": (parts[3] if len(parts) > 3 else "")[:500] or None,
                "custodian": employee.full_name,
                "condition": (parts[4] if len(parts) > 4 else "")[:40] or None,
                "note": (parts[5] if len(parts) > 5 else "")[:4000] or None,
            },
            source="MANUAL",
            created_by_id=actor_id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
    InvFixedAssetPreviousLine.query.filter_by(cycle_id=cycle.id, employee_id=employee_id).delete(synchronize_session=False)
    db.session.add_all(rows)
    return len(rows)


def issue_document(doc, actor_id):
    if doc.status != "APPROVED":
        raise ValueError("يلزم اعتماد الموظف قبل إصدار العهدة.")
    assignable_lines = [
        line
        for line in doc.lines
        if line.snapshot.get("eligible", True) and line.snapshot.get("included", True)
    ]
    report_only = doc.kind == "INVENTORY" and not assignable_lines
    _transition(doc, "APPROVED", "COMPLETED" if doc.kind in {"RETURN", "DISPOSE"} or report_only else "ISSUED")
    for line in doc.lines:
        if doc.kind == "INVENTORY" and (
            not line.snapshot.get("eligible", True)
            or not line.snapshot.get("included", True)
        ):
            continue
        old_state = line.snapshot["state"]
        values = {"updated_by_id": actor_id, "updated_at": datetime.utcnow()}
        if doc.kind in {"INVENTORY", "ASSIGN", "TRANSFER"}:
            values.update(custodian_user_id=doc.employee_id, lifecycle_status=(line.snapshot["target_lifecycle"] or "ACTIVE") if doc.kind == "INVENTORY" else "ACTIVE")
        else:
            values.update(custodian_user_id=None, lifecycle_status="DISPOSED" if doc.kind == "DISPOSE" else "IN_STORE")
            if doc.kind == "DISPOSE":
                values["is_active"] = False
        changed = InvFixedAsset.query.filter_by(id=line.asset_id, **old_state).update(values, synchronize_session=False)
        if changed != 1:
            raise ValueError(f"تغيرت بيانات الأصل {line.snapshot['tag']} بعد إرسال التقرير. ألغ المستند وأصدر تقريراً محدثاً.")
        db.session.expire(line.asset)
        old_owner = old_state["custodian_user_id"]
        if old_owner and old_owner != doc.employee_id:
            notify_document(doc, old_owner, f"تم نقل الأصل {line.snapshot['tag']} من عهدتك",
                            link_url="/portal/inventory/fixed-assets/documents" if len(doc.lines) > 1 else None)
    doc.issued_at, doc.issued_by_id = datetime.utcnow(), actor_id
    record_document_event(doc, "ISSUED", actor_id)
    InvAssetDocumentReservation.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
    notify_document(doc, doc.employee_id, "تم إصدار العهدة" if doc.status == "ISSUED" else ("تم اعتماد تقرير الجرد النهائي" if report_only else "تم تنفيذ " + KINDS[doc.kind]))


def cancel_document(doc):
    if doc.status not in {"PENDING", "APPROVED", "CHANGE_REQUESTED", "REJECTED", "REJECTED_ESCALATED"}:
        raise ValueError("لا يمكن إلغاء حركة منفذة. استخدم حركة إرجاع أو نقل موثقة.")
    _transition(doc, doc.status, "CANCELLED")
    InvAssetDocumentReservation.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
    record_document_event(doc, "CANCELLED", doc.created_by_id)
    notify_document(doc, doc.employee_id, "تم إلغاء المستند للمراجعة")
