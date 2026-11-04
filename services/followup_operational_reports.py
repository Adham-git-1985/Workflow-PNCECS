"""Operational report sections embedded in employee accomplishment reports.

The accomplishment report is intentionally the read-only presentation layer for
these sections.  The queries below keep the existing warehouse and transport
screens as the source of truth while limiting the data to the accomplishment
period and to the employee's operational permissions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
import json

from sqlalchemy import func
from sqlalchemy.orm import aliased

from extensions import db
from models import (
    InvEmployeeRequest,
    InvEmployeeRequestLine,
    InvIssueVoucher,
    InvItem,
    InvItemCategory,
    InvReturnVoucherLine,
    InvWarehouse,
    TransportDriver,
    TransportPermit,
    TransportVehicle,
    TransportZone,
    User,
)


INVENTORY_CONSUMABLE_PERMISSION = "INVENTORY_REQUEST_APPROVE"
INVENTORY_TECH_PERMISSION = "INVENTORY_TECH_WAREHOUSE_APPROVE"
INVENTORY_MAINTENANCE_PERMISSION = "INVENTORY_ADMIN_MAINTENANCE_APPROVE"
TRANSPORT_MANAGER_PERMISSION = "TRANSPORT_MANAGER_APPROVE"
TRANSPORT_ALTERNATE_MANAGER_PERMISSION = "TRANSPORT_DIRECTOR_APPROVE"

# The employee controls which of the permission-backed operational sections
# are included in their own accomplishment report.  Permission checks remain
# authoritative: a section is never exposed just because its checkbox was
# saved in a report.
FOLLOWUP_OPERATIONAL_SECTION_OPTIONS = (
    {
        "key": "consumables",
        "label": "تقرير المستهلكات من المستودع",
        "description": "حركات صرف وإرجاع المواد الاستهلاكية خلال فترة التقرير.",
        "permissions": (INVENTORY_CONSUMABLE_PERMISSION,),
    },
    {
        "key": "technology_requests",
        "label": "تقرير المستودع التكنولوجي",
        "description": "طلبات الأجهزة والمواد التكنولوجية المرتبطة بصلاحية الموظف.",
        "permissions": (INVENTORY_TECH_PERMISSION,),
    },
    {
        "key": "maintenance_requests",
        "label": "تقرير الصيانة من المستودع",
        "description": "طلبات الصيانة والأثاث المرتبطة بصلاحية الموظف.",
        "permissions": (INVENTORY_MAINTENANCE_PERMISSION,),
    },
    {
        "key": "transport_movements",
        "label": "متابعة حركات النقل",
        "description": "حركات النقل التي تقع ضمن صلاحية مدير النقل أو البديل.",
        "permissions": (
            TRANSPORT_MANAGER_PERMISSION,
            TRANSPORT_ALTERNATE_MANAGER_PERMISSION,
        ),
    },
)

ROUTE_TECH = "TECH"
ROUTE_ADMIN_MAINTENANCE = "ADMIN_MAINTENANCE"

REPORT_ROW_LIMIT = 250

_INVENTORY_STATUS_LABELS = {
    "SUBMITTED": "قيد الاعتماد",
    "APPROVED": "معتمد ومصروف",
    "PARTIALLY_RETURNED": "معاد جزئياً",
    "REJECTED": "مرفوض",
    "CANCELLED": "ملغى",
    "RETURNED": "معاد بالكامل",
}
_TRANSPORT_STATUS_LABELS = {
    "SUBMITTED": "قيد الاعتماد",
    "APPROVED": "معتمد",
    "REJECTED": "مرفوض",
    "COMPLETED": "مكتمل",
    "CANCELLED": "ملغى",
    "DRAFT": "مسودة",
}
_TRANSPORT_STAGE_LABELS = {
    "MANAGER": "المدير المباشر",
    "TRANSPORT": "مسؤول الحركة والنقل",
    "ADMIN": "مدير الشؤون الإدارية",
    "ADMIN_INFO": "مدير الشؤون الإدارية للاطلاع",
    "DONE": "مكتمل",
}


def _has_permission(user, key: str) -> bool:
    try:
        return bool(user and user.has_perm(key))
    except Exception:
        return False


def _as_visibility_value(value, default=True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def get_followup_operational_visibility(report) -> dict[str, bool]:
    """Return saved section visibility, defaulting unconfigured reports to hidden."""
    visibility = {
        option["key"]: False
        for option in FOLLOWUP_OPERATIONAL_SECTION_OPTIONS
    }
    raw = getattr(report, "operational_sections_json", None)
    if not raw:
        return visibility
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return visibility
    if not isinstance(payload, dict):
        return visibility
    for key in visibility:
        if key in payload:
            visibility[key] = _as_visibility_value(payload[key], visibility[key])
    return visibility


def get_followup_operational_options(report) -> list[dict]:
    """Return checkbox options applicable to the employee in the report."""
    employee = getattr(report, "employee", None)
    visibility = get_followup_operational_visibility(report)
    options = []
    for option in FOLLOWUP_OPERATIONAL_SECTION_OPTIONS:
        if any(_has_permission(employee, permission) for permission in option["permissions"]):
            options.append({
                "key": option["key"],
                "label": option["label"],
                "description": option["description"],
                "visible": visibility[option["key"]],
            })
    return options


def _period(report) -> tuple[date, date, datetime, datetime]:
    start = getattr(report, "period_start", None) or date.today()
    end = getattr(report, "period_end", None) or start
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    if end < start:
        end = start
    return start, end, datetime.combine(start, time.min), datetime.combine(
        end + timedelta(days=1), time.min
    )


def _item_label(name, variant=None, code=None) -> str:
    label = (name or code or "صنف غير محدد").strip()
    variant = (variant or "").strip()
    code = (code or "").strip()
    if variant:
        label = f"{label} — {variant}"
    if code and code not in label:
        label = f"{label} ({code})"
    return label


def _user_label(name, email=None) -> str:
    return (name or email or "غير محدد").strip()


def _metric(label: str, value, tone: str = "primary") -> dict:
    return {"label": label, "value": value, "tone": tone}


def _base_section(*, key: str, title: str, subtitle: str, icon: str, tone: str) -> dict:
    return {
        "key": key,
        "title": title,
        "subtitle": subtitle,
        "icon": icon,
        "tone": tone,
        "metrics": [],
        "rows": [],
        "truncated": False,
    }


def _consumables_section(report) -> dict:
    start, end, _start_at, _end_at = _period(report)
    return_totals = (
        db.session.query(
            InvReturnVoucherLine.source_request_line_id.label("line_id"),
            func.sum(InvReturnVoucherLine.qty).label("returned_qty"),
        )
        .filter(InvReturnVoucherLine.source_request_line_id.isnot(None))
        .group_by(InvReturnVoucherLine.source_request_line_id)
        .subquery()
    )
    item = InvItem
    requester = aliased(User)
    q = (
        db.session.query(
            InvEmployeeRequestLine.id.label("line_id"),
            InvEmployeeRequest.id.label("request_id"),
            InvEmployeeRequest.purpose,
            InvIssueVoucher.voucher_date,
            InvIssueVoucher.voucher_no,
            requester.name.label("requester_name"),
            requester.email.label("requester_email"),
            item.name.label("item_name"),
            item.variant.label("item_variant"),
            item.code.label("item_code"),
            item.unit.label("item_unit"),
            InvWarehouse.name.label("warehouse_name"),
            InvEmployeeRequestLine.approved_qty,
            func.coalesce(return_totals.c.returned_qty, 0).label("returned_qty"),
        )
        .join(
            InvEmployeeRequest,
            InvEmployeeRequest.id == InvEmployeeRequestLine.request_id,
        )
        .join(
            InvIssueVoucher,
            InvIssueVoucher.id == InvEmployeeRequestLine.issue_voucher_id,
        )
        .join(requester, requester.id == InvEmployeeRequest.requester_user_id)
        .join(item, item.id == InvEmployeeRequestLine.item_id)
        .outerjoin(InvWarehouse, InvWarehouse.id == InvEmployeeRequestLine.warehouse_id)
        .outerjoin(
            return_totals,
            return_totals.c.line_id == InvEmployeeRequestLine.id,
        )
        .filter(InvEmployeeRequest.status.in_(("APPROVED", "PARTIALLY_RETURNED")))
        # The request-line -> issue-voucher link is the source of truth for
        # employee consumption. Do not restrict the voucher kind here: older
        # and manually reconciled requests can use another kind while still
        # appearing in the warehouse employee-consumption report.
        .filter(InvIssueVoucher.voucher_date >= start.isoformat())
        .filter(InvIssueVoucher.voucher_date <= end.isoformat())
        .order_by(InvIssueVoucher.voucher_date.desc(), InvIssueVoucher.id.desc())
    )
    raw_rows = q.limit(REPORT_ROW_LIMIT + 1).all()
    section = _base_section(
        key="consumables",
        title="تقرير المستهلكات من المستودع",
        subtitle=f"حركات الصرف الصافي خلال الفترة {start.isoformat()} إلى {end.isoformat()}",
        icon="bi-box-seam",
        tone="primary",
    )
    section["truncated"] = len(raw_rows) > REPORT_ROW_LIMIT
    rows = []
    item_summary = defaultdict(float)
    employee_summary = defaultdict(float)
    total_returned = 0.0
    total_consumed = 0.0
    for row in raw_rows[:REPORT_ROW_LIMIT]:
        approved = max(0.0, float(row.approved_qty or 0))
        returned = min(approved, max(0.0, float(row.returned_qty or 0)))
        consumed = max(0.0, approved - returned)
        item_label = _item_label(row.item_name, row.item_variant, row.item_code)
        employee_label = _user_label(row.requester_name, row.requester_email)
        rows.append({
            "date": row.voucher_date or "-",
            "voucher_no": row.voucher_no or "-",
            "request_id": row.request_id,
            "employee": employee_label,
            "item": item_label,
            "unit": row.item_unit or "",
            "warehouse": row.warehouse_name or "-",
            "approved_qty": approved,
            "returned_qty": returned,
            "consumed_qty": consumed,
            "purpose": row.purpose or "",
        })
        item_summary[item_label] += consumed
        employee_summary[employee_label] += consumed
        total_returned += returned
        total_consumed += consumed
    section["rows"] = rows
    section["item_summary"] = sorted(item_summary.items(), key=lambda value: (-value[1], value[0]))
    section["employee_summary"] = sorted(employee_summary.items(), key=lambda value: (-value[1], value[0]))
    section["metrics"] = [
        _metric("عمليات الصرف", len(rows)),
        _metric("الصرف الصافي", round(total_consumed, 2), "success"),
        _metric("الإرجاع", round(total_returned, 2), "warning"),
    ]
    return section


def _route_section(report, *, route: str, key: str, permission: str, title: str, icon: str, tone: str) -> dict:
    start, end, start_at, end_at = _period(report)
    requester = aliased(User)
    q = (
        db.session.query(
            InvEmployeeRequest.id.label("request_id"),
            InvEmployeeRequest.created_at,
            InvEmployeeRequest.status,
            InvEmployeeRequest.approval_stage,
            InvEmployeeRequest.purpose,
            requester.name.label("requester_name"),
            requester.email.label("requester_email"),
            InvEmployeeRequestLine.requested_qty,
            InvEmployeeRequestLine.approved_qty,
            InvItem.name.label("item_name"),
            InvItem.variant.label("item_variant"),
            InvItem.code.label("item_code"),
            InvItem.unit.label("item_unit"),
            InvItemCategory.name.label("category_name"),
            InvWarehouse.name.label("warehouse_name"),
        )
        .join(
            InvEmployeeRequestLine,
            InvEmployeeRequestLine.request_id == InvEmployeeRequest.id,
        )
        .join(InvItem, InvItem.id == InvEmployeeRequestLine.item_id)
        .outerjoin(InvItemCategory, InvItemCategory.id == InvItem.category_id)
        .outerjoin(InvWarehouse, InvWarehouse.id == InvEmployeeRequestLine.warehouse_id)
        .join(requester, requester.id == InvEmployeeRequest.requester_user_id)
        .filter(InvEmployeeRequest.route_type == route)
        .filter(InvEmployeeRequest.status != "CANCELLED")
        .filter(InvEmployeeRequest.created_at >= start_at)
        .filter(InvEmployeeRequest.created_at < end_at)
        .order_by(InvEmployeeRequest.created_at.desc(), InvEmployeeRequest.id.desc())
    )
    raw_rows = q.limit(REPORT_ROW_LIMIT + 1).all()
    section = _base_section(
        key=key,
        title=title,
        subtitle=f"طلبات مرتبطة بالصلاحية خلال الفترة {start.isoformat()} إلى {end.isoformat()}",
        icon=icon,
        tone=tone,
    )
    section["permission"] = permission
    section["truncated"] = len(raw_rows) > REPORT_ROW_LIMIT
    rows = []
    request_ids = set()
    approved_total = 0.0
    for row in raw_rows[:REPORT_ROW_LIMIT]:
        request_ids.add(int(row.request_id))
        approved_total += max(0.0, float(row.approved_qty or 0))
        rows.append({
            "request_id": row.request_id,
            "date": row.created_at.strftime("%Y-%m-%d") if row.created_at else "-",
            "employee": _user_label(row.requester_name, row.requester_email),
            "item": _item_label(row.item_name, row.item_variant, row.item_code),
            "category": row.category_name or "-",
            "requested_qty": float(row.requested_qty or 0),
            "approved_qty": float(row.approved_qty or 0),
            "unit": row.item_unit or "",
            "warehouse": row.warehouse_name or "-",
            "status": _INVENTORY_STATUS_LABELS.get(row.status, row.status or "-"),
            "stage": row.approval_stage or "-",
            "stage_label": row.approval_stage or "-",
            "purpose": row.purpose or "",
        })
    for row in rows:
        row["stage_label"] = {
            "TECH_WAREHOUSE": "مدير المستودع التكنولوجي",
            "TECH_DIRECTOR": "مدير التكنولوجيا والمطبوعات",
            "ADMIN_MAINTENANCE": "مسؤول مستودع الصيانة والأثاث",
            "ADMIN_FINANCE": "مدير الشؤون الإدارية والمالية",
            "SECRETARY_GENERAL": "الأمين العام",
            "DONE": "مكتمل",
        }.get(row["stage"], row["stage"])
    section["rows"] = rows
    section["metrics"] = [
        _metric("الطلبات", len(request_ids)),
        _metric("بنود الأصناف", len(rows)),
        _metric("الكميات المعتمدة", round(approved_total, 2), "success"),
    ]
    return section


def _transport_section(report) -> dict:
    start, end, start_at, end_at = _period(report)
    requester = aliased(User)
    vehicle = aliased(TransportVehicle)
    driver = aliased(TransportDriver)
    origin = aliased(TransportZone)
    destination = aliased(TransportZone)
    movement_day = func.coalesce(TransportPermit.depart_at, TransportPermit.created_at)
    q = (
        db.session.query(
            TransportPermit.id,
            TransportPermit.ref_no,
            TransportPermit.purpose,
            TransportPermit.origin_text,
            TransportPermit.dest_text,
            TransportPermit.depart_at,
            TransportPermit.return_at,
            TransportPermit.created_at,
            TransportPermit.status,
            TransportPermit.approval_stage,
            requester.name.label("requester_name"),
            requester.email.label("requester_email"),
            vehicle.plate_no.label("vehicle_plate"),
            vehicle.label.label("vehicle_label"),
            driver.name.label("driver_name"),
            origin.name.label("origin_name"),
            destination.name.label("destination_name"),
        )
        .join(requester, requester.id == TransportPermit.requester_user_id)
        .outerjoin(vehicle, vehicle.id == TransportPermit.vehicle_id)
        .outerjoin(driver, driver.id == TransportPermit.driver_id)
        .outerjoin(origin, origin.id == TransportPermit.origin_zone_id)
        .outerjoin(destination, destination.id == TransportPermit.dest_zone_id)
        .filter(TransportPermit.is_deleted.is_(False))
        .filter(movement_day >= start_at, movement_day < end_at)
        .order_by(movement_day.desc(), TransportPermit.id.desc())
    )
    raw_rows = q.limit(REPORT_ROW_LIMIT + 1).all()
    section = _base_section(
        key="transport_movements",
        title="متابعة حركات النقل",
        subtitle=f"طلبات الحركة خلال الفترة {start.isoformat()} إلى {end.isoformat()}",
        icon="bi-car-front-fill",
        tone="info",
    )
    section["truncated"] = len(raw_rows) > REPORT_ROW_LIMIT
    rows = []
    status_counts = defaultdict(int)
    for row in raw_rows[:REPORT_ROW_LIMIT]:
        day = row.depart_at or row.created_at
        origin_label = row.origin_name or row.origin_text or "-"
        destination_label = row.destination_name or row.dest_text or "-"
        status_label = _TRANSPORT_STATUS_LABELS.get(row.status, row.status or "-")
        status_counts[status_label] += 1
        rows.append({
            "movement_id": row.id,
            "ref_no": row.ref_no or f"#{row.id}",
            "date": day.strftime("%Y-%m-%d %H:%M") if day else "-",
            "employee": _user_label(row.requester_name, row.requester_email),
            "route": f"{origin_label} ← {destination_label}",
            "origin": origin_label,
            "destination": destination_label,
            "purpose": row.purpose or "-",
            "vehicle": " — ".join(value for value in (row.vehicle_label, row.vehicle_plate) if value) or "-",
            "driver": row.driver_name or "-",
            "status": status_label,
            "stage": _TRANSPORT_STAGE_LABELS.get(row.approval_stage, row.approval_stage or "-"),
        })
    section["rows"] = rows
    section["metrics"] = [
        _metric("الحركات", len(rows)),
        _metric("المعتمدة", status_counts.get("معتمد", 0), "success"),
        _metric("قيد الاعتماد", status_counts.get("قيد الاعتماد", 0), "warning"),
    ]
    return section


def build_followup_operational_sections(report) -> list[dict]:
    """Build sections applicable to the employee represented by ``report``."""
    employee = getattr(report, "employee", None)
    if employee is None:
        return []

    visibility = get_followup_operational_visibility(report)
    sections = []
    if visibility["consumables"] and _has_permission(employee, INVENTORY_CONSUMABLE_PERMISSION):
        sections.append(_consumables_section(report))
    if visibility["technology_requests"] and _has_permission(employee, INVENTORY_TECH_PERMISSION):
        sections.append(_route_section(
            report,
            route=ROUTE_TECH,
            key="technology_requests",
            permission=INVENTORY_TECH_PERMISSION,
            title="تقرير طلبات التكنولوجيا من المستودع",
            icon="bi-pc-display",
            tone="success",
        ))
    if visibility["maintenance_requests"] and _has_permission(employee, INVENTORY_MAINTENANCE_PERMISSION):
        sections.append(_route_section(
            report,
            route=ROUTE_ADMIN_MAINTENANCE,
            key="maintenance_requests",
            permission=INVENTORY_MAINTENANCE_PERMISSION,
            title="تقرير طلبات الصيانة والأثاث من المستودع",
            icon="bi-tools",
            tone="warning",
        ))
    if visibility["transport_movements"] and (
        _has_permission(employee, TRANSPORT_MANAGER_PERMISSION) or _has_permission(
        employee, TRANSPORT_ALTERNATE_MANAGER_PERMISSION
        )
    ):
        sections.append(_transport_section(report))
    return sections
