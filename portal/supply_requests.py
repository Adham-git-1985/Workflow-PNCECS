from collections import defaultdict
from io import BytesIO
import json
from datetime import date, datetime

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from . import portal_bp
from extensions import db
from models import (
    EmployeeFile,
    InvEmployeeRequest,
    InvEmployeeRequestAction,
    InvEmployeeRequestLine,
    InvIssueVoucher,
    InvIssueVoucherLine,
    InvItem,
    InvItemCategory,
    InvWarehouse,
    Message,
    MessageRecipient,
    Notification,
    SystemSetting,
    User,
    UserPermission,
)
from utils.perms import perm_required
from services.hr_request_workflow import resolve_responsible_managers
from services.official_request_forms import (
    DOCX_MIME,
    build_supply_request_docx,
    build_supply_request_pdf,
)


STAGES = {
    "MANAGER": "المسؤولون المباشرون",
    "WAREHOUSE": "مدير المستودع",
    "DONE": "مكتمل",
}


def _setting(key):
    row = SystemSetting.query.filter_by(key=key).first()
    return int(row.value) if row and (row.value or "").isdigit() else None


def _set_setting(key, value):
    row = SystemSetting.query.filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.session.add(SystemSetting(key=key, value=value))


def _grant_permission(user_id, key):
    if not user_id:
        return
    row = UserPermission.query.filter_by(user_id=user_id, key=key).first()
    if row:
        row.is_allowed = True
    else:
        db.session.add(UserPermission(user_id=user_id, key=key, is_allowed=True))


def _manager_ids(row):
    """Return the direct-manager snapshot for a submitted materials request."""
    raw = (getattr(row, "manager_user_ids", None) or "").strip()
    ids = []
    if raw:
        try:
            values = json.loads(raw)
            values = values if isinstance(values, list) else [values]
            ids = [int(value) for value in values if str(value).isdigit()]
        except (TypeError, ValueError, json.JSONDecodeError):
            ids = [int(value) for value in raw.split(",") if value.strip().isdigit()]
    if not ids and row.manager_user_id:
        ids = [int(row.manager_user_id)]
    if not ids:
        ids = [int(user.id) for user in resolve_responsible_managers(row.requester_user_id)]
    return list(dict.fromkeys(ids))


def _is_system_admin(user):
    try:
        return bool(user.has_role("SUPER_ADMIN") or user.has_role("SUPERADMIN") or user.has_role("ADMIN"))
    except Exception:
        return (getattr(user, "role", "") or "").upper().replace("_", "") in {"SUPERADMIN", "ADMIN"}


def _recipient_ids(row):
    if row.approval_stage == "MANAGER":
        return _manager_ids(row)
    if row.approval_stage == "WAREHOUSE":
        configured = _setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID")
        if configured:
            return [configured]
        return [user.id for user in User.query.all() if user.has_perm("INVENTORY_REQUEST_APPROVE")]
    return []


def _can_process(row):
    if row.status != "SUBMITTED":
        return False
    if _is_system_admin(current_user):
        return True
    if row.approval_stage == "MANAGER":
        return current_user.id in _manager_ids(row)
    if row.approval_stage == "WAREHOUSE":
        configured = _setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID")
        return current_user.id == configured or current_user.has_perm("INVENTORY_REQUEST_APPROVE")
    return False


def _can_manage():
    configured_ids = {
        _setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID"),
    }
    return _is_system_admin(current_user) or current_user.id in configured_ids or current_user.has_perm("INVENTORY_REQUEST_APPROVE") or current_user.has_perm("STORE_MANAGE")


def _can_manage_catalog():
    return current_user.has_perm("INVENTORY_REQUEST_APPROVE") or current_user.has_perm("STORE_MANAGE")


def _can_view(row):
    return row.requester_user_id == current_user.id or _can_manage() or _can_process(row) or any(
        action.actor_user_id == current_user.id for action in row.actions
    )


def _action_for_stage(row, stage, *, after=None):
    return next(
        (
            action for action in sorted(row.actions, key=lambda value: value.created_at or datetime.min, reverse=True)
            if action.stage == stage and action.action == "APPROVED" and (after is None or action.created_at >= after)
        ),
        None,
    )


