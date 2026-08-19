from datetime import datetime

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import portal_bp
from extensions import db
from models import (EmployeeFile, InvEmployeeRequest, InvEmployeeRequestAction,
                    Message, MessageRecipient, Notification, SystemSetting, User)
from utils.perms import perm_required


STAGES = {"MANAGER": "المدير المباشر", "WAREHOUSE": "مدير المستودع", "ADMIN": "مدير الشؤون الإدارية", "DONE": "مكتمل"}


def _setting(key):
    row = SystemSetting.query.filter_by(key=key).first()
    return int(row.value) if row and (row.value or '').isdigit() else None


def _set_setting(key, value):
    row = SystemSetting.query.filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.session.add(SystemSetting(key=key, value=value))


def _recipient(row):
    if row.approval_stage == 'MANAGER': return row.manager_user_id
    if row.approval_stage == 'WAREHOUSE': return _setting('INVENTORY_WAREHOUSE_MANAGER_USER_ID')
    if row.approval_stage == 'ADMIN': return _setting('INVENTORY_ADMIN_MANAGER_USER_ID')


def _can_process(row):
    if row.status != 'SUBMITTED': return False
    return _recipient(row) == current_user.id or current_user.has_perm('INVENTORY_REQUEST_APPROVE') or current_user.has_perm('STORE_MANAGE')


def _can_manage():
    return current_user.id in {_setting('INVENTORY_WAREHOUSE_MANAGER_USER_ID'), _setting('INVENTORY_ADMIN_MANAGER_USER_ID')} or current_user.has_perm('INVENTORY_REQUEST_APPROVE') or current_user.has_perm('STORE_MANAGE')


def _can_view(row):
    return row.requester_user_id == current_user.id or _can_manage() or _can_process(row) or any(action.actor_user_id == current_user.id for action in row.actions)


def _notify(row, user_id, text):
    if not user_id or user_id == current_user.id: return
    link = url_for('portal.inventory_employee_request_view', request_id=row.id)
    db.session.add(Notification(user_id=user_id, message=text, type='INFO', source='portal', link_url=link, is_read=False, created_at=datetime.utcnow()))
    message = Message(sender_id=current_user.id, subject=f'طلب مواد #{row.id}', body=f'{text}\n{link}', target_kind='USER', target_id=user_id, created_at=datetime.utcnow())
    db.session.add(message); db.session.flush()
    db.session.add(MessageRecipient(message_id=message.id, recipient_user_id=user_id))


@portal_bp.route('/admin/inventory-request-settings', methods=['GET', 'POST'])
@login_required
@perm_required('PORTAL_ADMIN_PERMISSIONS_MANAGE')
def inventory_request_settings():
    if request.method == 'POST':
        _set_setting('INVENTORY_WAREHOUSE_MANAGER_USER_ID', request.form.get('warehouse_manager_user_id') or '')
        _set_setting('INVENTORY_ADMIN_MANAGER_USER_ID', request.form.get('admin_manager_user_id') or '')
        db.session.commit(); flash('تم حفظ مسؤولي اعتماد طلبات المواد.', 'success')
        return redirect(url_for('portal.inventory_request_settings'))
    return render_template('portal/inventory/request_settings.html', users=User.query.order_by(User.name.asc()).all(), warehouse_manager_id=_setting('INVENTORY_WAREHOUSE_MANAGER_USER_ID'), admin_manager_id=_setting('INVENTORY_ADMIN_MANAGER_USER_ID'))


@portal_bp.route('/inventory/employee-requests')
@login_required
def inventory_employee_requests():
    query = InvEmployeeRequest.query
    if not _can_manage(): query = query.filter_by(requester_user_id=current_user.id)
    return render_template('portal/inventory/employee_requests.html', rows=query.order_by(InvEmployeeRequest.created_at.desc()).all(), stages=STAGES, can_manage=_can_manage())


@portal_bp.route('/inventory/employee-requests/new', methods=['GET', 'POST'])
@login_required
def inventory_employee_request_new():
    if request.method == 'POST':
        items, purpose = (request.form.get('items_text') or '').strip(), (request.form.get('purpose') or '').strip()
        if not items or not purpose:
            flash('أدخل المواد المطلوبة وسبب الطلب.', 'danger'); return redirect(request.url)
        employee = EmployeeFile.query.get(current_user.id)
        manager_id = employee.direct_manager_user_id if employee else None
        row = InvEmployeeRequest(requester_user_id=current_user.id, manager_user_id=manager_id, items_text=items, purpose=purpose, note=(request.form.get('note') or '').strip() or None, approval_stage='MANAGER' if manager_id else 'WAREHOUSE')
        db.session.add(row); db.session.flush()
        db.session.add(InvEmployeeRequestAction(request_id=row.id, stage=row.approval_stage, action='SUBMITTED', actor_user_id=current_user.id, note='تم إرسال الطلب.'))
        _notify(row, _recipient(row), f'طلب مواد #{row.id} بانتظار متابعتك لدى {STAGES[row.approval_stage]}.')
        db.session.commit(); return redirect(url_for('portal.inventory_employee_request_view', request_id=row.id))
    return render_template('portal/inventory/employee_request_form.html', item=None)


@portal_bp.route('/inventory/employee-requests/<int:request_id>', methods=['GET', 'POST'])
@login_required
def inventory_employee_request_view(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_view(row): abort(403)
    if request.method == 'POST':
        if not (row.requester_user_id == current_user.id or _can_manage()): abort(403)
        row.items_text, row.purpose, row.note = (request.form.get('items_text') or '').strip(), (request.form.get('purpose') or '').strip(), (request.form.get('note') or '').strip() or None
        db.session.add(InvEmployeeRequestAction(request_id=row.id, stage=row.approval_stage, action='UPDATED', actor_user_id=current_user.id, note='تم تعديل الطلب.')); db.session.commit()
        flash('تم حفظ التعديل.', 'success'); return redirect(request.url)
    return render_template('portal/inventory/employee_request_view.html', item=row, stages=STAGES, can_process=_can_process(row), can_edit=row.requester_user_id == current_user.id or _can_manage())


@portal_bp.route('/inventory/employee-requests/<int:request_id>/approve', methods=['POST'])
@login_required
def inventory_employee_request_approve(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_process(row): abort(403)
    note = (request.form.get('note') or '').strip() or None
    db.session.add(InvEmployeeRequestAction(request_id=row.id, stage=row.approval_stage, action='APPROVED', actor_user_id=current_user.id, note=note))
    if row.approval_stage == 'MANAGER': row.approval_stage = 'WAREHOUSE'
    elif row.approval_stage == 'WAREHOUSE': row.approval_stage = 'ADMIN'
    else: row.status, row.approval_stage, row.decided_at = 'APPROVED', 'DONE', datetime.utcnow()
    _notify(row, _recipient(row) if row.status == 'SUBMITTED' else row.requester_user_id, f'تم اعتماد طلب المواد #{row.id}' + (f'، بانتظار {STAGES[row.approval_stage]}.' if row.status == 'SUBMITTED' else ' نهائياً.'))
    db.session.commit(); return redirect(url_for('portal.inventory_employee_request_view', request_id=row.id))


@portal_bp.route('/inventory/employee-requests/report')
@login_required
def inventory_employee_requests_report():
    if not _can_manage(): abort(403)
    return render_template('portal/inventory/employee_requests.html', rows=InvEmployeeRequest.query.order_by(InvEmployeeRequest.created_at.desc()).all(), stages=STAGES, can_manage=True, report=True)
