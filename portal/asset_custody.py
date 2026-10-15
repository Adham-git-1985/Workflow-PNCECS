from io import BytesIO
from pathlib import Path
import secrets
from datetime import datetime

from flask import abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from flask_wtf.csrf import validate_csrf
from PIL import Image
from wtforms.validators import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import noload
from werkzeug.utils import secure_filename

from extensions import db
from models import (
    InvAssetDocument,
    InvAssetDocumentLine,
    InvFixedAsset,
    InvFixedAssetCycle,
    InvFixedAssetCycleMember,
    InvFixedAssetCommitteeReport,
    InvFixedAssetPreviousLine,
    User,
)
from services.asset_custody import (COMMITTEE_REPORT_STATUSES, EMPLOYEE_RESPONSE_LABELS,
                                    KINDS, STATUSES, cancel_document, create_movement,
                                    decide_committee_report,
                                    escalate_inventory_rejection, get_or_create_committee_report,
                                    inventory_documents_for_cycle, issue_document,
                                    review_document, review_inventory_document,
                                    revise_inventory_document, save_manual_previous_lines,
                                    send_cycle_reports, submit_committee_report)
from services.fixed_asset_reports import build_committee_report_pdf, build_committee_variance_index
from services.fixed_asset_inventory import ASSET_CONDITIONS, ASSET_LIFECYCLE_STATUSES, INVENTORY_RESULT_LABELS
from . import portal_bp
from .fixed_assets import _audit, _choices, _optional_int
from .routes import STORE_MANAGE, _perm_any


REPORT_HEADER_MAX_BYTES = 8 * 1024 * 1024
REPORT_HEADER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@portal_bp.before_request
def protect_asset_actions():
    endpoints = {
        "asset_send_reports",
        "asset_document",
        "asset_committee_report",
        "asset_committee_report_header",
        "asset_committee_report_previous",
        "asset_committee_report_decision",
        "asset_movement",
        "fixed_asset_cycle_scan",
        "fixed_asset_mobile_device_approve",
        "fixed_asset_mobile_device_revoke",
    }
    if (request.method == "POST" and (request.endpoint or "").split(".")[-1] in endpoints
            and current_user.is_authenticated and current_app.config.get("WTF_CSRF_ENABLED", True)):
        try:
            validate_csrf(request.form.get("csrf_token"))
        except ValidationError:
            abort(400, description="انتهت صلاحية النموذج؛ حدّث الصفحة وحاول مجدداً.")


def _committee_member_cycle_ids(user_id):
    return {
        row.cycle_id
        for row in InvFixedAssetCycleMember.query.filter_by(user_id=user_id).all()
    }


def _can_committee_act(cycle):
    return bool(
        current_user.has_perm(STORE_MANAGE)
        or any(member.user_id == current_user.id for member in (cycle.members or []))
    )


def _is_secretary_general():
    if not current_user.is_authenticated:
        return False
    try:
        role = (current_user.role or "").upper().replace("-", "_").replace(" ", "_")
    except Exception:
        role = ""
    return bool(
        current_user.has_perm("INVENTORY_FIXED_ASSET_REPORT_APPROVE")
        or current_user.has_perm("INVENTORY_SECRETARY_GENERAL_APPROVE")
        or "SECRETARY_GENERAL" in role
        or role in {"SECRETARY", "GENERAL_SECRETARY"}
    )


def _report_header_dir():
    path = Path(current_app.instance_path) / "uploads" / "fixed_asset_reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_report_header(upload):
    if upload is None or not upload.filename:
        return None
    original_name = secure_filename(upload.filename)
    extension = Path(original_name).suffix.lower()
    if extension not in REPORT_HEADER_EXTENSIONS:
        raise ValueError("الترويسة يجب أن تكون صورة PNG أو JPG أو WEBP.")
    payload = upload.read(REPORT_HEADER_MAX_BYTES + 1)
    if len(payload) > REPORT_HEADER_MAX_BYTES:
        raise ValueError("حجم الترويسة يجب ألا يتجاوز 8 ميغابايت.")
    try:
        image = Image.open(BytesIO(payload))
        image.verify()
    except Exception as exc:
        raise ValueError("ملف الترويسة ليس صورة صالحة.") from exc
    filename = f"committee-header-{secrets.token_urlsafe(18)}{extension}"
    target = _report_header_dir() / filename
    target.write_bytes(payload)
    return filename


