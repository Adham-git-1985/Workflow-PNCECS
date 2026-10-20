from __future__ import annotations

from datetime import date, datetime
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import exists, func, insert, or_

from extensions import db
from models import (
    InvFixedAsset,
    InvFixedAssetCycle,
    InvFixedAssetCycleMember,
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
    """Create the expected entry rows for a cycle without hydrating the catalog.

    The old implementation loaded every fixed asset and every joined lookup
    relationship into Python, then loaded the complete previous-cycle entry
    objects again.  That made opening a committee increasingly expensive as
    the asset register grew.  This path only selects the scalar fields needed
    for scope matching and baseline calculation, and lets SQLite skip entries
    that already exist through correlated ``EXISTS`` predicates.
    """
    if cycle.id is None:
        db.session.flush()

    previous_entries = {}
    previous_cycle_id = cycle.previous_cycle_id
    if previous_cycle_id:
        previous_rows = (
            db.session.query(
                InvFixedAssetEntry.asset_id,
                InvFixedAssetEntry.expected_warehouse_id,
                InvFixedAssetEntry.expected_room_id,
                InvFixedAssetEntry.expected_custodian_user_id,
                InvFixedAssetEntry.expected_condition,
                InvFixedAssetEntry.expected_lifecycle_status,
                InvFixedAssetEntry.observed_warehouse_id,
                InvFixedAssetEntry.observed_room_id,
                InvFixedAssetEntry.observed_custodian_user_id,
                InvFixedAssetEntry.observed_condition,
                InvFixedAssetEntry.observed_lifecycle_status,
                InvFixedAssetEntry.result_status,
                InvFixedAssetEntry.scanned_at,
            )
            .filter(InvFixedAssetEntry.cycle_id == previous_cycle_id)
            .all()
        )
        previous_entries = {row.asset_id: row for row in previous_rows}

    existing_entry = exists().where(
        InvFixedAssetEntry.cycle_id == cycle.id,
        InvFixedAssetEntry.asset_id == InvFixedAsset.id,
    )
    previous_entry_exists = None
    if previous_cycle_id:
        previous_entry_exists = exists().where(
            InvFixedAssetEntry.cycle_id == previous_cycle_id,
            InvFixedAssetEntry.asset_id == InvFixedAsset.id,
        )

    assets_query = (
        db.session.query(
            InvFixedAsset.id.label("id"),
            InvFixedAsset.warehouse_id,
            InvFixedAsset.room_id,
            InvFixedAsset.custodian_user_id,
            InvFixedAsset.asset_condition,
            InvFixedAsset.lifecycle_status,
        )
        .filter(InvFixedAsset.lifecycle_status != "DISPOSED")
        .filter(~existing_entry)
    )
    if previous_entry_exists is None:
        assets_query = assets_query.filter(InvFixedAsset.is_active.is_(True))
    else:
        # Keep assets from the previous cycle even if they were archived after
        # that cycle; this preserves the recovery/missing-asset workflow.
        assets_query = assets_query.filter(
            or_(InvFixedAsset.is_active.is_(True), previous_entry_exists)
        )

    assets = assets_query.order_by(InvFixedAsset.id.asc()).all()

    added = 0
    entry_rows = []
    now = datetime.utcnow()
    for asset in assets:
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

        entry_rows.append({
            "cycle_id": cycle.id,
            "asset_id": asset.id,
            "was_expected": True,
            "previous_result_status": previous_result,
            "expected_warehouse_id": expected_warehouse_id,
            "expected_room_id": expected_room_id,
            "expected_custodian_user_id": _previous_expected_value(
                previous_entry,
                "observed_custodian_user_id",
                "expected_custodian_user_id",
                asset.custodian_user_id,
            ),
            "expected_condition": _previous_expected_value(
                previous_entry,
                "observed_condition",
                "expected_condition",
                asset.asset_condition,
            ),
            "expected_lifecycle_status": _previous_expected_value(
                previous_entry,
                "observed_lifecycle_status",
                "expected_lifecycle_status",
                asset.lifecycle_status,
            ),
            "result_status": "PENDING",
            "scan_count": 0,
            "created_at": now,
            "updated_at": now,
        })
        added += 1

    if entry_rows:
        # These rows are a derived snapshot, not objects the caller needs to
        # mutate in this transaction.  One executemany keeps a large opening
        # cycle from constructing thousands of ORM instances and flush events.
        db.session.execute(insert(InvFixedAssetEntry), entry_rows)
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


def _next_inventory_date(cycle: InvFixedAssetCycle) -> str:
    """Keep the annual inventory cadence when opening the next cycle."""
    try:
        current = date.fromisoformat(cycle.inventory_date)
        try:
            return current.replace(year=current.year + 1).isoformat()
        except ValueError:
            # February 29 has no representation in a non-leap following year.
            return date(current.year + 1, 2, 28).isoformat()
    except (TypeError, ValueError):
        return f"{date.today().year + 1:04d}-01-01"


def _next_inventory_cycle_code(year: int) -> str:
    prefix = f"FA-INV-{year}"
    sequence = 1
    while True:
        candidate = f"{prefix}-{sequence:03d}"
        if not InvFixedAssetCycle.query.filter_by(code=candidate).first():
            return candidate
        sequence += 1


def open_followup_inventory_cycle(
    cycle: InvFixedAssetCycle,
    *,
    created_by_id: int | None,
) -> InvFixedAssetCycle:
    """Open the next annual committee cycle exactly once after approval."""
    if cycle.status != "CLOSED":
        raise ValueError("يجب إغلاق دورة الجرد قبل فتح الدورة السنوية التالية.")

    existing = (
        InvFixedAssetCycle.query
        .filter(InvFixedAssetCycle.previous_cycle_id == cycle.id)
        .order_by(InvFixedAssetCycle.id.desc())
        .first()
    )
    if existing is not None:
        cycle.successor_cycle_id = existing.id
        return existing

    next_inventory_date = _next_inventory_date(cycle)
    next_year = int(next_inventory_date[:4])
    now = datetime.utcnow()
    next_cycle = InvFixedAssetCycle(
        code=_next_inventory_cycle_code(next_year),
        name=f"الجرد السنوي للأصول الثابتة {next_year}",
        inventory_date=next_inventory_date,
        committee_id=cycle.committee_id,
        committee_name=cycle.committee_name,
        previous_cycle_id=cycle.id,
        scope_warehouse_id=cycle.scope_warehouse_id,
        scope_room_id=cycle.scope_room_id,
        status="ACTIVE",
        note=f"دورة جديدة أنشئت تلقائياً بعد اعتماد تقرير اللجنة للدورة {cycle.code}.",
        started_at=now,
        created_by_id=created_by_id,
        created_at=now,
    )
    db.session.add(next_cycle)
    db.session.flush()

    members = (
        InvFixedAssetCycleMember.query
        .with_entities(
            InvFixedAssetCycleMember.user_id,
            InvFixedAssetCycleMember.member_role,
        )
        .filter(InvFixedAssetCycleMember.cycle_id == cycle.id)
        .all()
    )
    db.session.add_all([
        InvFixedAssetCycleMember(
            cycle_id=next_cycle.id,
            user_id=user_id,
            member_role=member_role,
            created_at=now,
        )
        for user_id, member_role in members
    ])
    db.session.flush()
    populate_cycle_entries(next_cycle)
    cycle.successor_cycle_id = next_cycle.id
    return next_cycle


def reopen_inventory_cycle(cycle: InvFixedAssetCycle) -> int:
    if cycle.finalized_at is not None:
        raise ValueError("اعتمد الأمين العام تقرير هذه الدورة؛ لا يمكن إعادة فتحها.")
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
    return cycle_result_counts_for_cycles([cycle_id]).get(
        int(cycle_id),
        {**{key: 0 for key in INVENTORY_RESULT_LABELS}, "TOTAL": 0, "SCANNED": 0},
    )


def cycle_result_counts_for_cycles(cycle_ids) -> dict[int, dict[str, int]]:
    """Aggregate result counts for many cycles with one grouped query."""
    normalized_ids = sorted({int(cycle_id) for cycle_id in (cycle_ids or []) if cycle_id})
    if not normalized_ids:
        return {}

    counts_by_cycle = {
        cycle_id: {key: 0 for key in INVENTORY_RESULT_LABELS}
        for cycle_id in normalized_ids
    }
    rows = (
        db.session.query(
            InvFixedAssetEntry.cycle_id,
            InvFixedAssetEntry.result_status,
            func.count(InvFixedAssetEntry.id),
        )
        .filter(InvFixedAssetEntry.cycle_id.in_(normalized_ids))
        .group_by(InvFixedAssetEntry.cycle_id, InvFixedAssetEntry.result_status)
        .all()
    )
    for cycle_id, status, count in rows:
        counts_by_cycle[int(cycle_id)][status or "PENDING"] = int(count or 0)
    for counts in counts_by_cycle.values():
        counts["TOTAL"] = sum(
            count for status, count in counts.items()
            if status != "TOTAL"
        )
        counts["SCANNED"] = counts["TOTAL"] - counts.get("PENDING", 0) - counts.get("MISSING", 0) - counts.get("STILL_MISSING", 0)
    return counts_by_cycle
