from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import func, or_

from extensions import db
from models import (
    InvFixedAsset,
    InvFixedAssetCycle,
    InvFixedAssetEntry,
    InvFixedAssetScanLog,
    InvAssetDocument,
    InvAssetDocumentLine,
)


ASSET_CONDITIONS = {
    "NEW": "جديد",
    "GOOD": "جيد",
    "FAIR": "متوسط",
    "DAMAGED": "متضرر",
    "UNUSABLE": "غير صالح للاستخدام",
}

ASSET_LIFECYCLE_STATUSES = {
    "ACTIVE": "قيد الاستخدام",
    "IN_STORE": "في المستودع",
    "MAINTENANCE": "قيد الصيانة",
    "LOST": "مفقود",
    "DISPOSED": "متلف/مستبعد",
}

CYCLE_STATUSES = {
    "ACTIVE": "جرد جارٍ",
    "CLOSED": "مغلق",
}

INVENTORY_RESULT_LABELS = {
    "PENDING": "لم يُجرد بعد",
    "MATCHED": "مطابق",
    "MOVED": "تغيّر الموقع أو العهدة",
    "CONDITION_CHANGED": "تغيّرت الحالة",
    "MOVED_AND_CONDITION_CHANGED": "تغيّر الموقع والحالة",
    "RECOVERED": "تم العثور عليه بعد فقدانه",
    "NEW_SINCE_PREVIOUS": "لم يكن في الجرد السابق",
    "UNEXPECTED": "خارج نطاق الجرد",
    "MISSING": "غير موجود",
    "STILL_MISSING": "ما زال غير موجود",
}

INVENTORY_RESULT_COLORS = {
    "PENDING": "secondary",
    "MATCHED": "success",
    "MOVED": "warning",
    "CONDITION_CHANGED": "info",
    "MOVED_AND_CONDITION_CHANGED": "warning",
    "RECOVERED": "primary",
    "NEW_SINCE_PREVIOUS": "primary",
    "UNEXPECTED": "danger",
    "MISSING": "danger",
    "STILL_MISSING": "dark",
}

MISSING_RESULTS = {"MISSING", "STILL_MISSING"}


def normalize_asset_identifier(raw_value: str | None) -> str:
    value = unquote(str(raw_value or "").strip())
    if not value:
        return ""

    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        query = parse_qs(parsed.query)
        if query.get("code"):
            value = query["code"][0]
        else:
            value = parsed.path.rstrip("/").rsplit("/", 1)[-1]

    value = value.strip().strip("/")
    upper_value = value.upper()
    for prefix in ("ASSET:", "FIXED-ASSET:", "FIXED_ASSET:", "FA:"):
        if upper_value.startswith(prefix):
            return value[len(prefix):].strip()
    return value


def find_asset_by_identifier(raw_value: str | None) -> InvFixedAsset | None:
    identifier = normalize_asset_identifier(raw_value)
    if not identifier:
        return None
    folded = identifier.casefold()
    return (
        InvFixedAsset.query
        .filter(
            or_(
                func.lower(InvFixedAsset.asset_tag) == folded,
                func.lower(InvFixedAsset.qr_token) == folded,
            )
        )
        .first()
    )


def _previous_expected_value(previous_entry, observed_name: str, expected_name: str, fallback):
    if previous_entry is None:
        return fallback
    if previous_entry.scanned_at and previous_entry.result_status not in MISSING_RESULTS:
        observed = getattr(previous_entry, observed_name)
        if observed is not None:
            return observed
    expected = getattr(previous_entry, expected_name)
    return fallback if expected is None else expected


def _previous_location(previous_entry, asset: InvFixedAsset) -> tuple[int | None, int | None]:
    return (
        _previous_expected_value(
            previous_entry,
            "observed_warehouse_id",
            "expected_warehouse_id",
            asset.warehouse_id,
        ),
        _previous_expected_value(
            previous_entry,
            "observed_room_id",
            "expected_room_id",
            asset.room_id,
        ),
    )


def _matches_scope(
    cycle: InvFixedAssetCycle,
    asset: InvFixedAsset,
    previous_entry: InvFixedAssetEntry | None = None,
) -> bool:
    def location_matches(warehouse_id, room_id) -> bool:
        if cycle.scope_warehouse_id and warehouse_id != cycle.scope_warehouse_id:
            return False
        if cycle.scope_room_id and room_id != cycle.scope_room_id:
            return False
        return True

    if location_matches(asset.warehouse_id, asset.room_id):
        return True
    if previous_entry is not None:
        previous_warehouse_id, previous_room_id = _previous_location(previous_entry, asset)
        return location_matches(previous_warehouse_id, previous_room_id)
    return False