def _supply_form_payload(row):
    employee = EmployeeFile.query.get(row.requester_user_id)
    last_update = max(
        (action.created_at for action in row.actions if action.action == "UPDATED" and action.created_at),
        default=None,
    )
    manager_action = _action_for_stage(row, "MANAGER", after=last_update)
    warehouse_action = _action_for_stage(row, "WAREHOUSE", after=last_update)
    created_at = row.created_at or datetime.utcnow()
    return {
        "request_no": str(row.id),
        "request_date": created_at.strftime("%Y/%m/%d"),
        "organization": (
            getattr(getattr(employee, "organization", None), "name_ar", None)
            or getattr(getattr(employee, "organization", None), "name", None)
            or "-"
        ),
        "directorate": (
            getattr(getattr(employee, "directorate", None), "name_ar", None)
            or getattr(getattr(employee, "directorate", None), "name", None)
            or "-"
        ),
        "requester_name": row.requester.full_name or row.requester.name or row.requester.email,
        "requester_date": created_at.strftime("%Y/%m/%d"),
        "manager_name": (manager_action.actor.full_name if manager_action and manager_action.actor else ""),
        "manager_note": manager_action.note if manager_action else "",
        "warehouse_name": (warehouse_action.actor.full_name if warehouse_action and warehouse_action.actor else ""),
        "warehouse_note": warehouse_action.note if warehouse_action else "",
        "lines": [
            {
                "item": line.item.label if line.item else "-",
                "unit": (line.item.unit if line.item else "") or "-",
                "quantity": f"{float(line.requested_qty or 0):g}",
            }
            for line in row.lines
        ],
    }


def _notify(row, recipient_ids, text):
    recipient_ids = sorted({user_id for user_id in recipient_ids if user_id and user_id != current_user.id})
    if not recipient_ids:
        return
    link = url_for("portal.inventory_employee_request_view", request_id=row.id)
    for user_id in recipient_ids:
        db.session.add(Notification(
            user_id=user_id,
            message=text,
            type="INFO",
            source="portal",
            link_url=link,
            is_read=False,
            created_at=datetime.utcnow(),
        ))
    message = Message(
        sender_id=current_user.id,
        subject=f"طلب مواد #{row.id}",
        body=f"{text}\n{link}",
        target_kind="USER",
        target_id=recipient_ids[0],
        created_at=datetime.utcnow(),
        is_system_generated=True,
    )
    db.session.add(message)
    db.session.flush()
    db.session.add_all([
        MessageRecipient(message_id=message.id, recipient_user_id=user_id)
        for user_id in recipient_ids
    ])


def _inventory_balances():
    from .routes import _inv_build_balances

    return _inv_build_balances()


def _catalog_context(*, include_items=True):
    item_query = InvItem.query.filter(InvItem.is_active.is_(True)).order_by(InvItem.name.asc())
    # The employee request screen resolves items through the small remote
    # lookup.  Do not render the complete catalogue three times in its form.
    items = item_query.all() if include_items else []
    categories = InvItemCategory.query.filter(InvItemCategory.is_active.is_(True)).order_by(InvItemCategory.name.asc()).all()
    warehouses = InvWarehouse.query.filter(InvWarehouse.is_active.is_(True)).order_by(InvWarehouse.name.asc()).all()
    balances = _inventory_balances() if include_items else {}
    item_totals = {
        item.id: sum(float(quantity or 0) for (warehouse_id, item_id), quantity in balances.items() if item_id == item.id)
        for item in items
    }
    warehouse_balances = {
        f"{warehouse_id}:{item_id}": float(quantity or 0)
        for (warehouse_id, item_id), quantity in balances.items()
    }
    return items, categories, warehouses, item_totals, warehouse_balances


def _parse_requested_lines():
    item_ids = request.form.getlist("item_id")
    quantities = request.form.getlist("requested_qty")
    requested = defaultdict(float)
    for item_id_raw, quantity_raw in zip(item_ids, quantities):
        if not (item_id_raw or "").isdigit():
            continue
        try:
            quantity = float(quantity_raw)
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            requested[int(item_id_raw)] += quantity
    active_ids = {
        item.id for item in InvItem.query.filter(InvItem.id.in_(requested.keys()), InvItem.is_active.is_(True)).all()
    } if requested else set()
    return [(item_id, quantity) for item_id, quantity in requested.items() if item_id in active_ids]


def _request_summary(lines):
    return "، ".join(
        f"{line.requested_qty:g} {line.item.unit or 'وحدة'} من {line.item.name}"
        for line in lines
    )


def _replace_lines(row, requested_lines):
    row.lines.clear()
    db.session.flush()
    for item_id, quantity in requested_lines:
        row.lines.append(InvEmployeeRequestLine(item_id=item_id, requested_qty=quantity))
    db.session.flush()
    row.items_text = _request_summary(row.lines)


