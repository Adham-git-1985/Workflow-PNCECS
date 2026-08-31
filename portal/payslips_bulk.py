from __future__ import annotations

import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import fitz  # PyMuPDF
from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import case, func
from werkzeug.utils import secure_filename

from . import portal_bp
from extensions import db
from models import EmployeeAttachment, EmployeeFile, User
from utils.perms import perm_required
from services.employee_attachment_archive import sync_employee_attachment_to_archive

# Reuse the existing HR payslip/storage helpers from portal.routes so the new page
# stays connected to the old send/publish workflow.
from .routes import (  # noqa: E402
    HR_EMP_ATTACH,
    _employee_upload_dir,
    _ensure_employee_attachment_payslip_schema,
    _portal_audit,
)


_ARABIC_DIGITS_MAP = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def _normalize_digits(value: str | None) -> str:
    return (value or "").translate(_ARABIC_DIGITS_MAP)


def _digits_only(value: str | None) -> str:
    return "".join(re.findall(r"\d+", _normalize_digits(value or "")))


def _extract_identity_number(page_text: str | None) -> str | None:
    """Extract the 9-digit ID from a Palestinian Ministry of Finance payslip page."""
    text = _normalize_digits(page_text or "")
    text = re.sub(r"\s+", " ", text)

    patterns = [
        r"رقم\s*الهوية\s*[:：]?\s*([0-9]{9})",
        r"رقم\s*الهويه\s*[:：]?\s*([0-9]{9})",
        r"الهوية\s*[:：]?\s*([0-9]{9})",
        r"الهويه\s*[:：]?\s*([0-9]{9})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    # In some Arabic PDFs, text extraction may join fields without spaces.
    # Fallback: first 9-digit number near the top of the page.
    top_text = text[:1200]
    ids = re.findall(r"(?<!\d)([0-9]{9})(?!\d)", top_text)
    return ids[0] if ids else None


def _payslip_batch_dir(batch_id: str) -> Path:
    safe_batch_id = secure_filename(batch_id) or datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(current_app.instance_path) / "uploads" / "payslips" / safe_batch_id


def _unique_pdf_path(output_dir: Path, identity_number: str) -> Path:
    safe_id = secure_filename(identity_number) or "NO_ID"
    target = output_dir / f"{safe_id}.pdf"
    counter = 2
    while target.exists():
        target = output_dir / f"{safe_id}_{counter}.pdf"
        counter += 1
    return target


def _find_employee_by_identity(identity_number: str | None) -> EmployeeFile | None:
    """Find employee by EmployeeFile.national_id, allowing spaces/dashes/Arabic digits."""
    wanted = _digits_only(identity_number)
    if not wanted:
        return None

    # Fast exact-ish checks first.
    try:
        emp = (
            EmployeeFile.query
            .filter(func.trim(EmployeeFile.national_id) == wanted)
            .first()
        )
        if emp:
            return emp
    except Exception:
        pass

    # Robust fallback for values saved with separators or Arabic/Persian digits.
    try:
        rows = EmployeeFile.query.filter(EmployeeFile.national_id.isnot(None)).all()
        for emp in rows:
            if _digits_only(getattr(emp, "national_id", None)) == wanted:
                return emp
    except Exception:
        return None

    return None


def _employee_display_name(emp: EmployeeFile | None, user: User | None = None) -> str:
    if emp is None:
        return ""
    return (
        getattr(emp, "full_name_quad", None)
        or (getattr(user, "full_name", None) if user else None)
        or (getattr(user, "name", None) if user else None)
        or (getattr(user, "email", None) if user else None)
        or ""
    )


def _recent_payslip_months(limit: int = 12) -> list[dict]:
    """Return recent months that have payslips saved in EmployeeAttachment."""
    _ensure_employee_attachment_payslip_schema()
    out: list[dict] = []
    try:
        rows = (
            db.session.query(
                EmployeeAttachment.payslip_year.label("year"),
                EmployeeAttachment.payslip_month.label("month"),
                func.count(EmployeeAttachment.id).label("total"),
                func.sum(
                    case((EmployeeAttachment.is_published == False, 1), else_=0)  # noqa: E712
                ).label("drafts"),
                func.sum(
                    case((EmployeeAttachment.is_published == True, 1), else_=0)  # noqa: E712
                ).label("sent"),
            )
            .filter(EmployeeAttachment.attachment_type == "PAYSLIP")
            .filter(EmployeeAttachment.payslip_year.isnot(None))
            .filter(EmployeeAttachment.payslip_month.isnot(None))
            .group_by(EmployeeAttachment.payslip_year, EmployeeAttachment.payslip_month)
            .order_by(EmployeeAttachment.payslip_year.desc(), EmployeeAttachment.payslip_month.desc())
            .limit(limit)
            .all()
        )
        for r in rows:
            out.append({
                "year": int(r.year or 0),
                "month": int(r.month or 0),
                "total": int(r.total or 0),
                "drafts": int(r.drafts or 0),
                "sent": int(r.sent or 0),
            })
    except Exception:
        out = []
    return out


def _register_or_replace_payslip(
    *,
    emp: EmployeeFile,
    source_pdf: Path,
    original_name: str,
    year: int,
    month: int,
    page_label: str,
    identity_number: str,
) -> EmployeeAttachment:
    user_id = int(emp.user_id)
    employee_dir = _employee_upload_dir(user_id)
    stored_name = f"{uuid4().hex}.pdf"
    stored_path = employee_dir / stored_name
    shutil.copy2(source_pdf, stored_path)

    att = EmployeeAttachment.query.filter_by(
        user_id=user_id,
        attachment_type="PAYSLIP",
        payslip_year=year,
        payslip_month=month,
    ).first()

    if att:
        try:
            old_path = employee_dir / (att.stored_name or "")
            if old_path.exists():
                old_path.unlink()
        except Exception:
            pass
        att.original_name = original_name
        att.stored_name = stored_name
        att.note = f"قسيمة راتب مستخرجة من ملف جماعي - الصفحات {page_label} - رقم الهوية {identity_number}"
        att.uploaded_by_id = current_user.id
        att.uploaded_at = datetime.utcnow()
    else:
        att = EmployeeAttachment(
            user_id=user_id,
            attachment_type="PAYSLIP",
            original_name=original_name,
            stored_name=stored_name,
            note=f"قسيمة راتب مستخرجة من ملف جماعي - الصفحات {page_label} - رقم الهوية {identity_number}",
            payslip_year=year,
            payslip_month=month,
            uploaded_by_id=current_user.id,
            uploaded_at=datetime.utcnow(),
        )
        db.session.add(att)

    # Draft by default; the send page will publish/send it later.
    try:
        att.is_published = False
        att.published_at = None
        att.published_by_id = None
    except Exception:
        pass

    sync_employee_attachment_to_archive(att, source_path=stored_path)

    return att


def _split_and_register_payslip_pdf(
    input_pdf_path: Path,
    output_dir: Path,
    *,
    year: int,
    month: int,
) -> tuple[list[dict], list[str], dict]:
    """Group pages by identity, then save each grouped payslip as one draft attachment."""
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(input_pdf_path))
    rows: list[dict] = []
    warnings: list[str] = []
    counters = {"saved": 0, "skipped": 0, "pages": 0}

    page_groups: list[dict] = []
    groups_by_identity: dict[str, dict] = {}

    try:
        for page_index in range(doc.page_count):
            page_number = page_index + 1
            counters["pages"] += 1

            page_text = doc.load_page(page_index).get_text("text")
            identity_number = _extract_identity_number(page_text)

            if identity_number:
                group = groups_by_identity.get(identity_number)
                if group is None:
                    group = {
                        "identity_number": identity_number,
                        "has_identity": True,
                        "page_indexes": [],
                    }
                    groups_by_identity[identity_number] = group
                    page_groups.append(group)
            else:
                identity_number = f"page_{page_number:03d}_NO_ID"
                warnings.append(f"لم يتم العثور على رقم الهوية في الصفحة رقم {page_number}.")
                group = {
                    "identity_number": identity_number,
                    "has_identity": False,
                    "page_indexes": [],
                }
                page_groups.append(group)

            group["page_indexes"].append(page_index)

        for group in page_groups:
            identity_number = group["identity_number"]
            page_indexes = group["page_indexes"]
            page_numbers = [page_index + 1 for page_index in page_indexes]
            page_label = "، ".join(str(page_number) for page_number in page_numbers)

            split_pdf_path = _unique_pdf_path(output_dir, identity_number)
            grouped_pdf = fitz.open()
            try:
                for page_index in page_indexes:
                    grouped_pdf.insert_pdf(doc, from_page=page_index, to_page=page_index)
                grouped_pdf.save(str(split_pdf_path), garbage=4, deflate=True)
            finally:
                grouped_pdf.close()

            row = {
                "page": page_label,
                "page_numbers": page_numbers,
                "identity_number": identity_number,
                "filename": split_pdf_path.name,
                "employee_no": "",
                "employee_name": "",
                "user_id": None,
                "attachment_id": None,
                "status": "skipped",
                "message": "",
            }

            emp = _find_employee_by_identity(identity_number) if group["has_identity"] else None
            if not emp:
                counters["skipped"] += 1
                row["message"] = f"لم يتم العثور على موظف يحمل رقم الهوية {identity_number} في ملف الموظفين."
                warnings.append(f"الصفحات {page_label}: {row['message']}")
                rows.append(row)
                continue

            user = None
            try:
                user = User.query.get(int(emp.user_id))
            except Exception:
                user = None

            try:
                att = _register_or_replace_payslip(
                    emp=emp,
                    source_pdf=split_pdf_path,
                    original_name=split_pdf_path.name,
                    year=year,
                    month=month,
                    page_label=page_label,
                    identity_number=identity_number,
                )
                db.session.flush()

                counters["saved"] += 1
                row.update({
                    "employee_no": getattr(emp, "employee_no", "") or "",
                    "employee_name": _employee_display_name(emp, user),
                    "user_id": int(emp.user_id),
                    "attachment_id": getattr(att, "id", None),
                    "status": "saved",
                    "message": f"تم حفظ قسيمة من {len(page_numbers)} صفحة كمسودة وستظهر في صفحة إرسال قسائم الرواتب.",
                })
            except Exception as exc:
                counters["skipped"] += 1
                row["message"] = f"تعذر حفظ القسيمة في ملف الموظف: {exc}"
                warnings.append(f"الصفحات {page_label}: {row['message']}")

            rows.append(row)
    finally:
        doc.close()

    return rows, warnings, counters