def _report_header_path(report):
    if not report or not report.header_filename:
        return None
    path = _report_header_dir() / secure_filename(report.header_filename)
    return path if path.is_file() else None


def _report_previous_rows(cycle_id):
    rows = InvFixedAssetPreviousLine.query.filter_by(cycle_id=cycle_id).order_by(
        InvFixedAssetPreviousLine.employee_id.asc(), InvFixedAssetPreviousLine.id.asc()
    ).all()
    result = {}
    for row in rows:
        result.setdefault(row.employee_id, []).append(row)
    return result


def _visible_document(document_id):
    doc = InvAssetDocument.query.get_or_404(document_id)
    committee_actor = bool(doc.cycle and _can_committee_act(doc.cycle))
    if not current_user.has_perm(STORE_MANAGE) and not committee_actor and doc.employee_id != current_user.id:
        # A former holder may see the movement of their own asset only.
        if not all(line.snapshot["state"]["custodian_user_id"] == current_user.id for line in doc.lines):
            abort(403)
    return doc


@portal_bp.route("/inventory/fixed-assets/documents")
@login_required
def asset_documents():
    manager = current_user.has_perm(STORE_MANAGE)
    committee_cycle_ids = _committee_member_cycle_ids(current_user.id)
    query = InvAssetDocument.query
    if not manager:
        if committee_cycle_ids:
            query = query.filter(
                (InvAssetDocument.employee_id == current_user.id)
                | (InvAssetDocument.cycle_id.in_(committee_cycle_ids))
            )
        else:
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
        assets=assets, kinds=KINDS, statuses=STATUSES, manager=manager,
        committee_cycle_ids=committee_cycle_ids, selected_status=status)


@portal_bp.route("/inventory/fixed-assets/cycles/<int:cycle_id>/send-reports", methods=["POST"])
@login_required
def asset_send_reports(cycle_id):
    cycle = InvFixedAssetCycle.query.get_or_404(cycle_id)
    if not (current_user.has_perm(STORE_MANAGE) or _can_committee_act(cycle)):
        abort(403)
    try:
        docs = send_cycle_reports(cycle, current_user.id)
        _audit("INV_ASSET_REPORT_SEND", f"إرسال {len(docs)} تقرير من الدورة {cycle.code}", "INV_FIXED_ASSET_CYCLE", cycle.id)
        db.session.commit()
        flash(f"تم إرسال {len(docs)} تقرير للموظفين للاعتماد.", "success")
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else "يوجد إجراء متزامن على الأصل؛ حدّث الصفحة.", "danger")
    return redirect(url_for("portal.asset_committee_report", cycle_id=cycle.id))