def _stock_errors(lines, warehouse_id):
    balances = _inventory_balances()
    errors = []
    by_item = defaultdict(float)
    for line in lines:
        by_item[line.item_id] += float(line.approved_qty or 0)
    for item_id, quantity in by_item.items():
        available = float(balances.get((warehouse_id, item_id), 0) or 0)
        if quantity > available:
            item = InvItem.query.get(item_id)
            errors.append(f"{item.name if item else item_id}: المطلوب {quantity:g} والمتاح {available:g}")
    return errors


def _create_issue_vouchers(row):
    grouped = defaultdict(list)
    for line in row.lines:
        if line.warehouse_id and float(line.approved_qty or 0) > 0:
            grouped[line.warehouse_id].append(line)
    for warehouse_id, lines in grouped.items():
        errors = _stock_errors(lines, warehouse_id)
        if errors:
            raise ValueError("الرصيد غير كافٍ: " + "؛ ".join(errors))
        voucher = InvIssueVoucher(
            issue_kind="EMPLOYEE",
            voucher_no=f"EMP-{row.id}-{warehouse_id}",
            voucher_date=date.today().isoformat(),
            from_warehouse_id=warehouse_id,
            to_room_name=row.requester.full_name,
            note=f"صرف آلي مقابل طلب مواد الموظف #{row.id}: {row.purpose}",
            created_by_id=current_user.id,
        )
        db.session.add(voucher)
        db.session.flush()
        for line in lines:
            db.session.add(InvIssueVoucherLine(
                voucher_id=voucher.id,
                item_id=line.item_id,
                qty=line.approved_qty,
                details=f"طلب مواد الموظف #{row.id}",
            ))
            line.issue_voucher_id = voucher.id


@portal_bp.route("/admin/inventory-request-settings", methods=["GET", "POST"])
@login_required
@perm_required("PORTAL_ADMIN_PERMISSIONS_MANAGE")
def inventory_request_settings():
    if request.method == "POST":
        warehouse_manager_id = request.form.get("warehouse_manager_user_id") or ""
        _set_setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID", warehouse_manager_id)
        _grant_permission(int(warehouse_manager_id) if warehouse_manager_id.isdigit() else None, "INVENTORY_REQUEST_APPROVE")
        _grant_permission(int(warehouse_manager_id) if warehouse_manager_id.isdigit() else None, "PORTAL_REPORTS_READ")
        db.session.commit()
        flash("تم حفظ مسؤولي اعتماد طلبات المواد.", "success")
        return redirect(url_for("portal.inventory_request_settings"))
    return render_template(
        "portal/inventory/request_settings.html",
        users=User.query.order_by(User.name.asc()).all(),
        warehouse_manager_id=_setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID"),
    )


@portal_bp.route("/inventory/employee-requests")
@login_required
def inventory_employee_requests():
    query = InvEmployeeRequest.query
    if not _can_manage():
        query = query.filter_by(requester_user_id=current_user.id)
    rows = query.order_by(InvEmployeeRequest.created_at.desc()).all()
    pending_count = sum(1 for row in rows if _can_process(row))
    return render_template(
        "portal/inventory/employee_requests.html",
        rows=rows,
        stages=STAGES,
        can_manage=_can_manage(),
        can_manage_catalog=_can_manage_catalog(),
        pending_count=pending_count,
    )


@portal_bp.route("/inventory/employee-requests/tasks")
@login_required
def inventory_employee_request_tasks():
    rows = [
        row for row in InvEmployeeRequest.query.filter_by(status="SUBMITTED").order_by(InvEmployeeRequest.created_at.desc()).all()
        if _can_process(row)
    ]
    return render_template(
        "portal/inventory/employee_requests.html",
        rows=rows,
        stages=STAGES,
        can_manage=_can_manage(),
        can_manage_catalog=_can_manage_catalog(),
        pending_count=len(rows),
        tasks=True,
    )


