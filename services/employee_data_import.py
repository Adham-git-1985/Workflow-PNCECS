from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import unicodedata
from typing import Any

from sqlalchemy import func

from extensions import db
from models import (
    Department,
    Directorate,
    Division,
    EmployeeDependent,
    EmployeeFile,
    EmployeeQualification,
    EmployeeSecondment,
    HRLookupItem,
    Organization,
    Section,
    User,
)


FORM_SCHEMA_PREFIX = "EMP-DATA-FORM/"
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


class EmployeeDataImportError(ValueError):
    def __init__(self, message: str, *, plan: dict | None = None):
        super().__init__(message)
        self.plan = plan or {}


TEXT_FIELDS = {
    "employee_no": "الرقم الوظيفي",
    "full_name_quad": "الاسم الرباعي",
    "timeclock_code": "كود ساعة الدوام",
    "national_id": "رقم الهوية/الجواز",
    "birth_date": "تاريخ الميلاد",
    "address": "العنوان",
    "phone": "الهاتف",
    "mobile": "رقم الجوال",
    "email": "البريد الإلكتروني",
    "status_date": "تاريخ سريان الحالة",
    "status_note": "ملاحظة حالة الموظف",
    "hire_date": "تاريخ التعيين",
    "last_promotion_date": "تاريخ آخر ترقية",
    "bank_account": "رقم الحساب/IBAN",
    "notes": "ملاحظات عامة",
}

FLOAT_FIELDS = {
    "hourly_number": "الرقم في الساعة",
}

DATE_FIELDS = {"birth_date", "status_date", "hire_date", "last_promotion_date"}

LOOKUP_FIELDS = {
    "identity_type_lookup_id": ("IDENTITY_TYPE", "نوع وثيقة الهوية"),
    "gender_lookup_id": ("GENDER", "الجنس"),
    "marital_status_lookup_id": ("MARITAL_STATUS", "الحالة الاجتماعية"),
    "religion_lookup_id": ("RELIGION", "الديانة"),
    "disability_lookup_id": ("DISABILITY", "حالة الإعاقة"),
    "home_governorate_lookup_id": ("HOME_GOV", "محافظة السكن"),
    "locality_lookup_id": ("LOCALITY", "التجمع السكاني"),
    "work_governorate_lookup_id": ("WORK_GOV", "محافظة العمل"),
    "work_location_lookup_id": ("WORK_LOCATION", "موقع العمل"),
    "employee_status_lookup_id": ("EMP_STATUS", "حالة الموظف"),
    "shift_lookup_id": ("SHIFT", "الوردية"),
    "project_lookup_id": ("PROJECT", "المشروع"),
    "appointment_type_lookup_id": ("APPOINTMENT_TYPE", "نوع التعيين"),
    "job_category_lookup_id": ("JOB_CATEGORY", "الفئة الوظيفية"),
    "job_grade_lookup_id": ("JOB_GRADE", "الدرجة الوظيفية"),
    "job_title_lookup_id": ("JOB_TITLE", "المسمى الوظيفي"),
    "admin_title_lookup_id": ("ADMIN_TITLE", "المسمى الإداري"),
    "bank_lookup_id": ("BANK", "البنك"),
}

ORG_FIELDS = {
    "organization_id": (Organization, "الإدارة العامة"),
    "directorate_id": (Directorate, "الدائرة"),
    "department_id": (Department, "القسم"),
    "division_id": (Division, "الشعبة"),
}

# The questionnaire uses human-facing levels that start at "الإدارة العامة"
# while EmployeeFile also stores the institution above it. Keep the incoming
# keys for backward compatibility with issued forms, but write each value to its
# real hierarchy field.
EMPLOYEE_ORG_FIELDS = (
    ("organization_id", "directorate_id", Directorate, "الإدارة العامة"),
    ("directorate_id", "department_id", Department, "الدائرة"),
    ("department_id", "section_id", Section, "القسم"),
    ("division_id", "division_id", Division, "الشعبة"),
)

REPEATED_LABELS = {
    "dependents": "التابعون",
    "qualifications": "المؤهلات",
    "secondments": "التكليفات",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).lower()
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Mn", "Cf"})
    text = text.replace("ـ", "")
    return "".join(ch for ch in text if ch.isalnum())