@portal_bp.route("/hr/payslips/bulk-upload", methods=["GET", "POST"])
@login_required
@perm_required(HR_EMP_ATTACH)
def hr_payslips_bulk_upload():
    """
    New HR payslip upload page:
      - Upload one combined PDF.
      - Split every page by the ID number inside the page.
      - Match ID number against EmployeeFile.national_id.
      - Save each matched slip as EmployeeAttachment(PAYSLIP) draft for the selected year/month.
      - The existing send page can then publish/send the drafts.
    """
    _ensure_employee_attachment_payslip_schema()

    now = datetime.now()
    try:
        default_year = int(request.values.get("year") or now.year)
    except Exception:
        default_year = int(now.year)
    try:
        default_month = int(request.values.get("month") or now.month)
    except Exception:
        default_month = int(now.month)
    if not (2000 <= default_year <= 2100):
        default_year = int(now.year)
    if not (1 <= default_month <= 12):
        default_month = int(now.month)

    rows: list[dict] = []
    warnings: list[str] = []
    batch_id = None
    counters = {"saved": 0, "skipped": 0, "pages": 0}
    year = default_year
    month = default_month

    if request.method == "POST":
        y = (request.form.get("year") or "").strip()
        m = (request.form.get("month") or "").strip()

        try:
            year = int(y)
            month = int(m)
        except Exception:
            flash("الرجاء اختيار سنة/شهر صحيحين قبل رفع القسائم.", "danger")
            return redirect(url_for("portal.hr_payslips_bulk_upload"))

        if not (2000 <= year <= 2100) or not (1 <= month <= 12):
            flash("السنة/الشهر غير صالحين.", "danger")
            return redirect(url_for("portal.hr_payslips_bulk_upload"))

        uploaded_file = request.files.get("pdf_file")
        if not uploaded_file or not uploaded_file.filename:
            flash("يرجى اختيار ملف PDF.", "danger")
            return redirect(url_for("portal.hr_payslips_bulk_upload", year=year, month=month))

        if not uploaded_file.filename.lower().endswith(".pdf"):
            flash("الملف يجب أن يكون بصيغة PDF فقط.", "danger")
            return redirect(url_for("portal.hr_payslips_bulk_upload", year=year, month=month))

        batch_id = f"{year:04d}_{month:02d}_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
        batch_dir = _payslip_batch_dir(batch_id)
        input_dir = batch_dir / "input"
        output_dir = batch_dir / "output"
        input_dir.mkdir(parents=True, exist_ok=True)

        original_filename = secure_filename(uploaded_file.filename) or "payslips.pdf"
        input_pdf_path = input_dir / original_filename
        uploaded_file.save(str(input_pdf_path))

        try:
            rows, warnings, counters = _split_and_register_payslip_pdf(
                input_pdf_path,
                output_dir,
                year=year,
                month=month,
            )

            zip_path = batch_dir / "payslips_split.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for item in rows:
                    pdf_path = output_dir / item["filename"]
                    if pdf_path.exists():
                        zip_file.write(pdf_path, arcname=item["filename"])

            try:
                _portal_audit(
                    action="HR_PAYSLIPS_BULK_UPLOAD",
                    note=f"رفع قسائم رواتب شهر {year:04d}-{month:02d} من ملف PDF جماعي (تم حفظ {counters['saved']} مسودة، تخطي {counters['skipped']})",
                    target_type="PAYSLIP",
                    target_id=0,
                )
            except Exception:
                pass

            db.session.commit()
            flash(
                f"تمت العملية لشهر {year:04d}-{month:02d}: حفظ {counters['saved']} مسودة، تخطي {counters['skipped']}.",
                "success" if counters["saved"] else "warning",
            )

        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception("Payslip PDF split/register failed")
            flash(f"حدث خطأ أثناء تقسيم/حفظ القسائم: {exc}", "danger")
            return redirect(url_for("portal.hr_payslips_bulk_upload", year=year, month=month))

    return render_template(
        "portal/payslips_bulk_upload.html",
        rows=rows,
        warnings=warnings,
        batch_id=batch_id,
        year=year,
        month=month,
        counters=counters,
        recent_months=_recent_payslip_months(),
    )


@portal_bp.route("/hr/payslips/bulk-upload/<batch_id>/download-zip")
@login_required
@perm_required(HR_EMP_ATTACH)
def hr_payslips_bulk_download_zip(batch_id):
    batch_dir = _payslip_batch_dir(batch_id)
    zip_path = batch_dir / "payslips_split.zip"

    if not zip_path.exists():
        abort(404)

    return send_from_directory(batch_dir, "payslips_split.zip", as_attachment=True)


@portal_bp.route("/hr/payslips/bulk-upload/<batch_id>/download/<filename>")
@login_required
@perm_required(HR_EMP_ATTACH)
def hr_payslips_bulk_download_file(batch_id, filename):
    batch_dir = _payslip_batch_dir(batch_id)
    output_dir = batch_dir / "output"
    safe_filename = secure_filename(filename)
    file_path = output_dir / safe_filename

    if not file_path.exists():
        abort(404)

    return send_from_directory(output_dir, safe_filename, as_attachment=True)