@portal_bp.route("/inventory/fixed-assets/documents/<int:document_id>", methods=["GET", "POST"])
@login_required
def asset_document(document_id):
    doc = _visible_document(document_id)
    manager = current_user.has_perm(STORE_MANAGE)
    committee_actor = bool(doc.cycle and _can_committee_act(doc.cycle))
    if request.method == "POST":
        action = request.form.get("action")
        if action == "review":
            if current_user.id != doc.employee_id:
                abort(403)
        elif action == "revise":
            if not committee_actor:
                abort(403)
        elif action == "escalate":
            if not committee_actor:
                abort(403)
        elif action in {"issue", "cancel"}:
            if not manager:
                abort(403)
        else:
            abort(400)
        try:
            if action == "review":
                if doc.kind == "INVENTORY":
                    inventory_action = request.form.get("inventory_action")
                    if inventory_action:
                        review_inventory_document(
                            doc,
                            current_user.id,
                            inventory_action,
                            request.form.get("employee_note"),
                        )
                    else:
                        # Compatibility with the previous per-line review
                        # form used by existing bookmarked pages and clients.
                        responses = {line.id: (request.form.get(f"decision_{line.id}"), request.form.get(f"note_{line.id}")) for line in doc.lines}
                        review_document(doc, current_user.id, responses)
                else:
                    responses = {line.id: (request.form.get(f"decision_{line.id}"), request.form.get(f"note_{line.id}")) for line in doc.lines}
                    review_document(doc, current_user.id, responses)
            elif action == "revise":
                updates = {}
                for line in doc.lines:
                    updates[line.id] = {
                        "tag": request.form.get(f"tag_{line.id}"),
                        "name": request.form.get(f"name_{line.id}"),
                        "serial": request.form.get(f"serial_{line.id}"),
                        "location": request.form.get(f"location_{line.id}"),
                        "condition": request.form.get(f"condition_{line.id}"),
                        "target_lifecycle": request.form.get(f"lifecycle_{line.id}"),
                        "inventory_note": request.form.get(f"inventory_note_{line.id}"),
                        "included": bool(request.form.get(f"include_{line.id}")),
                    }
                revise_inventory_document(
                    doc,
                    current_user.id,
                    updates,
                    request.form.get("committee_revision_note"),
                )
            elif action == "escalate":
                escalate_inventory_rejection(doc, current_user.id, request.form.get("escalation_note"))
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
    return render_template(
        "portal/inventory/fixed_assets/document.html",
        doc=doc,
        committee_report=doc.cycle.committee_report if doc.cycle else None,
        manager=manager,
        committee_actor=committee_actor,
        kinds=KINDS,
        statuses=STATUSES,
        can_review=doc.employee_id == current_user.id and doc.status == "PENDING",
        can_revise=committee_actor and doc.kind == "INVENTORY" and doc.status == "CHANGE_REQUESTED",
        can_escalate=committee_actor and doc.kind == "INVENTORY" and doc.status == "REJECTED",
        asset_conditions=ASSET_CONDITIONS,
        asset_lifecycle_statuses=ASSET_LIFECYCLE_STATUSES,
        events=doc.events,
    )


def _report_access(cycle):
    return _can_committee_act(cycle) or _is_secretary_general()