def _name_tokens(value: Any) -> set[str]:
    text = unicodedata.normalize("NFKC", _clean(value)).lower()
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Mn", "Cf"})
    text = text.replace("ـ", "")
    return {
        "".join(ch for ch in token if ch.isalnum())
        for token in re.split(r"\s+", text)
        if any(ch.isalnum() for ch in token)
    }


def validate_employee_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise EmployeeDataImportError("بنية ملف الإجابات غير صحيحة.")
    schema = _clean(payload.get("schema"))
    if not schema.startswith(FORM_SCHEMA_PREFIX):
        raise EmployeeDataImportError("إصدار نموذج الإجابات غير مدعوم.")
    if not isinstance(payload.get("fields"), dict):
        raise EmployeeDataImportError("ملف الإجابات لا يحتوي على حقول صالحة.")
    if payload.get("employee") is not None and not isinstance(payload.get("employee"), dict):
        raise EmployeeDataImportError("بيانات تعريف الموظف داخل ملف الإجابات غير صالحة.")
    if payload.get("tables") is not None and not isinstance(payload.get("tables"), dict):
        raise EmployeeDataImportError("جداول ملف الإجابات غير صالحة.")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise EmployeeDataImportError("حجم ملف الإجابات يتجاوز 2 ميجابايت.")
    return payload


def canonical_payload_hash(payload: dict) -> str:
    validate_employee_payload(payload)
    canonical = {
        "schema": payload.get("schema"),
        "employee": payload.get("employee") or {},
        "fields": payload.get("fields") or {},
        "tables": payload.get("tables") or {},
        "selections": payload.get("selections") or [],
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _field_items(payload: dict, field: str) -> list[dict]:
    raw = (payload.get("fields") or {}).get(field) or []
    if not isinstance(raw, list):
        raw = [raw]
    items = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            value = _clean(item.get("value"))
            entry_id = _clean(item.get("entry_id"))
            occurrence = item.get("occurrence")
        else:
            value = _clean(item)
            entry_id = ""
            occurrence = None
        if not value:
            continue
        if occurrence is None and entry_id:
            match = re.search(r"#(\d+)$", entry_id)
            occurrence = int(match.group(1)) if match else None
        try:
            occurrence = int(occurrence) if occurrence is not None else index
        except (TypeError, ValueError):
            occurrence = index
        items.append({"value": value, "occurrence": occurrence})
    return items


def _first_value(payload: dict, field: str) -> str | None:
    items = _field_items(payload, field)
    return items[0]["value"] if items else None


def _record_value(record: dict, key: str) -> str | None:
    value = record.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    value = _clean(value)
    return value or None


def _records_with_prefix(payload: dict, prefix: str) -> list[dict]:
    records: list[dict] = []
    for table_rows in (payload.get("tables") or {}).values():
        if not isinstance(table_rows, list):
            continue
        for record in table_rows:
            if isinstance(record, dict) and any(str(key).startswith(prefix) for key in record):
                records.append(record)
    return records


def _parse_date(value: str, context: str, unresolved: list[dict]) -> str | None:
    normalized = _clean(value).translate(str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789",
    )).replace("–", "-").replace("—", "-")
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(normalized, pattern).strftime("%Y-%m-%d")
        except ValueError:
            continue
    unresolved.append({"field": context, "value": value, "reason": "التاريخ غير صحيح؛ استخدم YYYY-MM-DD"})
    return None


def _parse_float(value: str, context: str, unresolved: list[dict]) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        unresolved.append({"field": context, "value": value, "reason": "القيمة الرقمية غير صحيحة"})
        return None


def _lookup_match(category: str, value: str) -> HRLookupItem | None:
    wanted = _norm(value)
    if not wanted:
        return None
    rows = HRLookupItem.query.filter_by(category=category).all()
    for row in rows:
        if wanted in {_norm(row.code), _norm(row.name_ar), _norm(row.name_en), _norm(row.label)}:
            return row
    return None