@portal_bp.route("/inventory/employee-requests/new", methods=["GET", "POST"])
@login_required
def inventory_employee_request_new():
    items, categories, warehouses, item_totals, warehouse_balances = _catalog_context(include_items=False)
    catalog_has_items = InvItem.query.filter(InvItem.is_active.is_(True)).limit(1).first() is not None
    if request.method == "POST":
        requested_lines = _parse_requested_lines()
        purpose = (request.form.get("purpose") or "").strip()
        if not requested_lines or not purpose:
            flash("اختر مادة واحدة على الأقل وأدخل الكمية وسبب الطلب.", "danger")
            return render_template(
                "portal/inventory/employee_request_form.html",
                item=None,
                items=items,
                categories=categories,
                item_totals=item_totals,
                catalog_has_items=catalog_has_items,
                can_manage_catalog=_can_manage_catalog(),
            )
        managers = resolve_responsible_managers(current_user.id)
        manager_ids = [int(manager.id) for manager in managers if manager and manager.id != current_user.id]
        manager_id = manager_ids[0] if manager_ids else None
        row = InvEmployeeRequest(
            requester_user_id=current_user.id,
            manager_user_id=manager_id,
            manager_user_ids=json.dumps(manager_ids, separators=(",", ":")) if manager_ids else None,
            items_text="",
            purpose=purpose,
            note=(request.form.get("note") or "").strip() or None,
            approval_stage="MANAGER" if manager_id else "WAREHOUSE",
        )
        db.session.add(row)
        db.session.flush()
        _replace_lines(row, requested_lines)
        db.session.add(InvEmployeeRequestAction(
            request_id=row.id,
            stage=row.approval_stage,
            action="SUBMITTED",
            actor_user_id=current_user.id,
            note="تم إرسال طلب المواد للاعتماد.",
        ))
        _notify(row, _recipient_ids(row), f"طلب مواد #{row.id} بانتظار متابعتك لدى {STAGES[row.approval_stage]}.")
        db.session.commit()
        flash("تم إرسال طلب المواد للاعتماد.", "success")
        return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
    return render_template(
        "portal/inventory/employee_request_form.html",
        item=None,
        items=items,
        categories=categories,
        item_totals=item_totals,
        catalog_has_items=catalog_has_items,
        can_manage_catalog=_can_manage_catalog(),
    )


