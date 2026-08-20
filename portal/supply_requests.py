from collections import defaultdict
from datetime import date, datetime

from flask import abort, flash, redirect, render_template, request, url_for
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


STAGES = {
    "MANAGER": "المدير المباشر",
    "WAREHOUSE": "مدير المستودع",
    "ADMIN": "مدير الشؤون الإدارية",
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


def _recipient_ids(row):
    if row.approval_stage == "MANAGER":
        return [row.manager_user_id] if row.manager_user_id else []
    if row.approval_stage == "WAREHOUSE":
        configured = _setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID")
        if configured:
            return [configured]
        return [user.id for user in User.query.all() if user.has_perm("INVENTORY_REQUEST_APPROVE")]
    if row.approval_stage == "ADMIN":
        configured = _setting("INVENTORY_ADMIN_MANAGER_USER_ID")
        if configured:
            return [configured]
        return [user.id for user in User.query.all() if user.has_perm("STORE_MANAGE")]
    return []


def _can_process(row):
    if row.status != "SUBMITTED":
        return False
    if current_user.has_perm("STORE_MANAGE"):
        return True
    if row.approval_stage == "MANAGER":
        return row.manager_user_id == current_user.id
    if row.approval_stage == "WAREHOUSE":
        configured = _setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID")
        return current_user.id == configured or current_user.has_perm("INVENTORY_REQUEST_APPROVE")
    if row.approval_stage == "ADMIN":
        return current_user.id == _setting("INVENTORY_ADMIN_MANAGER_USER_ID")
    return False


def _can_manage():
    configured_ids = {
        _setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID"),
        _setting("INVENTORY_ADMIN_MANAGER_USER_ID"),
    }
    return current_user.id in configured_ids or current_user.has_perm("INVENTORY_REQUEST_APPROVE") or current_user.has_perm("STORE_MANAGE")


def _can_view(row):
    return row.requester_user_id == current_user.id or _can_manage() or _can_process(row) or any(
        action.actor_user_id == current_user.id for action in row.actions
    )


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


def _catalog_context():
    items = InvItem.query.filter(InvItem.is_active.is_(True)).order_by(InvItem.name.asc()).all()
    categories = InvItemCategory.query.filter(InvItemCategory.is_active.is_(True)).order_by(InvItemCategory.name.asc()).all()
    warehouses = InvWarehouse.query.filter(InvWarehouse.is_active.is_(True)).order_by(InvWarehouse.name.asc()).all()
    balances = _inventory_balances()
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
        admin_manager_id = request.form.get("admin_manager_user_id") or ""
        _set_setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID", warehouse_manager_id)
        _set_setting("INVENTORY_ADMIN_MANAGER_USER_ID", admin_manager_id)
        _grant_permission(int(warehouse_manager_id) if warehouse_manager_id.isdigit() else None, "INVENTORY_REQUEST_APPROVE")
        _grant_permission(int(warehouse_manager_id) if warehouse_manager_id.isdigit() else None, "PORTAL_REPORTS_READ")
        _grant_permission(int(admin_manager_id) if admin_manager_id.isdigit() else None, "PORTAL_REPORTS_READ")
        db.session.commit()
        flash("تم حفظ مسؤولي اعتماد طلبات المواد.", "success")
        return redirect(url_for("portal.inventory_request_settings"))
    return render_template(
        "portal/inventory/request_settings.html",
        users=User.query.order_by(User.name.asc()).all(),
        warehouse_manager_id=_setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID"),
        admin_manager_id=_setting("INVENTORY_ADMIN_MANAGER_USER_ID"),
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
        pending_count=len(rows),
        tasks=True,
    )


@portal_bp.route("/inventory/employee-requests/new", methods=["GET", "POST"])
@login_required
def inventory_employee_request_new():
    items, categories, warehouses, item_totals, warehouse_balances = _catalog_context()
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
            )
        employee = EmployeeFile.query.get(current_user.id)
        manager_id = employee.direct_manager_user_id if employee else None
        row = InvEmployeeRequest(
            requester_user_id=current_user.id,
            manager_user_id=manager_id,
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
    )


@portal_bp.route("/inventory/employee-requests/<int:request_id>", methods=["GET", "POST"])
@login_required
def inventory_employee_request_view(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_view(row):
        abort(403)
    items, categories, warehouses, item_totals, warehouse_balances = _catalog_context()
    can_edit = row.status == "SUBMITTED" and (row.requester_user_id == current_user.id or _can_manage())
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
            row.approval_stage = "MANAGER" if row.manager_user_id else "WAREHOUSE"
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
    )


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
        row.approval_stage = "ADMIN"
    else:
        try:
            _create_issue_vouchers(row)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        row.status = "APPROVED"
        row.approval_stage = "DONE"
        row.decided_at = datetime.utcnow()
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
