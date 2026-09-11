"""Employee review and traceable fixed-asset custody transactions.

Callers own commit/rollback; notifications and changes share one transaction.
"""
from datetime import datetime

from extensions import db
from models import InvAssetDocument, InvAssetDocumentLine, InvAssetDocumentReservation, InvFixedAsset, Notification, User

KINDS = {"INVENTORY": "تقرير جرد", "ASSIGN": "إسناد عهدة", "TRANSFER": "نقل عهدة",
         "RETURN": "إرجاع عهدة", "DISPOSE": "إسقاط عهدة"}
STATUSES = {"PENDING": "بانتظار الموظف", "APPROVED": "اعتمده الموظف", "REJECTED": "أعيد للمراجعة",
            "ISSUED": "عهدة صادرة", "COMPLETED": "منفذ", "CANCELLED": "ملغى"}
STATE_FIELDS = ("custodian_user_id", "warehouse_id", "room_id", "asset_condition", "lifecycle_status", "is_active", "asset_tag", "name", "serial_number")


def asset_state(asset):
    return {key: getattr(asset, key) for key in STATE_FIELDS}


def notify_document(doc, user_id, message, *, link_url=None):
    db.session.add(Notification(user_id=user_id, message=f"{message} — رقم {doc.id}", source="portal",
                                link_url=link_url or f"/portal/inventory/fixed-assets/documents/{doc.id}",
                                event_key=f"asset-doc-{doc.id}-{doc.status}", actor_id=doc.created_by_id))


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
                    InvAssetDocument.status.in_(["PENDING", "APPROVED"])).first())


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
    for asset in assets:
        entry = entries.get(asset.id) if entries else None
        eligible = asset.is_active and asset.lifecycle_status != "DISPOSED" and (not entry or (entry.scanned_at is not None and entry.observed_lifecycle_status not in ("DISPOSED", "LOST")))
        db.session.add(InvAssetDocumentReservation(asset_id=asset.id, document_id=doc.id))
        doc.lines.append(InvAssetDocumentLine(asset_id=asset.id, snapshot={
            "tag": asset.asset_tag, "name": asset.name, "serial": asset.serial_number,
            "state": asset_state(asset), "location": asset.location_label,
            "condition": entry.observed_condition if entry and entry.scanned_at else asset.asset_condition,
            "result": entry.result_status if entry else None, "inventory_note": entry.note if entry else None,
            "eligible": bool(eligible),
            "target_lifecycle": entry.observed_lifecycle_status if entry and entry.scanned_at else asset.lifecycle_status,
        }))
    db.session.flush()
    if kind not in {"RETURN", "DISPOSE"}:
        notify_document(doc, employee_id, "يرجى مراجعة بنود " + KINDS[kind])
    return doc


def send_cycle_reports(cycle, actor_id):
    if cycle.status != "CLOSED":
        raise ValueError("أغلق دورة الجرد أولاً لتثبيت نتائج التقرير.")
    sent_employee_ids = {row.employee_id for row in InvAssetDocument.query.filter(
        InvAssetDocument.cycle_id == cycle.id, InvAssetDocument.status.notin_(["CANCELLED", "REJECTED"])).all()}
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
    notify_document(doc, doc.created_by_id, "أعاد الموظف التقرير مع ملاحظات" if rejected else "اعتمد الموظف جميع البنود؛ المستند جاهز للإصدار")


def issue_document(doc, actor_id):
    if doc.status != "APPROVED":
        raise ValueError("يلزم اعتماد الموظف قبل إصدار العهدة.")
    report_only = doc.kind == "INVENTORY" and not any(line.snapshot["eligible"] for line in doc.lines)
    _transition(doc, "APPROVED", "COMPLETED" if doc.kind in {"RETURN", "DISPOSE"} or report_only else "ISSUED")
    for line in doc.lines:
        if not line.snapshot["eligible"] and doc.kind == "INVENTORY":
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
    InvAssetDocumentReservation.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
    notify_document(doc, doc.employee_id, "تم إصدار العهدة" if doc.status == "ISSUED" else ("تم اعتماد تقرير الجرد النهائي" if report_only else "تم تنفيذ " + KINDS[doc.kind]))


def cancel_document(doc):
    if doc.status not in {"PENDING", "APPROVED", "REJECTED"}:
        raise ValueError("لا يمكن إلغاء حركة منفذة. استخدم حركة إرجاع أو نقل موثقة.")
    _transition(doc, doc.status, "CANCELLED")
    InvAssetDocumentReservation.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
    notify_document(doc, doc.employee_id, "تم إلغاء المستند للمراجعة")
