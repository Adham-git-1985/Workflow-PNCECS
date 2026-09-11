from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf.csrf import validate_csrf
from wtforms.validators import ValidationError
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import InvAssetDocument, InvAssetDocumentLine, InvFixedAsset, InvFixedAssetCycle
from services.asset_custody import (KINDS, STATUSES, cancel_document, create_movement,
                                    issue_document, review_document, send_cycle_reports)
from . import portal_bp
from .fixed_assets import _audit, _choices, _optional_int
from .routes import STORE_MANAGE, _perm_any


@portal_bp.before_request
def protect_asset_actions():
    endpoints = {"asset_send_reports", "asset_document", "asset_movement", "fixed_asset_mobile_settings", "fixed_asset_cycle_scan"}
    if (request.method == "POST" and (request.endpoint or "").split(".")[-1] in endpoints
            and current_user.is_authenticated and current_app.config.get("WTF_CSRF_ENABLED", True)):
        try:
            validate_csrf(request.form.get("csrf_token"))
        except ValidationError:
            abort(400, description="انتهت صلاحية النموذج؛ حدّث الصفحة وحاول مجدداً.")


def _visible_document(document_id):
    doc = InvAssetDocument.query.get_or_404(document_id)
    if not current_user.has_perm(STORE_MANAGE) and doc.employee_id != current_user.id:
        # A former holder may see the movement of their own asset only.
        if not all(line.snapshot["state"]["custodian_user_id"] == current_user.id for line in doc.lines):
            abort(403)
    return doc


@portal_bp.route("/inventory/fixed-assets/documents")
@login_required
def asset_documents():
    manager = current_user.has_perm(STORE_MANAGE)
    query = InvAssetDocument.query
    if not manager:
        query = query.filter_by(employee_id=current_user.id)
    cycle_id = _optional_int(request.args.get("cycle_id"))
    if cycle_id:
        query = query.filter_by(cycle_id=cycle_id)
    employee_id = _optional_int(request.args.get("employee_id"))
    if manager and employee_id:
        query = query.filter_by(employee_id=employee_id)
    status = request.args.get("status", "")
    if status in STATUSES:
        query = query.filter_by(status=status)
    assets = InvFixedAsset.query.filter_by(custodian_user_id=current_user.id).order_by(InvFixedAsset.asset_tag).all()
    return render_template("portal/inventory/fixed_assets/documents.html",
        documents=query.order_by(InvAssetDocument.id.desc()).paginate(per_page=30),
        assets=assets, kinds=KINDS, statuses=STATUSES, manager=manager, selected_status=status)


@portal_bp.route("/inventory/fixed-assets/cycles/<int:cycle_id>/send-reports", methods=["POST"])
@login_required
@_perm_any(STORE_MANAGE)
def asset_send_reports(cycle_id):
    cycle = InvFixedAssetCycle.query.get_or_404(cycle_id)
    try:
        docs = send_cycle_reports(cycle, current_user.id)
        _audit("INV_ASSET_REPORT_SEND", f"إرسال {len(docs)} تقرير من الدورة {cycle.code}", "INV_FIXED_ASSET_CYCLE", cycle.id)
        db.session.commit()
        flash(f"تم إرسال {len(docs)} تقرير للموظفين للاعتماد.", "success")
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else "يوجد إجراء متزامن على الأصل؛ حدّث الصفحة.", "danger")
    return redirect(url_for("portal.asset_documents", cycle_id=cycle.id))


@portal_bp.route("/inventory/fixed-assets/documents/<int:document_id>", methods=["GET", "POST"])
@login_required
def asset_document(document_id):
    doc = _visible_document(document_id)
    manager = current_user.has_perm(STORE_MANAGE)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "review":
            if current_user.id != doc.employee_id:
                abort(403)
        elif action in {"issue", "cancel"}:
            if not manager:
                abort(403)
        else:
            abort(400)
        try:
            if action == "review":
                responses = {line.id: (request.form.get(f"decision_{line.id}"), request.form.get(f"note_{line.id}")) for line in doc.lines}
                review_document(doc, current_user.id, responses)
            elif action == "issue":
                issue_document(doc, current_user.id)
            else:
                cancel_document(doc)
            _audit("INV_ASSET_DOCUMENT_" + action.upper(), f"{KINDS[doc.kind]} #{doc.id}: {doc.status}", "INV_ASSET_DOCUMENT", doc.id)
            db.session.commit()
            flash("تم حفظ الإجراء وإشعار المعنيين.", "success")
            return redirect(url_for("portal.asset_document", document_id=doc.id))
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "يوجد إجراء متزامن على الأصل؛ حدّث الصفحة.", "danger")
    return render_template("portal/inventory/fixed_assets/document.html", doc=doc, manager=manager,
        kinds=KINDS, statuses=STATUSES, can_review=doc.employee_id == current_user.id and doc.status == "PENDING")


@portal_bp.route("/inventory/fixed-assets/assets/<int:asset_id>/movement", methods=["GET", "POST"])
@login_required
@_perm_any(STORE_MANAGE)
def asset_movement(asset_id):
    asset = InvFixedAsset.query.get_or_404(asset_id)
    if request.method == "POST":
        try:
            doc = create_movement(asset, request.form.get("kind"), _optional_int(request.form.get("employee_id")),
                                  current_user.id, request.form.get("reason"))
            _audit("INV_ASSET_MOVEMENT", f"{KINDS[doc.kind]} للأصل {asset.asset_tag}", "INV_ASSET_DOCUMENT", doc.id)
            db.session.commit()
            return redirect(url_for("portal.asset_document", document_id=doc.id))
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "يوجد إجراء متزامن على الأصل؛ حدّث الصفحة.", "danger")
    return render_template("portal/inventory/fixed_assets/movement.html", asset=asset, kinds=KINDS, **_choices())