def _create_lookup(category: str, value: str, created_lookups: list[dict]) -> HRLookupItem:
    digest = hashlib.sha1(f"{category}:{value}".encode("utf-8")).hexdigest()[:16].upper()
    code = f"FORM_{digest}"
    existing = HRLookupItem.query.filter_by(category=category, code=code).first()
    if existing:
        return existing
    max_order = db.session.query(func.max(HRLookupItem.sort_order)).filter_by(category=category).scalar() or 0
    row = HRLookupItem(
        category=category,
        code=code,
        name_ar=value,
        name_en=None,
        sort_order=int(max_order) + 10,
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.session.add(row)
    db.session.flush()
    created_lookups.append({"category": category, "value": value, "id": row.id})
    return row


def _resolve_lookup(
    category: str,
    value: str | None,
    context: str,
    unresolved: list[dict],
    *,
    create_missing: bool,
    created_lookups: list[dict],
) -> tuple[int | None, str | None]:
    if not value:
        return None, None
    match = _lookup_match(category, value)
    if match:
        return match.id, match.label
    if create_missing:
        match = _create_lookup(category, value, created_lookups)
        return match.id, match.label
    unresolved.append({"field": context, "value": value, "reason": f"غير موجود في القائمة المرجعية {category}"})
    return None, None


def _named_model_matches(model, value: str | None) -> list:
    wanted = _norm(value)
    if not wanted:
        return []
    matches = []
    for row in model.query.all():
        candidates = [_norm(getattr(row, attr, None)) for attr in ("name_ar", "name_en", "code")]
        if wanted and wanted in candidates:
            matches.append(row)
    return matches


def _resolve_named_model(model, value: str | None, context: str, unresolved: list[dict]):
    if not value:
        return None, None
    matches = _named_model_matches(model, value)
    if len(matches) == 1:
        row = matches[0]
        return row.id, getattr(row, "name_ar", None) or getattr(row, "name_en", None) or getattr(row, "code", None)
    reason = "أكثر من قيمة مطابقة" if len(matches) > 1 else "غير موجود في الهيكلية"
    unresolved.append({"field": context, "value": value, "reason": reason})
    return None, None


def _resolve_employee_placement(payload: dict, unresolved: list[dict]) -> list[dict]:
    """Resolve questionnaire placement fields against their real hierarchy levels."""
    resolved: dict[str, Any] = {}
    direct_input: dict[str, tuple[str, str]] = {}

    for source_field, target_field, model, label in EMPLOYEE_ORG_FIELDS:
        incoming = _first_value(payload, source_field)
        if incoming is None:
            continue
        matches = _named_model_matches(model, incoming)

        # Repeated Arabic labels are valid in different branches. Scope lower
        # levels to the parent already selected in the same questionnaire.
        if target_field == "department_id" and resolved.get("directorate_id"):
            matches = [
                row for row in matches
                if int(getattr(row, "directorate_id", 0) or 0) == int(resolved["directorate_id"])
            ]
        elif target_field == "section_id":
            if resolved.get("department_id"):
                matches = [
                    row for row in matches
                    if int(getattr(row, "department_id", 0) or 0) == int(resolved["department_id"])
                ]
            elif resolved.get("directorate_id"):
                matches = [
                    row for row in matches
                    if (
                        int(getattr(row, "directorate_id", 0) or 0) == int(resolved["directorate_id"])
                        or int(getattr(getattr(row, "department", None), "directorate_id", 0) or 0)
                        == int(resolved["directorate_id"])
                    )
                ]
        elif target_field == "division_id" and resolved.get("section_id"):
            matches = [
                row for row in matches
                if int(getattr(row, "section_id", 0) or 0) == int(resolved["section_id"])
            ]

        if len(matches) != 1:
            reason = "أكثر من قيمة مطابقة ضمن المسار المحدد" if len(matches) > 1 else "غير موجود في الهيكلية ضمن المسار المحدد"
            unresolved.append({"field": label, "value": incoming, "reason": reason})
            continue

        row = matches[0]
        resolved[target_field] = int(row.id)
        direct_input[target_field] = (incoming, label)

    # Fill ancestors so EmployeeFile remains internally consistent even though
    # the questionnaire does not ask for the institution/root explicitly.
    division = db.session.get(Division, resolved["division_id"]) if resolved.get("division_id") else None
    section = db.session.get(Section, resolved["section_id"]) if resolved.get("section_id") else None
    department = db.session.get(Department, resolved["department_id"]) if resolved.get("department_id") else None
    directorate = db.session.get(Directorate, resolved["directorate_id"]) if resolved.get("directorate_id") else None

    if division and not section and getattr(division, "section_id", None):
        section = db.session.get(Section, int(division.section_id))
        resolved["section_id"] = int(section.id) if section else None
    if section and not department and getattr(section, "department_id", None):
        department = db.session.get(Department, int(section.department_id))
        resolved["department_id"] = int(department.id) if department else None
    if department and not directorate and getattr(department, "directorate_id", None):
        directorate = db.session.get(Directorate, int(department.directorate_id))
        resolved["directorate_id"] = int(directorate.id) if directorate else None
    if directorate and getattr(directorate, "organization_id", None):
        resolved["organization_id"] = int(directorate.organization_id)

    target_meta = {
        "organization_id": (Organization, "المؤسسة"),
        "directorate_id": (Directorate, "الإدارة العامة"),
        "department_id": (Department, "الدائرة"),
        "section_id": (Section, "القسم"),
        "division_id": (Division, "الشعبة"),
    }
    operations = []
    for target_field in ("organization_id", "directorate_id", "department_id", "section_id", "division_id"):
        item_id = resolved.get(target_field)
        if not item_id:
            continue
        model, default_label = target_meta[target_field]
        incoming, label = direct_input.get(target_field, (_named_label(model, item_id) or "", default_label))
        operations.append({
            "field": target_field,
            "model": model,
            "label": label,
            "incoming": incoming,
            "resolved": item_id,
            "resolved_label": _named_label(model, item_id),
        })
    return operations


def _resolve_manager(value: str | None, context: str, unresolved: list[dict]):
    if not value:
        return None, None
    wanted = _norm(value)
    exact_matches = []
    token_matches = []
    wanted_tokens = _name_tokens(value)
    for user in User.query.all():
        employee_file = getattr(user, "employee_file", None)
        raw_candidates = (
            user.name,
            user.email,
            getattr(employee_file, "full_name_quad", None),
        )
        if wanted and wanted in {_norm(candidate) for candidate in raw_candidates}:
            exact_matches.append(user)
            continue
        if len(wanted_tokens) >= 2 and any(
            wanted_tokens.issubset(_name_tokens(candidate))
            for candidate in raw_candidates
            if candidate
        ):
            token_matches.append(user)
    matches = exact_matches or token_matches
    if len(matches) == 1:
        user = matches[0]
        return user.id, user.name or user.email
    reason = "أكثر من موظف مطابق" if len(matches) > 1 else "الموظف غير موجود"
    unresolved.append({"field": context, "value": value, "reason": reason})
    return None, None


def _lookup_label(item_id: int | None) -> str | None:
    row = HRLookupItem.query.get(item_id) if item_id else None
    return row.label if row else None


def _named_label(model, item_id: int | None) -> str | None:
    row = model.query.get(item_id) if item_id else None
    if not row:
        return None
    return getattr(row, "name_ar", None) or getattr(row, "name_en", None) or getattr(row, "code", None)


def _current_manager_label(user_id: int | None) -> str | None:
    user = User.query.get(user_id) if user_id else None
    return (user.name or user.email) if user else None


def _operation(field: str, label: str, incoming: str, current: Any, resolved: Any, resolved_label: str | None = None) -> dict:
    return {
        "field": field,
        "label": label,
        "incoming": incoming,
        "current": current,
        "resolved": resolved,
        "resolved_label": resolved_label if resolved_label is not None else resolved,
        "changed": current != resolved,
    }


def build_employee_import_plan(
    payload: dict,
    employee: User,
    *,
    create_missing_lookups: bool = False,
) -> dict:
    validate_employee_payload(payload)
    employee_file = EmployeeFile.query.filter_by(user_id=employee.id).first()
    unresolved: list[dict] = []
    created_lookups: list[dict] = []
    operations: list[dict] = []

    for field, label in TEXT_FIELDS.items():
        incoming = _first_value(payload, field)
        if incoming is None:
            continue
        resolved: Any = incoming
        if field in DATE_FIELDS:
            resolved = _parse_date(incoming, label, unresolved)
        elif field == "timeclock_code" and not incoming.isdigit():
            unresolved.append({"field": label, "value": incoming, "reason": "كود الساعة يجب أن يحتوي أرقاماً فقط"})
            resolved = None
        if resolved is None:
            continue
        current = getattr(employee_file, field, None) if employee_file else None
        operations.append(_operation(field, label, incoming, current, resolved))

    for field, label in FLOAT_FIELDS.items():
        incoming = _first_value(payload, field)
        if incoming is None:
            continue
        resolved = _parse_float(incoming, label, unresolved)
        if resolved is None:
            continue
        current = getattr(employee_file, field, None) if employee_file else None
        operations.append(_operation(field, label, incoming, current, resolved))

    for field, (category, label) in LOOKUP_FIELDS.items():
        incoming = _first_value(payload, field)
        if incoming is None:
            continue
        resolved, resolved_label = _resolve_lookup(
            category,
            incoming,
            label,
            unresolved,
            create_missing=create_missing_lookups,
            created_lookups=created_lookups,
        )
        if resolved is None:
            continue
        current = getattr(employee_file, field, None) if employee_file else None
        operations.append(_operation(field, label, incoming, current, resolved, resolved_label))
        operations[-1]["current_label"] = _lookup_label(current)

    for placement in _resolve_employee_placement(payload, unresolved):
        field = placement["field"]
        model = placement["model"]
        current = getattr(employee_file, field, None) if employee_file else None
        operations.append(_operation(
            field,
            placement["label"],
            placement["incoming"],
            current,
            placement["resolved"],
            placement["resolved_label"],
        ))
        operations[-1]["current_label"] = _named_label(model, current)

    manager_incoming = _first_value(payload, "direct_manager_user_id")
    if manager_incoming:
        resolved, resolved_label = _resolve_manager(manager_incoming, "المسؤول المباشر", unresolved)
        if resolved is not None:
            current = employee_file.direct_manager_user_id if employee_file else None
            operations.append(_operation("direct_manager_user_id", "المسؤول المباشر", manager_incoming, current, resolved, resolved_label))
            operations[-1]["current_label"] = _current_manager_label(current)

    dependent_rows = _build_dependents(payload, employee.id, unresolved, create_missing_lookups, created_lookups)
    qualification_rows = _build_qualifications(payload, employee.id, unresolved, create_missing_lookups, created_lookups)
    secondment_rows = _build_secondments(payload, employee.id, unresolved, create_missing_lookups, created_lookups)

    field_labels = {
        **TEXT_FIELDS,
        **FLOAT_FIELDS,
        **{field: label for field, (_, label) in LOOKUP_FIELDS.items()},
        **{source_field: label for source_field, _, _, label in EMPLOYEE_ORG_FIELDS},
        "direct_manager_user_id": "المسؤول المباشر",
    }
    unresolved_labels = {issue["field"] for issue in unresolved}
    correction_fields = [
        {"field": field, "label": label, "value": _first_value(payload, field) or ""}
        for field, label in field_labels.items()
        if label in unresolved_labels
    ]

    return {
        "employee_id": employee.id,
        "employee_name": employee.name or employee.email,
        "employee_file_exists": employee_file is not None,
        "operations": operations,
        "unresolved": unresolved,
        "correction_fields": correction_fields,
        "created_lookups": created_lookups,
        "dependents": dependent_rows,
        "qualifications": qualification_rows,
        "secondments": secondment_rows,
    }


def _build_dependents(payload, user_id, unresolved, create_missing, created_lookups):
    rows = []
    existing = EmployeeDependent.query.filter_by(user_id=user_id).all()
    for index, record in enumerate(_records_with_prefix(payload, "dependent."), start=1):
        name = _record_value(record, "dependent.full_name")
        if not name:
            continue
        national_id = _record_value(record, "dependent.national_id")
        birth_date = _record_value(record, "dependent.birth_date")
        if birth_date:
            birth_date = _parse_date(birth_date, f"التابع {index}: تاريخ الميلاد", unresolved)
        relation_id, relation_label = _resolve_lookup(
            "DEP_RELATION",
            _record_value(record, "dependent.relation_lookup_id"),
            f"التابع {index}: صلة القرابة",
            unresolved,
            create_missing=create_missing,
            created_lookups=created_lookups,
        )
        gender_id, gender_label = _resolve_lookup(
            "GENDER",
            _record_value(record, "dependent.gender_lookup_id"),
            f"التابع {index}: الجنس",
            unresolved,
            create_missing=create_missing,
            created_lookups=created_lookups,
        )
        allowance_raw = _record_value(record, "dependent.allowance")
        allowance = _parse_float(allowance_raw, f"التابع {index}: العلاوة", unresolved) if allowance_raw else None
        match = None
        for item in existing:
            if national_id and _norm(item.national_id) == _norm(national_id):
                match = item
                break
            if not national_id and _norm(item.full_name) == _norm(name) and _clean(item.birth_date) == _clean(birth_date):
                match = item
                break
        rows.append({
            "match_id": match.id if match else None,
            "action": "update" if match else "create",
            "full_name": name,
            "relation_lookup_id": relation_id,
            "relation_label": relation_label,
            "national_id": national_id,
            "gender_lookup_id": gender_id,
            "gender_label": gender_label,
            "birth_date": birth_date,
            "allowance": allowance,
        })
    return rows


def _build_qualifications(payload, user_id, unresolved, create_missing, created_lookups):
    rows = []
    existing = EmployeeQualification.query.filter_by(user_id=user_id).all()
    mapping = {
        "degree_lookup_id": ("QUAL_DEGREE", "الدرجة العلمية"),
        "specialization_lookup_id": ("QUAL_SPECIALIZATION", "التخصص"),
        "grade_lookup_id": ("QUAL_GRADE", "التقدير"),
        "university_lookup_id": ("UNIVERSITY", "الجامعة"),
        "country_lookup_id": ("COUNTRY", "الدولة"),
    }
    for index, record in enumerate(_records_with_prefix(payload, "qualification."), start=1):
        values = {}
        labels = {}
        for field, (category, label) in mapping.items():
            raw = _record_value(record, f"qualification.{field}")
            resolved, resolved_label = _resolve_lookup(
                category,
                raw,
                f"المؤهل {index}: {label}",
                unresolved,
                create_missing=create_missing,
                created_lookups=created_lookups,
            )
            values[field] = resolved
            labels[field] = resolved_label
        qualification_date = _record_value(record, "qualification.qualification_date")
        if qualification_date:
            qualification_date = _parse_date(qualification_date, f"المؤهل {index}: تاريخ المؤهل", unresolved)
        notes = _record_value(record, "qualification.notes")
        if not any(values.values()) and not qualification_date and not notes:
            continue
        match = next((item for item in existing if (
            item.degree_lookup_id == values["degree_lookup_id"]
            and item.specialization_lookup_id == values["specialization_lookup_id"]
            and _clean(item.qualification_date) == _clean(qualification_date)
            and item.university_lookup_id == values["university_lookup_id"]
        )), None)
        rows.append({
            "match_id": match.id if match else None,
            "action": "update" if match else "create",
            **values,
            "labels": labels,
            "qualification_date": qualification_date,
            "notes": notes,
        })
    return rows


def _build_secondments(payload, user_id, unresolved, create_missing, created_lookups):
    field_names = [
        "date_from", "date_to", "organization_id", "directorate_id", "department_id", "division_id",
        "direct_manager_user_id", "work_governorate_lookup_id", "work_location_lookup_id",
        "admin_title_lookup_id", "details",
    ]
    indexed = {}
    occurrences = set()
    for field in field_names:
        values = _field_items(payload, f"secondment.{field}")
        indexed[field] = {item["occurrence"]: item["value"] for item in values}
        occurrences.update(indexed[field])
    existing = EmployeeSecondment.query.filter_by(user_id=user_id).all()
    rows = []
    for row_number, occurrence in enumerate(sorted(occurrences), start=1):
        raw = {field: indexed[field].get(occurrence) for field in field_names}
        if not any(raw.values()):
            continue
        date_from = _parse_date(raw["date_from"], f"التكليف {row_number}: من تاريخ", unresolved) if raw["date_from"] else None
        date_to = _parse_date(raw["date_to"], f"التكليف {row_number}: إلى تاريخ", unresolved) if raw["date_to"] else None
        resolved = {}
        labels = {}
        for field, (model, label) in ORG_FIELDS.items():
            value, resolved_label = _resolve_named_model(model, raw[field], f"التكليف {row_number}: {label}", unresolved)
            resolved[field] = value
            labels[field] = resolved_label
        manager_id, manager_label = _resolve_manager(raw["direct_manager_user_id"], f"التكليف {row_number}: المسؤول المباشر", unresolved)
        resolved["direct_manager_user_id"] = manager_id
        labels["direct_manager_user_id"] = manager_label
        for field, category, label in (
            ("work_governorate_lookup_id", "WORK_GOV", "محافظة العمل"),
            ("work_location_lookup_id", "WORK_LOCATION", "موقع العمل"),
            ("admin_title_lookup_id", "ADMIN_TITLE", "المسمى الإداري"),
        ):
            value, resolved_label = _resolve_lookup(
                category,
                raw[field],
                f"التكليف {row_number}: {label}",
                unresolved,
                create_missing=create_missing,
                created_lookups=created_lookups,
            )
            resolved[field] = value
            labels[field] = resolved_label
        match = next((item for item in existing if (
            _clean(item.date_from) == _clean(date_from)
            and _clean(item.date_to) == _clean(date_to)
            and item.organization_id == resolved["organization_id"]
            and item.department_id == resolved["department_id"]
        )), None)
        rows.append({
            "match_id": match.id if match else None,
            "action": "update" if match else "create",
            "date_from": date_from,
            "date_to": date_to,
            **resolved,
            "labels": labels,
            "details": raw["details"],
        })
    return rows


def apply_employee_import_payload(
    payload: dict,
    employee: User,
    reviewer_id: int,
    *,
    create_missing_lookups: bool = False,
) -> dict:
    plan = build_employee_import_plan(
        payload,
        employee,
        create_missing_lookups=create_missing_lookups,
    )
    if plan["unresolved"]:
        raise EmployeeDataImportError(
            "توجد قيم لم تُطابق مع القوائم أو الهيكلية؛ عالجها قبل الاعتماد.",
            plan=plan,
        )

    employee_file = EmployeeFile.query.filter_by(user_id=employee.id).first()
    created_employee_file = employee_file is None
    if not employee_file:
        employee_file = EmployeeFile(user_id=employee.id, created_at=datetime.utcnow())
        db.session.add(employee_file)

    updated_fields = []
    for operation in plan["operations"]:
        if not operation["changed"]:
            continue
        setattr(employee_file, operation["field"], operation["resolved"])
        updated_fields.append(operation["field"])
        if operation["field"] == "full_name_quad":
            employee.name = operation["resolved"]

    employee_file.updated_at = datetime.utcnow()
    employee_file.updated_by_id = reviewer_id

    summary = {
        "employee_file_created": created_employee_file,
        "updated_fields": updated_fields,
        "created_lookups": plan["created_lookups"],
        "dependents": {"created": 0, "updated": 0},
        "qualifications": {"created": 0, "updated": 0},
        "secondments": {"created": 0, "updated": 0},
    }

    for row in plan["dependents"]:
        item = EmployeeDependent.query.get(row["match_id"]) if row["match_id"] else EmployeeDependent(user_id=employee.id, created_at=datetime.utcnow())
        if not row["match_id"]:
            db.session.add(item)
        for field in ("full_name", "relation_lookup_id", "national_id", "gender_lookup_id", "birth_date", "allowance"):
            if row.get(field) is not None:
                setattr(item, field, row[field])
        item.updated_at = datetime.utcnow()
        item.updated_by_id = reviewer_id
        summary["dependents"]["updated" if row["match_id"] else "created"] += 1

    for row in plan["qualifications"]:
        item = EmployeeQualification.query.get(row["match_id"]) if row["match_id"] else EmployeeQualification(user_id=employee.id, created_at=datetime.utcnow())
        if not row["match_id"]:
            db.session.add(item)
        for field in (
            "degree_lookup_id", "specialization_lookup_id", "grade_lookup_id", "qualification_date",
            "university_lookup_id", "country_lookup_id", "notes",
        ):
            if row.get(field) is not None:
                setattr(item, field, row[field])
        item.updated_at = datetime.utcnow()
        item.updated_by_id = reviewer_id
        summary["qualifications"]["updated" if row["match_id"] else "created"] += 1

    for row in plan["secondments"]:
        item = EmployeeSecondment.query.get(row["match_id"]) if row["match_id"] else EmployeeSecondment(user_id=employee.id, created_at=datetime.utcnow())
        if not row["match_id"]:
            db.session.add(item)
        for field in (
            "date_from", "date_to", "organization_id", "directorate_id", "department_id", "division_id",
            "direct_manager_user_id", "work_governorate_lookup_id", "work_location_lookup_id",
            "admin_title_lookup_id", "details",
        ):
            if row.get(field) is not None:
                setattr(item, field, row[field])
        item.updated_at = datetime.utcnow()
        item.updated_by_id = reviewer_id
        summary["secondments"]["updated" if row["match_id"] else "created"] += 1

    db.session.flush()
    return summary