@portal_bp.route("/inventory/employee-requests/<int:request_id>", methods=["GET", "POST"])
@login_required
def inventory_employee_request_view(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_view(row):
        abort(403)
    items, categories, warehouses, item_totals, warehouse_balances = _catalog_context()
    # The employee owns the requested quantities.  Managers record their
    # approval/comment in their own stage and cannot silently alter the form.
    can_edit = row.status == "SUBMITTED" and row.requester_user_id == current_user.id
    if request.method == "POST":
        if not can_edit:
            abort(403)
        requested_lines = _parse_requested_lines()
        purpose = (request.form.get("purpose") or "").strip()
        if not requested_lines or not purpose:
            flash("يجب أن يحتوي الطلب على مادة واحدة على الأقل وسبب واضح.", "danger")
            return redirect(request.url)
        old_signature = sorted((line.item_id, float(line.requested_qty)) for line in row.lines)
        new_signature = sorted((item_id, float(quantity)) for item_id, quantity in requested_lines)
        row.purpose = purpose
        row.note = (request.form.get("note") or "").strip() or None
        if old_signature != new_signature:
            _replace_lines(row, requested_lines)
            row.approval_stage = "MANAGER" if _manager_ids(row) else "WAREHOUSE"
            _notify(row, _recipient_ids(row), f"تم تعديل طلب المواد #{row.id} ويحتاج إعادة المتابعة لدى {STAGES[row.approval_stage]}.")
        db.session.add(InvEmployeeRequestAction(
            request_id=row.id,
            stage=row.approval_stage,
            action="UPDATED",
            actor_user_id=current_user.id,
            note="تم تعديل تفاصيل طلب المواد." + (" وأعيد إلى بداية مسار الاعتماد." if old_signature != new_signature else ""),
        ))
        db.session.commit()
        flash("تم حفظ تعديل الطلب.", "success")
        return redirect(request.url)
    return render_template(
        "portal/inventory/employee_request_view.html",
        item=row,
        items=items,
        categories=categories,
        warehouses=warehouses,
        item_totals=item_totals,
        warehouse_balances=warehouse_balances,
        stages=STAGES,
        can_process=_can_process(row),
        can_edit=can_edit,
        can_manage_catalog=_can_manage_catalog(),
    )


@portal_bp.route("/inventory/employee-requests/<int:request_id>/form.pdf")
@login_required
def inventory_employee_request_form_pdf(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_view(row):
        abort(403)
    response = send_file(
        BytesIO(build_supply_request_pdf(_supply_form_payload(row))),
        mimetype="application/pdf",
        as_attachment=request.args.get("download") == "1",
        download_name=f"طلب لوازم من المستودع - {row.id}.pdf",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@portal_bp.route("/inventory/employee-requests/<int:request_id>/form.docx")
@login_required
def inventory_employee_request_form_docx(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_view(row):
        abort(403)
    response = send_file(
        BytesIO(build_supply_request_docx(_supply_form_payload(row))),
        mimetype=DOCX_MIME,
        as_attachment=True,
        download_name=f"طلب لوازم من المستودع - {row.id}.docx",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@portal_bp.route("/inventory/employee-requests/<int:request_id>/approve", methods=["POST"])
@login_required
def inventory_employee_request_approve(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_process(row):
        abort(403)
    note = (request.form.get("note") or "").strip() or None
    current_stage = row.approval_stage
    if current_stage == "MANAGER":
        row.approval_stage = "WAREHOUSE"
    elif current_stage == "WAREHOUSE":
        warehouse_id_raw = request.form.get("warehouse_id") or ""
        if not warehouse_id_raw.isdigit():
            flash("اختر مستودع الصرف.", "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        warehouse_id = int(warehouse_id_raw)
        warehouse = InvWarehouse.query.filter_by(id=warehouse_id, is_active=True).first()
        if not warehouse:
            flash("مستودع الصرف غير صالح.", "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        total_approved = 0.0
        for line in row.lines:
            try:
                approved_qty = float(request.form.get(f"approved_qty_{line.id}") or 0)
            except ValueError:
                approved_qty = 0
            if approved_qty < 0 or approved_qty > float(line.requested_qty or 0):
                flash(f"الكمية المعتمدة للصنف {line.item.name} غير صحيحة.", "danger")
                return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
            line.approved_qty = approved_qty
            line.warehouse_id = warehouse_id
            total_approved += approved_qty
        if total_approved <= 0:
            flash("اعتمد كمية موجبة لصنف واحد على الأقل.", "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        errors = _stock_errors(row.lines, warehouse_id)
        if errors:
            flash("الرصيد غير كافٍ: " + "؛ ".join(errors), "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        try:
            _create_issue_vouchers(row)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        row.status = "APPROVED"
        row.approval_stage = "DONE"
        row.decided_at = datetime.utcnow()
    else:
        abort(400)
    db.session.add(InvEmployeeRequestAction(
        request_id=row.id,
        stage=current_stage,
        action="APPROVED",
        actor_user_id=current_user.id,
        note=note,
    ))
    if row.status == "SUBMITTED":
        _notify(row, _recipient_ids(row), f"طلب المواد #{row.id} بانتظار متابعتك لدى {STAGES[row.approval_stage]}.")
    else:
        _notify(row, [row.requester_user_id], f"تم اعتماد طلب المواد #{row.id} نهائياً وصرف المواد من المستودع.")
    db.session.commit()
    flash("تمت متابعة طلب المواد." if row.status == "SUBMITTED" else "تم الاعتماد النهائي وإنشاء سند الصرف وخصم الكميات.", "success")
    return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))


@portal_bp.route("/inventory/employee-requests/report")
@login_required
def inventory_employee_requests_report():
    if not _can_manage() and not current_user.has_perm("PORTAL_REPORTS_READ"):
        abort(403)
    selected_month = (request.args.get("month") or "").strip()
    selected_user_id = (request.args.get("user_id") or "").strip()
    selected_item_id = (request.args.get("item_id") or "").strip()
    query = (
        InvEmployeeRequestLine.query
        .join(InvEmployeeRequest, InvEmployeeRequest.id == InvEmployeeRequestLine.request_id)
        .filter(InvEmployeeRequest.status == "APPROVED", InvEmployeeRequestLine.issue_voucher_id.isnot(None))
    )
    if selected_month:
        query = query.join(InvIssueVoucher, InvIssueVoucher.id == InvEmployeeRequestLine.issue_voucher_id).filter(
            InvIssueVoucher.voucher_date.like(f"{selected_month}%")
        )
    if selected_user_id.isdigit():
        query = query.filter(InvEmployeeRequest.requester_user_id == int(selected_user_id))
    if selected_item_id.isdigit():
        query = query.filter(InvEmployeeRequestLine.item_id == int(selected_item_id))
    rows = query.order_by(InvEmployeeRequest.decided_at.desc(), InvEmployeeRequestLine.id.desc()).all()
    item_summary = defaultdict(float)
    employee_summary = defaultdict(float)
    for line in rows:
        item_summary[line.item.label] += float(line.approved_qty or 0)
        employee_summary[line.request.requester.full_name] += float(line.approved_qty or 0)
    return render_template(
        "portal/inventory/employee_consumption_report.html",
        rows=rows,
        item_summary=sorted(item_summary.items(), key=lambda pair: pair[0]),
        employee_summary=sorted(employee_summary.items(), key=lambda pair: pair[0]),
        users=User.query.order_by(User.name.asc()).all(),
        items=InvItem.query.order_by(InvItem.name.asc()).all(),
        selected_month=selected_month,
        selected_user_id=int(selected_user_id) if selected_user_id.isdigit() else None,
        selected_item_id=int(selected_item_id) if selected_item_id.isdigit() else None,
    )