@portal_bp.route("/inventory/fixed-assets/cycles/<int:cycle_id>/committee-report", methods=["GET", "POST"])
@login_required
def asset_committee_report(cycle_id):
    cycle = InvFixedAssetCycle.query.options(
        noload(InvFixedAssetCycle.entries),
        noload(InvFixedAssetCycle.manual_previous_lines),
    ).get_or_404(cycle_id)
    committee_actor = _can_committee_act(cycle)
    secretary_actor = _is_secretary_general()
    if not (committee_actor or secretary_actor):
        abort(403)
    report = InvFixedAssetCommitteeReport.query.filter_by(cycle_id=cycle.id).first()
    if report is None and committee_actor:
        report = get_or_create_committee_report(cycle, current_user.id)
        db.session.commit()
    if report is None:
        abort(404)

    if request.method == "POST":
        action = (request.form.get("action") or "save").strip().lower()
        try:
            if action in {"save", "submit"}:
                if not committee_actor:
                    abort(403)
                if report.status not in {"DRAFT", "RETURNED"}:
                    raise ValueError("لا يمكن تعديل التقرير بعد رفعه أو اعتماده.")
                report.introduction = (request.form.get("introduction") or "").strip() or None
                report.points = (request.form.get("points") or "").strip() or None
                report.summary = (request.form.get("summary") or "").strip() or None
                header_filename = _save_report_header(request.files.get("header"))
                if header_filename:
                    report.header_filename = header_filename
                report.updated_at = datetime.utcnow()
                if action == "submit":
                    submit_committee_report(report, current_user.id)
                    _audit(
                        "INV_FIXED_ASSET_COMMITTEE_REPORT_SUBMIT",
                        f"رفع تقرير اللجنة للدورة {cycle.code}",
                        "INV_FIXED_ASSET_COMMITTEE_REPORT",
                        report.id,
                    )
                    flash("تم رفع تقرير اللجنة إلى الأمين العام.", "success")
                else:
                    flash("تم حفظ إعدادات تقرير اللجنة.", "success")
            elif action in {"approve", "return"}:
                if not secretary_actor:
                    abort(403)
                decide_committee_report(report, current_user.id, "APPROVE" if action == "approve" else "RETURN", request.form.get("secretary_note"))
                _audit(
                    "INV_FIXED_ASSET_COMMITTEE_REPORT_" + action.upper(),
                    f"قرار تقرير اللجنة للدورة {cycle.code}",
                    "INV_FIXED_ASSET_COMMITTEE_REPORT",
                    report.id,
                )
                if action == "approve" and cycle.successor_cycle:
                    flash(
                        f"تم اعتماد تقرير اللجنة وإغلاق دورة {cycle.code} نهائياً، وفتح دورة الجرد الجديدة {cycle.successor_cycle.code}.",
                        "success",
                    )
                else:
                    flash("تم حفظ قرار الأمين العام.", "success")
            else:
                abort(400)
            db.session.commit()
            return redirect(url_for("portal.asset_committee_report", cycle_id=cycle.id))
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "تعذر حفظ تقرير اللجنة بسبب إجراء متزامن.", "danger")

    documents = inventory_documents_for_cycle(cycle.id)
    previous_by_employee = _report_previous_rows(cycle.id)
    employees = []
    seen = set()
    for doc in documents:
        if doc.employee_id not in seen:
            seen.add(doc.employee_id)
            employees.append(doc.employee)
    return render_template(
        "portal/inventory/fixed_assets/committee_report.html",
        cycle=cycle,
        report=report,
        documents=documents,
        employees=employees,
        previous_by_employee=previous_by_employee,
        committee_actor=committee_actor,
        secretary_actor=secretary_actor,
        report_statuses=COMMITTEE_REPORT_STATUSES,
        statuses=STATUSES,
        employee_response_labels=EMPLOYEE_RESPONSE_LABELS,
    )


@portal_bp.route("/inventory/fixed-assets/cycles/<int:cycle_id>/committee-report/header")
@login_required
def asset_committee_report_header(cycle_id):
    cycle = InvFixedAssetCycle.query.options(
        noload(InvFixedAssetCycle.entries),
        noload(InvFixedAssetCycle.manual_previous_lines),
    ).get_or_404(cycle_id)
    if not _report_access(cycle):
        abort(403)
    report = InvFixedAssetCommitteeReport.query.filter_by(cycle_id=cycle.id).first_or_404()
    path = _report_header_path(report)
    if path is None:
        abort(404)
    response = send_file(path, conditional=True)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@portal_bp.route("/inventory/fixed-assets/cycles/<int:cycle_id>/committee-report/previous/<int:employee_id>", methods=["GET", "POST"])