def populate_cycle_entries(cycle: InvFixedAssetCycle) -> int:
    if cycle.id is None:
        db.session.flush()

    existing_asset_ids = {
        asset_id
        for (asset_id,) in (
            db.session.query(InvFixedAssetEntry.asset_id)
            .filter(InvFixedAssetEntry.cycle_id == cycle.id)
            .all()
        )
    }

    previous_entries = {}
    if cycle.previous_cycle_id:
        previous_entries = {
            entry.asset_id: entry
            for entry in (
                InvFixedAssetEntry.query
                .filter(InvFixedAssetEntry.cycle_id == cycle.previous_cycle_id)
                .all()
            )
        }

    assets = (
        InvFixedAsset.query
        .filter(InvFixedAsset.is_active.is_(True))
        .filter(InvFixedAsset.lifecycle_status != "DISPOSED")
        .order_by(InvFixedAsset.id.asc())
        .all()
    )

    candidates = {asset.id: asset for asset in assets}
    if cycle.previous_cycle_id:
        for asset_id, previous_entry in previous_entries.items():
            asset = previous_entry.asset
            if asset and asset.lifecycle_status != "DISPOSED":
                candidates.setdefault(asset_id, asset)

    added = 0
    for asset in candidates.values():
        if asset.id in existing_asset_ids:
            continue
        previous_entry = previous_entries.get(asset.id)
        if not _matches_scope(cycle, asset, previous_entry):
            continue

        expected_warehouse_id, expected_room_id = _previous_location(previous_entry, asset)
        previous_result = None
        if cycle.previous_cycle_id:
            previous_result = (
                previous_entry.result_status
                if previous_entry is not None
                else "NOT_IN_PREVIOUS"
            )

        db.session.add(InvFixedAssetEntry(
            cycle_id=cycle.id,
            asset_id=asset.id,
            was_expected=True,
            previous_result_status=previous_result,
            expected_warehouse_id=expected_warehouse_id,
            expected_room_id=expected_room_id,
            expected_custodian_user_id=_previous_expected_value(
                previous_entry,
                "observed_custodian_user_id",
                "expected_custodian_user_id",
                asset.custodian_user_id,
            ),
            expected_condition=_previous_expected_value(
                previous_entry,
                "observed_condition",
                "expected_condition",
                asset.asset_condition,
            ),
            expected_lifecycle_status=_previous_expected_value(
                previous_entry,
                "observed_lifecycle_status",
                "expected_lifecycle_status",
                asset.lifecycle_status,
            ),
            result_status="PENDING",
            scan_count=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
        added += 1

    return added


def calculate_inventory_result(entry: InvFixedAssetEntry) -> str:
    if not entry.scanned_at:
        return "PENDING"
    if not entry.was_expected:
        return "UNEXPECTED"
    if entry.previous_result_status in MISSING_RESULTS:
        return "RECOVERED"
    if entry.previous_result_status == "NOT_IN_PREVIOUS":
        return "NEW_SINCE_PREVIOUS"

    moved = any((
        entry.expected_warehouse_id != entry.observed_warehouse_id,
        entry.expected_room_id != entry.observed_room_id,
        entry.expected_custodian_user_id != entry.observed_custodian_user_id,
    ))
    condition_changed = any((
        (entry.expected_condition or "") != (entry.observed_condition or ""),
        (entry.expected_lifecycle_status or "") != (entry.observed_lifecycle_status or ""),
    ))
    if moved and condition_changed:
        return "MOVED_AND_CONDITION_CHANGED"
    if moved:
        return "MOVED"
    if condition_changed:
        return "CONDITION_CHANGED"
    return "MATCHED"


def record_asset_scan(
    cycle: InvFixedAssetCycle,
    asset: InvFixedAsset,
    *,
    scanned_by_id: int | None,
    observed_warehouse_id: int | None,
    observed_room_id: int | None,
    observed_custodian_user_id: int | None,
    observed_condition: str | None,
    observed_lifecycle_status: str | None,
    note: str | None = None,
    source: str = "MANUAL",
    device_info: str | None = None,
) -> InvFixedAssetEntry:
    if cycle.status != "ACTIVE":
        raise ValueError("لا يمكن التسجيل في دورة جرد مغلقة.")

    entry = InvFixedAssetEntry.query.filter_by(cycle_id=cycle.id, asset_id=asset.id).first()
    if entry is None:
        entry = InvFixedAssetEntry(
            cycle_id=cycle.id,
            asset_id=asset.id,
            was_expected=False,
            previous_result_status="NOT_IN_SCOPE",
            expected_warehouse_id=asset.warehouse_id,
            expected_room_id=asset.room_id,
            expected_custodian_user_id=asset.custodian_user_id,
            expected_condition=asset.asset_condition,
            expected_lifecycle_status=asset.lifecycle_status,
            result_status="PENDING",
            scan_count=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.flush()

    result_before_scan = entry.result_status
    scanned_at = datetime.utcnow()
    clean_source = (source or "MANUAL").strip().upper()[:20] or "MANUAL"
    entry.observed_warehouse_id = observed_warehouse_id
    entry.observed_room_id = observed_room_id
    entry.observed_custodian_user_id = observed_custodian_user_id
    entry.observed_condition = observed_condition or asset.asset_condition or "GOOD"
    entry.observed_lifecycle_status = observed_lifecycle_status or asset.lifecycle_status or "ACTIVE"
    entry.scanned_at = scanned_at
    entry.scanned_by_id = scanned_by_id
    entry.scan_count = int(entry.scan_count or 0) + 1
    entry.last_scan_source = clean_source
    entry.updated_at = scanned_at
    if note is not None:
        entry.note = note.strip() or None
    entry.result_status = calculate_inventory_result(entry)

    db.session.add(InvFixedAssetScanLog(
        entry_id=entry.id,
        cycle_id=cycle.id,
        asset_id=asset.id,
        scanned_by_id=scanned_by_id,
        scanned_at=scanned_at,
        source=clean_source,
        previous_result_status=result_before_scan,
        result_status=entry.result_status,
        observed_warehouse_id=entry.observed_warehouse_id,
        observed_room_id=entry.observed_room_id,
        observed_custodian_user_id=entry.observed_custodian_user_id,
        observed_condition=entry.observed_condition,
        observed_lifecycle_status=entry.observed_lifecycle_status,
        note=entry.note,
        device_info=(device_info or "").strip()[:255] or None,
    ))
    return entry


def close_inventory_cycle(
    cycle: InvFixedAssetCycle,
    *,
    closed_by_id: int | None,
    apply_observed_values: bool = False,
    apply_custodian_values: bool = False,
) -> dict[str, int]:
    if cycle.status != "ACTIVE":
        raise ValueError("دورة الجرد مغلقة بالفعل.")

    missing_count = 0
    reconciled_count = 0
    now = datetime.utcnow()
    for entry in cycle.entries:
        if not entry.scanned_at:
            entry.result_status = (
                "STILL_MISSING"
                if entry.previous_result_status in MISSING_RESULTS
                else "MISSING"
            )
            entry.updated_at = now
            missing_count += 1
            continue

        if apply_observed_values and entry.asset:
            asset = entry.asset
            if ((apply_custodian_values and asset.custodian_user_id != entry.observed_custodian_user_id) or asset.lifecycle_status != entry.observed_lifecycle_status) and InvAssetDocumentLine.query.filter_by(asset_id=asset.id).first():
                raise ValueError("توجد عهدة موثقة لبعض الأصول؛ أغلق الجرد دون تحديث السجل ثم نفذ نقل أو إسقاط العهدة من حركات العهدة.")
            asset.warehouse_id = entry.observed_warehouse_id
            asset.room_id = entry.observed_room_id
            if apply_custodian_values:
                asset.custodian_user_id = entry.observed_custodian_user_id
            asset.asset_condition = entry.observed_condition or asset.asset_condition
            asset.lifecycle_status = entry.observed_lifecycle_status or asset.lifecycle_status
            if asset.lifecycle_status == "DISPOSED":
                asset.is_active = False
            asset.updated_by_id = closed_by_id
            asset.updated_at = now
            reconciled_count += 1

    cycle.status = "CLOSED"
    cycle.closed_at = now
    cycle.closed_by_id = closed_by_id
    return {"missing": missing_count, "reconciled": reconciled_count}


def reopen_inventory_cycle(cycle: InvFixedAssetCycle) -> int:
    if InvAssetDocument.query.filter(InvAssetDocument.cycle_id == cycle.id, InvAssetDocument.status.notin_(["CANCELLED", "REJECTED"])).first():
        raise ValueError("صدرت تقارير اعتماد لهذه الدورة. ألغ التقارير المعلقة أولاً، أو أنشئ دورة جديدة إن صدرت العهدة.")
    if cycle.status != "CLOSED":
        raise ValueError("دورة الجرد ليست مغلقة.")
    reset_count = 0
    now = datetime.utcnow()
    for entry in cycle.entries:
        if not entry.scanned_at and entry.result_status in MISSING_RESULTS:
            entry.result_status = "PENDING"
            entry.updated_at = now
            reset_count += 1
    cycle.status = "ACTIVE"
    cycle.closed_at = None
    cycle.closed_by_id = None
    return reset_count


def cycle_result_counts(cycle_id: int) -> dict[str, int]:
    counts = {key: 0 for key in INVENTORY_RESULT_LABELS}
    rows = (
        db.session.query(InvFixedAssetEntry.result_status, func.count(InvFixedAssetEntry.id))
        .filter(InvFixedAssetEntry.cycle_id == cycle_id)
        .group_by(InvFixedAssetEntry.result_status)
        .all()
    )
    for status, count in rows:
        counts[status or "PENDING"] = int(count or 0)
    counts["TOTAL"] = sum(
        count for status, count in counts.items()
        if status != "TOTAL"
    )
    counts["SCANNED"] = counts["TOTAL"] - counts.get("PENDING", 0) - counts.get("MISSING", 0) - counts.get("STILL_MISSING", 0)
    return counts
