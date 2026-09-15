from __future__ import annotations

import os
import re
import shutil
import threading
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
from sqlalchemy import case, func, or_
from werkzeug.utils import secure_filename

from . import portal_bp
from extensions import db
from models import EmployeeAttachment, EmployeeFile, User
from utils.perms import perm_required
from services.employee_attachment_archive import sync_employee_attachment_to_archive
from services.correspondence_intake import (
    OcrConfig,
    _resolve_tesseract_command,
    _run_tesseract_png,
)

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

_RAPID_OCR_ENGINE = None
_RAPID_OCR_LOCK = threading.Lock()


def _normalize_digits(value: str | None) -> str:
    return (value or "").translate(_ARABIC_DIGITS_MAP)


def _digits_only(value: str | None) -> str:
    return "".join(re.findall(r"\d+", _normalize_digits(value or "")))


def _extract_identity_number(page_text: str | None) -> str | None:
    """Extract the 9-digit ID from a Palestinian Ministry of Finance payslip page."""
    text = _normalize_digits(page_text or "")
    text = re.sub(r"\s+", " ", text)

    digit_sequence = r"([0-9](?:[\s\-]?[0-9]){8})(?![\s\-]?[0-9])"
    patterns = [
        rf"رقم\s*الهوية\s*[:：]?\s*{digit_sequence}",
        rf"رقم\s*الهويه\s*[:：]?\s*{digit_sequence}",
        rf"الهوية\s*[:：]?\s*{digit_sequence}",
        rf"الهويه\s*[:：]?\s*{digit_sequence}",
        rf"(?:national\s*(?:id|number)|identity(?:\s*(?:no|number))?|id\s*(?:no|number))\s*[:：#]?\s*{digit_sequence}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = _digits_only(match.group(1))
            if len(candidate) == 9:
                return candidate

    # In some Arabic PDFs, text extraction may join fields without spaces.
    # Fallback: first 9-digit number near the top of the page.
    top_text = text[:1200]
    ids = re.findall(r"(?<!\d)([0-9]{9})(?!\d)", top_text)
    for candidate in ids:
        normalized = _digits_only(candidate)
        if len(normalized) == 9:
            return normalized
    return None


def _run_rapid_ocr_png(png_bytes: bytes) -> str:
    """Run the bundled local OCR engine without sending payroll data outside."""
    global _RAPID_OCR_ENGINE

    with _RAPID_OCR_LOCK:
        if _RAPID_OCR_ENGINE is None:
            from rapidocr import RapidOCR

            _RAPID_OCR_ENGINE = RapidOCR()
        result = _RAPID_OCR_ENGINE(png_bytes)

    return "\n".join(str(value) for value in (getattr(result, "txts", None) or ()))


def _payslip_page_text(page) -> tuple[str, bool, str | None]:
    """Read native PDF text, then OCR image-only Ministry payslips locally."""
    native_text = page.get_text("text") or ""
    if _extract_identity_number(native_text):
        return native_text, False, None

    enabled_value = current_app.config.get("PAYSLIP_OCR_ENABLED", True)
    enabled = str(enabled_value).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return native_text, False, "لم يظهر رقم الهوية في نص القسيمة وقراءة OCR غير مفعّلة."

    # Identity and employee information are in the upper section. Cropping
    # reduces OCR time and keeps bank/summary numbers from winning the
    # nine-digit fallback match.
    clip = fitz.Rect(
        page.rect.x0,
        page.rect.y0,
        page.rect.x1,
        page.rect.y0 + (page.rect.height * 0.68),
    )
    dpi = int(current_app.config.get("PAYSLIP_OCR_DPI", 200))
    try:
        png_bytes = page.get_pixmap(dpi=dpi, clip=clip, alpha=False).tobytes("png")
    except Exception as exc:
        current_app.logger.warning("Payslip page rendering failed: %s", exc)
        return native_text, False, "تعذر تجهيز صفحة القسيمة للقراءة الضوئية."

    rapid_enabled_value = current_app.config.get("PAYSLIP_RAPID_OCR_ENABLED", True)
    rapid_enabled = str(rapid_enabled_value).strip().lower() in {"1", "true", "yes", "on"}
    rapid_error = None
    if rapid_enabled:
        try:
            rapid_text = _run_rapid_ocr_png(png_bytes)
            combined_text = "\n".join(value for value in (native_text, rapid_text) if value.strip())
            if _extract_identity_number(combined_text):
                return combined_text, True, None
        except (ImportError, ModuleNotFoundError) as exc:
            rapid_error = exc
        except Exception as exc:
            rapid_error = exc
            current_app.logger.warning("Payslip RapidOCR failed: %s", exc)

    config = OcrConfig(
        enabled=True,
        command=str(current_app.config.get(
            "PAYSLIP_TESSERACT_CMD",
            current_app.config.get("CORR_INTAKE_TESSERACT_CMD", "tesseract"),
        )),
        # The payroll identity digits are Latin digits. English is enough and
        # avoids requiring the optional Arabic Tesseract language package.
        languages="eng",
        max_pages=1,
        dpi=dpi,
        timeout_seconds=float(current_app.config.get("PAYSLIP_OCR_PAGE_TIMEOUT_SECONDS", 20)),
    )
    command = _resolve_tesseract_command(config)
    if not command and os.name == "nt":
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            candidate = Path(root or "") / "Tesseract-OCR" / "tesseract.exe"
            if root and candidate.is_file():
                command = str(candidate.resolve())
                break
    if not command:
        if rapid_error:
            current_app.logger.warning("Bundled payslip OCR is unavailable: %s", rapid_error)
            message = (
                "تعذر تشغيل محرك OCR المحلي لقراءة رقم الهوية من القسيمة المصورة؛ "
                "تحقق من تثبيت rapidocr وonnxruntime أو إعداد Tesseract."
            )
        elif rapid_enabled:
            message = (
                "تمت قراءة القسيمة المصورة بمحرك OCR المحلي، لكن تعذر تمييز رقم هوية "
                "صالح فيها؛ راجع وضوح الصفحة ورقم الهوية."
            )
        else:
            message = (
                "لم يظهر رقم الهوية في نص القسيمة، ومحركات OCR المحلية الإضافية غير متاحة."
            )
        return native_text, False, message

    try:
        page_text = _run_tesseract_png(
            png_bytes,
            command,
            config,
            config.timeout_seconds,
        )
    except Exception as exc:
        current_app.logger.warning("Payslip OCR failed: %s", exc)
        return native_text, False, "تعذر إجراء OCR لإحدى صفحات القسائم الممسوحة؛ راجع جودة الملف."
    combined_text = "\n".join(value for value in (native_text, page_text) if value.strip())
    return combined_text, bool(page_text.strip()), None


def _uploaded_payslip_files(file_storage) -> list:
    """Accept the current field plus legacy field names during deployment."""
    uploads = []
    seen = set()
    for field_name in ("pdf_files", "pdf_file", "files"):
        for uploaded in file_storage.getlist(field_name):
            if not uploaded or not getattr(uploaded, "filename", None):
                continue
            marker = id(uploaded)
            if marker in seen:
                continue
            seen.add(marker)
            uploads.append(uploaded)
    return uploads


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


def _combine_payslip_pdf_files(
    sources: list[tuple[Path, str]],
    combined_pdf_path: Path,
) -> list[str]:
    """Combine uploaded PDFs and retain the source filename for every page."""
    combined_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    combined_document = fitz.open()
    source_filenames_by_page: list[str] = []

    try:
        for source_path, source_filename in sources:
            source_document = None
            try:
                source_document = fitz.open(str(source_path))
                if source_document.page_count < 1:
                    raise ValueError("الملف لا يحتوي على صفحات.")
                combined_document.insert_pdf(source_document)
                source_filenames_by_page.extend(
                    [str(source_filename or source_path.name)] * source_document.page_count
                )
            except Exception as exc:
                raise ValueError(
                    f"تعذر قراءة ملف PDF «{source_filename or source_path.name}»: {exc}"
                ) from exc
            finally:
                if source_document is not None:
                    source_document.close()

        if combined_document.page_count < 1:
            raise ValueError("ملفات القسائم لا تحتوي على صفحات قابلة للمعالجة.")

        combined_document.save(str(combined_pdf_path), garbage=4, deflate=True)
    finally:
        combined_document.close()

    return source_filenames_by_page


def _find_employee_by_identity(identity_number: str | None) -> EmployeeFile | None:
    """Find payroll recipient by identity, including legacy daily-wage records."""
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
        pass

    # Some daily-wage employee files were imported before national_id became
    # mandatory. Their Ministry-of-Finance identity is stored in employee_no or
    # timeclock_code. Accept a unique exact match so their slips join the same
    # PAYSLIP collection without creating a separate workflow.
    try:
        candidates = (
            EmployeeFile.query
            .filter(or_(
                func.trim(EmployeeFile.employee_no) == wanted,
                func.trim(EmployeeFile.timeclock_code) == wanted,
            ))
            .all()
        )
        unique = {int(emp.user_id): emp for emp in candidates}
        if len(unique) == 1:
            return next(iter(unique.values()))
    except Exception:
        pass

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
    source_filenames: list[str] | None = None,
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

    unique_source_filenames = list(dict.fromkeys(
        str(value).strip() for value in (source_filenames or []) if str(value).strip()
    ))
    source_note = ""
    if unique_source_filenames:
        source_note = f" - الملفات المصدرية: {'، '.join(unique_source_filenames)}"
    # EmployeeAttachment.note is VARCHAR(255) in production.  Keep the
    # identity at the beginning and bound optional page/source details so a
    # batch containing many files cannot make an otherwise valid upload fail.
    note = (
        f"قسيمة راتب مستخرجة من ملف جماعي - رقم الهوية {identity_number} - "
        f"الصفحات {page_label}{source_note}"
    )[:255]

    if att:
        # Keep the prior file until the surrounding database transaction is
        # known to have committed. A rollback must not leave the existing
        # attachment pointing at a file that was already deleted.
        previous_path = employee_dir / (att.stored_name or "")
        att._payslip_obsolete_path = str(previous_path) if previous_path.is_file() else None
        att.original_name = original_name
        att.stored_name = stored_name
        att.note = note
        att.uploaded_by_id = current_user.id
        att.uploaded_at = datetime.utcnow()
    else:
        att = EmployeeAttachment(
            user_id=user_id,
            attachment_type="PAYSLIP",
            original_name=original_name,
            stored_name=stored_name,
            note=note,
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
    source_filenames_by_page: list[str] | None = None,
) -> tuple[list[dict], list[str], dict]:
    """Group pages by identity, then save each grouped payslip as one draft attachment."""
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(input_pdf_path))
    rows: list[dict] = []
    warnings: list[str] = []
    counters = {"saved": 0, "skipped": 0, "pages": 0}

    page_groups: list[dict] = []
    groups_by_identity: dict[str, dict] = {}
    ocr_pages = 0
    ocr_messages: set[str] = set()

    try:
        for page_index in range(doc.page_count):
            page_number = page_index + 1
            counters["pages"] += 1

            page_text, used_ocr, ocr_message = _payslip_page_text(doc.load_page(page_index))
            if used_ocr:
                ocr_pages += 1
            if ocr_message:
                ocr_messages.add(ocr_message)
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

        if ocr_pages:
            warnings.append(f"تم استخدام OCR المحلي لقراءة {ocr_pages} صفحة ممسوحة ضوئياً.")
        warnings.extend(sorted(ocr_messages))

        for group in page_groups:
            identity_number = group["identity_number"]
            page_indexes = group["page_indexes"]
            page_numbers = [page_index + 1 for page_index in page_indexes]
            page_label = "، ".join(str(page_number) for page_number in page_numbers)
            source_filenames: list[str] = []
            if source_filenames_by_page:
                for page_index in page_indexes:
                    if page_index >= len(source_filenames_by_page):
                        continue
                    source_filename = str(source_filenames_by_page[page_index] or "").strip()
                    if source_filename and source_filename not in source_filenames:
                        source_filenames.append(source_filename)
            source_filename_label = "، ".join(source_filenames)

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
                "source_filename": source_filename_label,
                "source_filenames": source_filenames,
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
                # Isolate one employee from another so one malformed legacy
                # record does not roll back every valid identity in the batch.
                with db.session.begin_nested():
                    att = _register_or_replace_payslip(
                        emp=emp,
                        source_pdf=split_pdf_path,
                        original_name=split_pdf_path.name,
                        year=year,
                        month=month,
                        page_label=page_label,
                        identity_number=identity_number,
                        source_filenames=source_filenames,
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
                    "_obsolete_path": getattr(att, "_payslip_obsolete_path", None),
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
      - Upload one or more PDF files in one batch.
      - Combine and split every page by the ID number inside the page.
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

        uploaded_files = _uploaded_payslip_files(request.files)
        if not uploaded_files:
            flash("يرجى اختيار ملف PDF واحد على الأقل.", "danger")
            return redirect(url_for("portal.hr_payslips_bulk_upload", year=year, month=month))

        invalid_names = [
            uploaded.filename
            for uploaded in uploaded_files
            if not uploaded.filename.lower().endswith(".pdf")
        ]
        if invalid_names:
            flash("جميع الملفات يجب أن تكون بصيغة PDF فقط.", "danger")
            return redirect(url_for("portal.hr_payslips_bulk_upload", year=year, month=month))

        batch_id = f"{year:04d}_{month:02d}_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
        batch_dir = _payslip_batch_dir(batch_id)
        input_dir = batch_dir / "input"
        output_dir = batch_dir / "output"
        input_dir.mkdir(parents=True, exist_ok=True)

        try:
            uploaded_sources: list[tuple[Path, str]] = []
            for upload_index, uploaded_file in enumerate(uploaded_files, start=1):
                original_filename = secure_filename(uploaded_file.filename) or f"payslips_{upload_index}.pdf"
                input_pdf_path = input_dir / original_filename
                duplicate_number = 2
                while input_pdf_path.exists():
                    input_pdf_path = input_dir / (
                        f"{Path(original_filename).stem}_{duplicate_number}"
                        f"{Path(original_filename).suffix or '.pdf'}"
                    )
                    duplicate_number += 1
                uploaded_file.save(str(input_pdf_path))
                uploaded_sources.append((input_pdf_path, uploaded_file.filename))

            combined_pdf_path = batch_dir / "combined_payslips.pdf"
            source_filenames_by_page = _combine_payslip_pdf_files(
                uploaded_sources,
                combined_pdf_path,
            )
            rows, warnings, counters = _split_and_register_payslip_pdf(
                combined_pdf_path,
                output_dir,
                year=year,
                month=month,
                source_filenames_by_page=source_filenames_by_page,
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
                    note=(
                        f"رفع قسائم رواتب شهر {year:04d}-{month:02d} من "
                        f"{len(uploaded_files)} ملف PDF (تم حفظ {counters['saved']} مسودة، "
                        f"تخطي {counters['skipped']})"
                    ),
                    target_type="PAYSLIP",
                    target_id=0,
                )
            except Exception:
                pass

            db.session.commit()
            # Database references now point to the new files, so superseded
            # copies can be removed without risking a broken attachment after
            # a transaction rollback.
            for item in rows:
                obsolete_path = item.pop("_obsolete_path", None)
                if not obsolete_path:
                    continue
                try:
                    Path(obsolete_path).unlink(missing_ok=True)
                except OSError:
                    current_app.logger.warning(
                        "Could not remove superseded payslip file: %s",
                        obsolete_path,
                    )
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
        "portal/hr/payslips_bulk_upload.html",
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