@login_required
def asset_committee_report_previous(cycle_id, employee_id):
    cycle = InvFixedAssetCycle.query.options(
        noload(InvFixedAssetCycle.entries),
        noload(InvFixedAssetCycle.manual_previous_lines),
    ).get_or_404(cycle_id)
    if not _can_committee_act(cycle):
        abort(403)
    employee = User.query.get_or_404(employee_id)
    report = InvFixedAssetCommitteeReport.query.filter_by(cycle_id=cycle.id).first()
    if report is None:
        report = get_or_create_committee_report(cycle, current_user.id)
    if report.status not in {"DRAFT", "RETURNED"}:
        flash("لا يمكن تعديل بيانات السنة السابقة بعد رفع التقرير.", "warning")
        return redirect(url_for("portal.asset_committee_report", cycle_id=cycle.id))

    lines = InvFixedAssetPreviousLine.query.filter_by(
        cycle_id=cycle.id,
        employee_id=employee.id,
    ).order_by(InvFixedAssetPreviousLine.id.asc()).all()
    if request.method == "POST":
        try:
            count = save_manual_previous_lines(cycle, employee.id, request.form.get("previous_data"), current_user.id)
            db.session.commit()
            flash(f"تم حفظ {count} بنداً من بيانات السنة السابقة للموظف.", "success")
            return redirect(url_for("portal.asset_committee_report", cycle_id=cycle.id))
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "تعذر حفظ البيانات السابقة.", "danger")
    initial_text = "\n".join(
        " | ".join([
            row.asset_tag,
            (row.snapshot or {}).get("name") or "",
            (row.snapshot or {}).get("serial") or "",
            (row.snapshot or {}).get("location") or "",
            (row.snapshot or {}).get("condition") or "",
            (row.snapshot or {}).get("note") or "",
        ])
        for row in lines
    )
    return render_template(
        "portal/inventory/fixed_assets/committee_previous.html",
        cycle=cycle,
        report=report,
        employee=employee,
        initial_text=initial_text,
    )


@portal_bp.route("/inventory/fixed-assets/cycles/<int:cycle_id>/committee-report/print")
@login_required
def asset_committee_report_print(cycle_id):
    cycle = InvFixedAssetCycle.query.options(
        noload(InvFixedAssetCycle.entries),
        noload(InvFixedAssetCycle.manual_previous_lines),
    ).get_or_404(cycle_id)
    if not _report_access(cycle):
        abort(403)
    report = InvFixedAssetCommitteeReport.query.filter_by(cycle_id=cycle.id).first_or_404()
    documents = inventory_documents_for_cycle(cycle.id)
    employees = []
    seen_employee_ids = set()
    for document in documents:
        if document.employee_id not in seen_employee_ids:
            seen_employee_ids.add(document.employee_id)
            employees.append(document.employee)
    previous_by_employee = _report_previous_rows(cycle.id)
    return render_template(
        "portal/inventory/fixed_assets/committee_report_print.html",
        cycle=cycle,
        report=report,
        documents=documents,
        employees=employees,
        previous_by_employee=previous_by_employee,
        variance_by_employee=build_committee_variance_index(
            cycle,
            documents,
            previous_by_employee,
            condition_labels=ASSET_CONDITIONS,
            lifecycle_labels=ASSET_LIFECYCLE_STATUSES,
            result_labels=INVENTORY_RESULT_LABELS,
        ),
        statuses=STATUSES,
        condition_labels=ASSET_CONDITIONS,
        lifecycle_labels=ASSET_LIFECYCLE_STATUSES,
        result_labels=INVENTORY_RESULT_LABELS,
        employee_response_labels=EMPLOYEE_RESPONSE_LABELS,
    )


@portal_bp.route("/inventory/fixed-assets/cycles/<int:cycle_id>/committee-report.pdf")
@login_required
def asset_committee_report_pdf(cycle_id):
    cycle = InvFixedAssetCycle.query.options(
        noload(InvFixedAssetCycle.entries),
        noload(InvFixedAssetCycle.manual_previous_lines),
    ).get_or_404(cycle_id)
    if not _report_access(cycle):
        abort(403)
    report = InvFixedAssetCommitteeReport.query.filter_by(cycle_id=cycle.id).first_or_404()
    output = build_committee_report_pdf(
        report,
        inventory_documents_for_cycle(cycle.id),
        _report_previous_rows(cycle.id),
        header_path=_report_header_path(report),
        condition_labels=ASSET_CONDITIONS,
        lifecycle_labels=ASSET_LIFECYCLE_STATUSES,
        result_labels=INVENTORY_RESULT_LABELS,
    )
    return send_file(
        output,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"fixed-asset-committee-report-{cycle.code}.pdf",
    )


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
