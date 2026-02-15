from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from . import portal_bp
from extensions import db
from utils.perms import perm_required
from models import (
    TransportVehicle,
    TransportDriver,
    TransportZone,
    TransportPermit,
    TransportTrip,
    TransportDriverTask,
    TransportDestination,
    TransportTripDestination,
    TransportMaintenance,
    TransportMaintenanceItem,
    TransportFuelFill,
    HRLookupItem,
    SystemSetting,
    AuditLog,
)


# -------------------------
# Helpers
# -------------------------
def _parse_dt(val: str) -> Optional[datetime]:
    s = (val or "").strip()
    if not s:
        return None
    try:
        # supports "YYYY-MM-DDTHH:MM"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _to_int(val: str) -> Optional[int]:
    s = (val or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return None


def _to_float(val: str) -> Optional[float]:
    s = (val or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _get_setting(key: str, default: str = "") -> str:
    try:
        row = SystemSetting.query.filter_by(key=key).first()
        if row and row.value is not None:
            return str(row.value)
    except Exception:
        pass
    return default


def _set_setting(key: str, value: str | None) -> None:
    key = (key or "").strip()
    if not key:
        return
    row = SystemSetting.query.filter_by(key=key).first()
    if not row:
        row = SystemSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value


def _audit(action: str, note: str, target_type: str = "TRANSPORT", target_id: int | None = None) -> None:
    try:
        db.session.add(AuditLog(
            user_id=current_user.id,
            action=action,
            note=note,
            target_type=target_type,
            target_id=target_id,
        ))
    except Exception:
        pass


# -------------------------
# Home (Dashboard)
# -------------------------
@portal_bp.route("/transport")
@login_required
@perm_required("TRANSPORT_READ")
def transport_home():
    stats = {
        "vehicles": 0,
        "drivers": 0,
        "permits_pending": 0,
        "trips_month": 0,
        "tasks_open": 0,
    }
    try:
        stats["vehicles"] = TransportVehicle.query.count()
        stats["drivers"] = TransportDriver.query.count()
        stats["permits_pending"] = TransportPermit.query.filter(TransportPermit.status == "SUBMITTED").count()

        now = datetime.now()
        month_start = datetime(now.year, now.month, 1)
        if now.month == 12:
            next_month = datetime(now.year + 1, 1, 1)
        else:
            next_month = datetime(now.year, now.month + 1, 1)
        stats["trips_month"] = TransportTrip.query.filter(TransportTrip.started_at >= month_start, TransportTrip.started_at < next_month).count()

        stats["tasks_open"] = TransportDriverTask.query.filter(TransportDriverTask.status.in_(["PENDING", "IN_PROGRESS"])).count()
    except Exception:
        pass

    return render_template("portal/transport/index.html", stats=stats)


# -------------------------
# Vehicles
# -------------------------
@portal_bp.route("/transport/vehicles")
@login_required
@perm_required("TRANSPORT_READ")
def transport_vehicles():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().upper()

    query = TransportVehicle.query
    if status in ("ACTIVE", "INACTIVE", "MAINTENANCE"):
        query = query.filter(TransportVehicle.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter((TransportVehicle.plate_no.ilike(like)) | (TransportVehicle.label.ilike(like)))

    items = query.order_by(TransportVehicle.status.asc(), TransportVehicle.plate_no.asc()).all()
    can_edit = current_user.has_perm("TRANSPORT_UPDATE") or current_user.has_perm("TRANSPORT_CREATE")
    can_delete = current_user.has_perm("TRANSPORT_DELETE")
    return render_template("portal/transport/vehicles_list.html", items=items, q=q, status=status, can_edit=can_edit, can_delete=can_delete)


@portal_bp.route("/transport/vehicles/new", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_CREATE")
def transport_vehicle_new():
    if request.method == "POST":
        plate_no = (request.form.get("plate_no") or "").strip()
        label = (request.form.get("label") or "").strip() or None
        vehicle_type = (request.form.get("vehicle_type") or "").strip() or None
        model = (request.form.get("model") or "").strip() or None
        year = _to_int(request.form.get("year") or "")
        status = ((request.form.get("status") or "ACTIVE").strip().upper() or "ACTIVE")
        odom = _to_float(request.form.get("current_odometer") or "") or 0.0
        notes = (request.form.get("notes") or "").strip() or None

        if not plate_no:
            flash("رقم اللوحة مطلوب.", "danger")
            return redirect(url_for("portal.transport_vehicle_new"))

        exists = TransportVehicle.query.filter_by(plate_no=plate_no).first()
        if exists:
            flash("رقم اللوحة موجود مسبقاً.", "warning")
            return redirect(url_for("portal.transport_vehicle_new"))

        row = TransportVehicle(
            plate_no=plate_no,
            label=label,
            vehicle_type=vehicle_type,
            model=model,
            year=year,
            status=status if status in ("ACTIVE", "INACTIVE", "MAINTENANCE") else "ACTIVE",
            current_odometer=odom,
            notes=notes,
            created_by_id=current_user.id,
        )
        db.session.add(row)
        db.session.flush()
        _audit("TRANSPORT_VEHICLE_CREATE", f"إضافة مركبة: {plate_no}", target_type="TRANSPORT_VEHICLE", target_id=row.id)
        db.session.commit()
        flash("تمت إضافة المركبة.", "success")
        return redirect(url_for("portal.transport_vehicles"))

    return render_template("portal/transport/vehicle_form.html", item=None)


@portal_bp.route("/transport/vehicles/<int:vehicle_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_vehicle_edit(vehicle_id: int):
    row = TransportVehicle.query.get_or_404(vehicle_id)

    if request.method == "POST":
        plate_no = (request.form.get("plate_no") or "").strip()
        label = (request.form.get("label") or "").strip() or None
        vehicle_type = (request.form.get("vehicle_type") or "").strip() or None
        model = (request.form.get("model") or "").strip() or None
        year = _to_int(request.form.get("year") or "")
        status = ((request.form.get("status") or "ACTIVE").strip().upper() or "ACTIVE")
        odom = _to_float(request.form.get("current_odometer") or "")
        notes = (request.form.get("notes") or "").strip() or None

        if not plate_no:
            flash("رقم اللوحة مطلوب.", "danger")
            return redirect(url_for("portal.transport_vehicle_edit", vehicle_id=vehicle_id))

        other = TransportVehicle.query.filter(TransportVehicle.plate_no == plate_no, TransportVehicle.id != row.id).first()
        if other:
            flash("رقم اللوحة مستخدم لمركبة أخرى.", "warning")
            return redirect(url_for("portal.transport_vehicle_edit", vehicle_id=vehicle_id))

        row.plate_no = plate_no
        row.label = label
        row.vehicle_type = vehicle_type
        row.model = model
        row.year = year
        row.status = status if status in ("ACTIVE", "INACTIVE", "MAINTENANCE") else row.status
        if odom is not None:
            row.current_odometer = odom
        row.notes = notes

        _audit("TRANSPORT_VEHICLE_UPDATE", f"تعديل مركبة: {row.plate_no}", target_type="TRANSPORT_VEHICLE", target_id=row.id)
        db.session.commit()
        flash("تم حفظ التعديل.", "success")
        return redirect(url_for("portal.transport_vehicles"))

    return render_template("portal/transport/vehicle_form.html", item=row)


@portal_bp.route("/transport/vehicles/<int:vehicle_id>/delete", methods=["POST"])
@login_required
@perm_required("TRANSPORT_DELETE")
def transport_vehicle_delete(vehicle_id: int):
    row = TransportVehicle.query.get_or_404(vehicle_id)
    plate = row.plate_no
    db.session.delete(row)
    _audit("TRANSPORT_VEHICLE_DELETE", f"حذف مركبة: {plate}", target_type="TRANSPORT_VEHICLE", target_id=vehicle_id)
    db.session.commit()
    flash("تم حذف المركبة.", "success")
    return redirect(url_for("portal.transport_vehicles"))


# -------------------------
# Drivers
# -------------------------
@portal_bp.route("/transport/drivers")
@login_required
@perm_required("TRANSPORT_READ")
def transport_drivers():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().upper()

    query = TransportDriver.query
    if status in ("ACTIVE", "INACTIVE"):
        query = query.filter(TransportDriver.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter((TransportDriver.name.ilike(like)) | (TransportDriver.phone.ilike(like)) | (TransportDriver.license_no.ilike(like)))

    items = query.order_by(TransportDriver.status.asc(), TransportDriver.name.asc()).all()
    can_edit = current_user.has_perm("TRANSPORT_UPDATE") or current_user.has_perm("TRANSPORT_CREATE")
    can_delete = current_user.has_perm("TRANSPORT_DELETE")
    return render_template("portal/transport/drivers_list.html", items=items, q=q, status=status, can_edit=can_edit, can_delete=can_delete)


@portal_bp.route("/transport/drivers/new", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_CREATE")
def transport_driver_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        phone = (request.form.get("phone") or "").strip() or None
        license_no = (request.form.get("license_no") or "").strip() or None
        status = ((request.form.get("status") or "ACTIVE").strip().upper() or "ACTIVE")
        notes = (request.form.get("notes") or "").strip() or None

        if not name:
            flash("اسم السائق مطلوب.", "danger")
            return redirect(url_for("portal.transport_driver_new"))

        row = TransportDriver(
            name=name,
            phone=phone,
            license_no=license_no,
            status=status if status in ("ACTIVE", "INACTIVE") else "ACTIVE",
            notes=notes,
            created_by_id=current_user.id,
        )
        db.session.add(row)
        db.session.flush()
        _audit("TRANSPORT_DRIVER_CREATE", f"إضافة سائق: {name}", target_type="TRANSPORT_DRIVER", target_id=row.id)
        db.session.commit()
        flash("تمت إضافة السائق.", "success")
        return redirect(url_for("portal.transport_drivers"))

    return render_template("portal/transport/driver_form.html", item=None)


@portal_bp.route("/transport/drivers/<int:driver_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_driver_edit(driver_id: int):
    row = TransportDriver.query.get_or_404(driver_id)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        phone = (request.form.get("phone") or "").strip() or None
        license_no = (request.form.get("license_no") or "").strip() or None
        status = ((request.form.get("status") or "ACTIVE").strip().upper() or "ACTIVE")
        notes = (request.form.get("notes") or "").strip() or None

        if not name:
            flash("اسم السائق مطلوب.", "danger")
            return redirect(url_for("portal.transport_driver_edit", driver_id=driver_id))

        row.name = name
        row.phone = phone
        row.license_no = license_no
        row.status = status if status in ("ACTIVE", "INACTIVE") else row.status
        row.notes = notes

        _audit("TRANSPORT_DRIVER_UPDATE", f"تعديل سائق: {row.name}", target_type="TRANSPORT_DRIVER", target_id=row.id)
        db.session.commit()
        flash("تم حفظ التعديل.", "success")
        return redirect(url_for("portal.transport_drivers"))

    return render_template("portal/transport/driver_form.html", item=row)


@portal_bp.route("/transport/drivers/<int:driver_id>/delete", methods=["POST"])
@login_required
@perm_required("TRANSPORT_DELETE")
def transport_driver_delete(driver_id: int):
    row = TransportDriver.query.get_or_404(driver_id)
    name = row.name
    db.session.delete(row)
    _audit("TRANSPORT_DRIVER_DELETE", f"حذف سائق: {name}", target_type="TRANSPORT_DRIVER", target_id=driver_id)
    db.session.commit()
    flash("تم حذف السائق.", "success")
    return redirect(url_for("portal.transport_drivers"))


# -------------------------
# Zones
# -------------------------
@portal_bp.route("/transport/zones")
@login_required
@perm_required("TRANSPORT_READ")
def transport_zones():
    q = (request.args.get("q") or "").strip()
    query = TransportZone.query
    if q:
        like = f"%{q}%"
        query = query.filter((TransportZone.name.ilike(like)) | (TransportZone.city.ilike(like)))

    items = query.order_by(TransportZone.is_active.desc(), TransportZone.name.asc()).all()
    can_edit = current_user.has_perm("TRANSPORT_UPDATE") or current_user.has_perm("TRANSPORT_CREATE")
    can_delete = current_user.has_perm("TRANSPORT_DELETE")
    return render_template("portal/transport/zones_list.html", items=items, q=q, can_edit=can_edit, can_delete=can_delete)


@portal_bp.route("/transport/zones/new", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_CREATE")
def transport_zone_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        city = (request.form.get("city") or "").strip() or None
        is_active = bool(request.form.get("is_active") == "1")
        notes = (request.form.get("notes") or "").strip() or None

        if not name:
            flash("اسم المنطقة مطلوب.", "danger")
            return redirect(url_for("portal.transport_zone_new"))

        exists = TransportZone.query.filter_by(name=name).first()
        if exists:
            flash("اسم المنطقة موجود مسبقاً.", "warning")
            return redirect(url_for("portal.transport_zone_new"))

        row = TransportZone(
            name=name,
            city=city,
            is_active=is_active,
            notes=notes,
            created_by_id=current_user.id,
        )
        db.session.add(row)
        db.session.flush()
        _audit("TRANSPORT_ZONE_CREATE", f"إضافة منطقة: {name}", target_type="TRANSPORT_ZONE", target_id=row.id)
        db.session.commit()
        flash("تمت إضافة المنطقة.", "success")
        return redirect(url_for("portal.transport_zones"))

    return render_template("portal/transport/zone_form.html", item=None)


@portal_bp.route("/transport/zones/<int:zone_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_zone_edit(zone_id: int):
    row = TransportZone.query.get_or_404(zone_id)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        city = (request.form.get("city") or "").strip() or None
        is_active = bool(request.form.get("is_active") == "1")
        notes = (request.form.get("notes") or "").strip() or None

        if not name:
            flash("اسم المنطقة مطلوب.", "danger")
            return redirect(url_for("portal.transport_zone_edit", zone_id=zone_id))

        other = TransportZone.query.filter(TransportZone.name == name, TransportZone.id != row.id).first()
        if other:
            flash("اسم المنطقة مستخدم مسبقاً.", "warning")
            return redirect(url_for("portal.transport_zone_edit", zone_id=zone_id))

        row.name = name
        row.city = city
        row.is_active = is_active
        row.notes = notes

        _audit("TRANSPORT_ZONE_UPDATE", f"تعديل منطقة: {row.name}", target_type="TRANSPORT_ZONE", target_id=row.id)
        db.session.commit()
        flash("تم حفظ التعديل.", "success")
        return redirect(url_for("portal.transport_zones"))

    return render_template("portal/transport/zone_form.html", item=row)


@portal_bp.route("/transport/zones/<int:zone_id>/delete", methods=["POST"])
@login_required
@perm_required("TRANSPORT_DELETE")
def transport_zone_delete(zone_id: int):
    row = TransportZone.query.get_or_404(zone_id)
    name = row.name
    db.session.delete(row)
    _audit("TRANSPORT_ZONE_DELETE", f"حذف منطقة: {name}", target_type="TRANSPORT_ZONE", target_id=zone_id)
    db.session.commit()
    flash("تم حذف المنطقة.", "success")
    return redirect(url_for("portal.transport_zones"))


# -------------------------
# Permits (Movement Authorizations)
# -------------------------
@portal_bp.route("/transport/permits")
@login_required
@perm_required("TRANSPORT_READ")
def transport_permits():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().upper()

    query = TransportPermit.query
    if status in ("DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "CANCELLED", "COMPLETED"):
        query = query.filter(TransportPermit.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter((TransportPermit.purpose.ilike(like)) | (TransportPermit.ref_no.ilike(like)))

    items = query.order_by(TransportPermit.created_at.desc(), TransportPermit.id.desc()).all()

    can_create = current_user.has_perm("TRANSPORT_CREATE")
    can_approve = current_user.has_perm("TRANSPORT_APPROVE")
    can_update = current_user.has_perm("TRANSPORT_UPDATE")
    return render_template("portal/transport/permits_list.html", items=items, q=q, status=status, can_create=can_create, can_approve=can_approve, can_update=can_update)


@portal_bp.route("/transport/permits/new", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_CREATE")
def transport_permit_new():
    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()
    drivers = TransportDriver.query.filter(TransportDriver.status == "ACTIVE").order_by(TransportDriver.name.asc()).all()
    zones = TransportZone.query.filter(TransportZone.is_active == True).order_by(TransportZone.name.asc()).all()  # noqa: E712

    if request.method == "POST":
        vehicle_id = _to_int(request.form.get("vehicle_id") or "")
        driver_id = _to_int(request.form.get("driver_id") or "")
        origin_zone_id = _to_int(request.form.get("origin_zone_id") or "")
        dest_zone_id = _to_int(request.form.get("dest_zone_id") or "")
        origin_text = (request.form.get("origin_text") or "").strip() or None
        dest_text = (request.form.get("dest_text") or "").strip() or None
        purpose = (request.form.get("purpose") or "").strip()
        passengers_count = _to_int(request.form.get("passengers_count") or "")
        depart_at = _parse_dt(request.form.get("depart_at") or "")
        return_at = _parse_dt(request.form.get("return_at") or "")
        note = (request.form.get("note") or "").strip() or None

        if not purpose:
            flash("سبب/غرض الحركة مطلوب.", "danger")
            return redirect(url_for("portal.transport_permit_new"))

        row = TransportPermit(
            requester_user_id=current_user.id,
            vehicle_id=vehicle_id,
            driver_id=driver_id,
            origin_zone_id=origin_zone_id,
            dest_zone_id=dest_zone_id,
            origin_text=origin_text,
            dest_text=dest_text,
            purpose=purpose,
            passengers_count=passengers_count,
            depart_at=depart_at,
            return_at=return_at,
            note=note,
            status="SUBMITTED",
            submitted_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.flush()
        _audit("TRANSPORT_PERMIT_CREATE", f"طلب إذن حركة: #{row.id}", target_type="TRANSPORT_PERMIT", target_id=row.id)
        db.session.commit()

        flash("تم إرسال إذن الحركة للاعتماد.", "success")
        return redirect(url_for("portal.transport_permits"))

    return render_template("portal/transport/permit_form.html", item=None, vehicles=vehicles, drivers=drivers, zones=zones)


@portal_bp.route("/transport/permits/<int:permit_id>")
@login_required
@perm_required("TRANSPORT_READ")
def transport_permit_view(permit_id: int):
    row = TransportPermit.query.get_or_404(permit_id)
    can_approve = current_user.has_perm("TRANSPORT_APPROVE") and row.status == "SUBMITTED"
    can_update = current_user.has_perm("TRANSPORT_UPDATE") and row.status in ("DRAFT", "SUBMITTED")
    return render_template("portal/transport/permit_view.html", item=row, can_approve=can_approve, can_update=can_update)


@portal_bp.route("/transport/permits/<int:permit_id>/approve", methods=["POST"])
@login_required
@perm_required("TRANSPORT_APPROVE")
def transport_permit_approve(permit_id: int):
    row = TransportPermit.query.get_or_404(permit_id)
    if row.status != "SUBMITTED":
        flash("لا يمكن اعتماد هذا الإذن بهذه الحالة.", "warning")
        return redirect(url_for("portal.transport_permit_view", permit_id=permit_id))

    decision_note = (request.form.get("decision_note") or "").strip() or None
    row.status = "APPROVED"
    row.approver_user_id = current_user.id
    row.decided_at = datetime.utcnow()
    row.decision_note = decision_note

    _audit("TRANSPORT_PERMIT_APPROVE", f"اعتماد إذن حركة: #{row.id}", target_type="TRANSPORT_PERMIT", target_id=row.id)
    db.session.commit()
    flash("تم اعتماد إذن الحركة.", "success")
    return redirect(url_for("portal.transport_permit_view", permit_id=permit_id))


@portal_bp.route("/transport/permits/<int:permit_id>/reject", methods=["POST"])
@login_required
@perm_required("TRANSPORT_APPROVE")
def transport_permit_reject(permit_id: int):
    row = TransportPermit.query.get_or_404(permit_id)
    if row.status != "SUBMITTED":
        flash("لا يمكن رفض هذا الإذن بهذه الحالة.", "warning")
        return redirect(url_for("portal.transport_permit_view", permit_id=permit_id))

    decision_note = (request.form.get("decision_note") or "").strip() or None
    row.status = "REJECTED"
    row.approver_user_id = current_user.id
    row.decided_at = datetime.utcnow()
    row.decision_note = decision_note

    _audit("TRANSPORT_PERMIT_REJECT", f"رفض إذن حركة: #{row.id}", target_type="TRANSPORT_PERMIT", target_id=row.id)
    db.session.commit()
    flash("تم رفض إذن الحركة.", "success")
    return redirect(url_for("portal.transport_permit_view", permit_id=permit_id))


# -------------------------
# Trips
# -------------------------
@portal_bp.route("/transport/trips")
@login_required
@perm_required("TRANSPORT_READ")
def transport_trips():
    q = (request.args.get("q") or "").strip()

    query = TransportTrip.query.filter(TransportTrip.is_deleted == False)  # noqa: E712
    if q:
        like = f"%{q}%"
        query = query.filter((TransportTrip.note.ilike(like)) | (TransportTrip.id.cast(db.String).ilike(like)))

    items = query.order_by(TransportTrip.started_at.desc(), TransportTrip.id.desc()).all()
    can_create = current_user.has_perm("TRANSPORT_CREATE")
    can_edit = current_user.has_perm("TRANSPORT_UPDATE")
    can_delete = current_user.has_perm("TRANSPORT_DELETE")
    return render_template(
        "portal/transport/trips_list.html",
        items=items,
        q=q,
        can_create=can_create,
        can_edit=can_edit,
        can_delete=can_delete,
    )



@portal_bp.route("/transport/trips/new", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_CREATE")
def transport_trip_new():
    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()
    drivers = TransportDriver.query.filter(TransportDriver.status == "ACTIVE").order_by(TransportDriver.name.asc()).all()
    permits = TransportPermit.query.filter(TransportPermit.status == "APPROVED").order_by(TransportPermit.id.desc()).limit(200).all()

    if request.method == "POST":
        permit_id = _to_int(request.form.get("permit_id") or "")
        vehicle_id = _to_int(request.form.get("vehicle_id") or "")
        driver_id = _to_int(request.form.get("driver_id") or "")

        started_at = _parse_dt(request.form.get("started_at") or "")
        ended_at = _parse_dt(request.form.get("ended_at") or "")

        start_odometer = _to_float(request.form.get("start_odometer") or "")
        end_odometer = _to_float(request.form.get("end_odometer") or "")
        note = (request.form.get("note") or "").strip() or None

        if not vehicle_id:
            flash("السيارة مطلوبة.", "danger")
            return redirect(url_for("portal.transport_trip_new"))

        if not started_at:
            flash("وقت الانطلاق مطلوب.", "danger")
            return redirect(url_for("portal.transport_trip_new"))

        distance_km = None
        if start_odometer is not None and end_odometer is not None:
            distance_km = max(0.0, float(end_odometer) - float(start_odometer))

        row = TransportTrip(
            permit_id=permit_id,
            vehicle_id=vehicle_id,
            driver_id=driver_id,
            started_at=started_at,
            ended_at=ended_at,
            start_odometer=start_odometer,
            end_odometer=end_odometer,
            distance_km=distance_km,
            note=note,
            created_by_id=current_user.id,
        )
        db.session.add(row)
        db.session.flush()

        # Update vehicle odometer if provided
        try:
            v = TransportVehicle.query.get(vehicle_id)
            if v and end_odometer is not None:
                v.current_odometer = float(end_odometer)
        except Exception:
            pass

        # Complete permit if ended
        try:
            if permit_id:
                p = TransportPermit.query.get(permit_id)
                if p and ended_at:
                    p.status = "COMPLETED"
        except Exception:
            pass

        _audit("TRANSPORT_TRIP_CREATE", f"تسجيل رحلة: #{row.id}", target_type="TRANSPORT_TRIP", target_id=row.id)
        db.session.commit()
        flash("تم تسجيل الرحلة.", "success")
        return redirect(url_for("portal.transport_trips"))

    return render_template("portal/transport/trip_form.html", item=None, vehicles=vehicles, drivers=drivers, permits=permits)


@portal_bp.route("/transport/trips/<int:trip_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_trip_edit(trip_id: int):
    row = TransportTrip.query.get_or_404(trip_id)
    if row.is_deleted:
        flash("هذه الرحلة محذوفة.", "warning")
        return redirect(url_for("portal.transport_trips"))

    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()
    drivers = TransportDriver.query.filter(TransportDriver.status == "ACTIVE").order_by(TransportDriver.name.asc()).all()
    permits = TransportPermit.query.filter(TransportPermit.status == "APPROVED").order_by(TransportPermit.id.desc()).limit(200).all()

    if request.method == "POST":
        permit_id = _to_int(request.form.get("permit_id") or "")
        vehicle_id = _to_int(request.form.get("vehicle_id") or "")
        driver_id = _to_int(request.form.get("driver_id") or "")

        started_at = _parse_dt(request.form.get("started_at") or "")
        ended_at = _parse_dt(request.form.get("ended_at") or "")

        start_odometer = _to_float(request.form.get("start_odometer") or "")
        end_odometer = _to_float(request.form.get("end_odometer") or "")
        note = (request.form.get("note") or "").strip() or None

        if not vehicle_id:
            flash("السيارة مطلوبة.", "danger")
            return redirect(url_for("portal.transport_trip_edit", trip_id=trip_id))

        if not started_at:
            flash("وقت الانطلاق مطلوب.", "danger")
            return redirect(url_for("portal.transport_trip_edit", trip_id=trip_id))

        distance_km = None
        if start_odometer is not None and end_odometer is not None:
            distance_km = max(0.0, float(end_odometer) - float(start_odometer))

        row.permit_id = permit_id
        row.vehicle_id = vehicle_id
        row.driver_id = driver_id
        row.started_at = started_at
        row.ended_at = ended_at
        row.start_odometer = start_odometer
        row.end_odometer = end_odometer
        row.distance_km = distance_km
        row.note = note

        # Update vehicle odometer if provided
        try:
            v = TransportVehicle.query.get(vehicle_id)
            if v and end_odometer is not None:
                v.current_odometer = float(end_odometer)
        except Exception:
            pass

        # Complete permit if ended
        try:
            if permit_id:
                p = TransportPermit.query.get(permit_id)
                if p and ended_at:
                    p.status = "COMPLETED"
        except Exception:
            pass

        _audit("TRANSPORT_TRIP_UPDATE", f"تعديل رحلة: #{row.id}", target_type="TRANSPORT_TRIP", target_id=row.id)
        db.session.commit()
        flash("تم تحديث الرحلة.", "success")
        return redirect(url_for("portal.transport_trips"))

    return render_template("portal/transport/trip_form.html", item=row, vehicles=vehicles, drivers=drivers, permits=permits)


@portal_bp.route("/transport/trips/<int:trip_id>/delete", methods=["POST"])
@login_required
@perm_required("TRANSPORT_DELETE")
def transport_trip_delete(trip_id: int):
    row = TransportTrip.query.get_or_404(trip_id)
    if row.is_deleted:
        flash("تم حذف الرحلة مسبقاً.", "info")
        return redirect(url_for("portal.transport_trips"))

    row.is_deleted = True
    row.deleted_at = datetime.utcnow()
    row.deleted_by_id = current_user.id

    _audit("TRANSPORT_TRIP_DELETE", f"حذف رحلة: #{row.id}", target_type="TRANSPORT_TRIP", target_id=row.id)
    db.session.commit()
    flash("تم حذف الرحلة.", "success")
    return redirect(url_for("portal.transport_trips"))




@portal_bp.route("/transport/tasks")
@login_required
@perm_required("TRANSPORT_READ")
def transport_tasks():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().upper()

    query = TransportDriverTask.query
    if status in ("PENDING", "IN_PROGRESS", "DONE", "CANCELLED"):
        query = query.filter(TransportDriverTask.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter((TransportDriverTask.title.ilike(like)) | (TransportDriverTask.description.ilike(like)))

    items = query.order_by(TransportDriverTask.created_at.desc(), TransportDriverTask.id.desc()).all()
    can_create = current_user.has_perm("TRANSPORT_CREATE")
    can_edit = current_user.has_perm("TRANSPORT_UPDATE")
    can_cancel = current_user.has_perm("TRANSPORT_UPDATE")
    return render_template(
        "portal/transport/tasks_list.html",
        items=items,
        q=q,
        status=status,
        can_create=can_create,
        can_edit=can_edit,
        can_cancel=can_cancel,
    )



@portal_bp.route("/transport/tasks/new", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_CREATE")
def transport_task_new():
    drivers = TransportDriver.query.filter(TransportDriver.status == "ACTIVE").order_by(TransportDriver.name.asc()).all()
    permits = TransportPermit.query.order_by(TransportPermit.id.desc()).limit(200).all()
    trips = TransportTrip.query.filter(TransportTrip.is_deleted == False).order_by(TransportTrip.id.desc()).limit(200).all()  # noqa: E712

    if request.method == "POST":
        driver_id = _to_int(request.form.get("driver_id") or "")
        permit_id = _to_int(request.form.get("permit_id") or "")
        trip_id = _to_int(request.form.get("trip_id") or "")
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        due_date = (request.form.get("due_date") or "").strip() or None
        status = ((request.form.get("status") or "PENDING").strip().upper() or "PENDING")

        if not driver_id:
            flash("السائق مطلوب.", "danger")
            return redirect(url_for("portal.transport_task_new"))
        if not title:
            flash("عنوان المهمة مطلوب.", "danger")
            return redirect(url_for("portal.transport_task_new"))

        row = TransportDriverTask(
            driver_id=driver_id,
            permit_id=permit_id,
            trip_id=trip_id,
            title=title,
            description=description,
            due_date=due_date,
            status=status if status in ("PENDING", "IN_PROGRESS", "DONE", "CANCELLED") else "PENDING",
            completed_at=(datetime.utcnow() if status == "DONE" else None),
            created_by_id=current_user.id,
        )
        db.session.add(row)
        db.session.flush()
        _audit("TRANSPORT_TASK_CREATE", f"مهمة سائق: #{row.id}", target_type="TRANSPORT_TASK", target_id=row.id)
        db.session.commit()
        flash("تم إنشاء المهمة.", "success")
        return redirect(url_for("portal.transport_tasks"))

    return render_template("portal/transport/task_form.html", item=None, drivers=drivers, permits=permits, trips=trips)


@portal_bp.route("/transport/tasks/<int:task_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_task_edit(task_id: int):
    row = TransportDriverTask.query.get_or_404(task_id)

    drivers = TransportDriver.query.filter(TransportDriver.status == "ACTIVE").order_by(TransportDriver.name.asc()).all()
    permits = TransportPermit.query.order_by(TransportPermit.id.desc()).limit(200).all()
    trips = TransportTrip.query.filter(TransportTrip.is_deleted == False).order_by(TransportTrip.id.desc()).limit(200).all()  # noqa: E712

    if request.method == "POST":
        driver_id = _to_int(request.form.get("driver_id") or "")
        permit_id = _to_int(request.form.get("permit_id") or "")
        trip_id = _to_int(request.form.get("trip_id") or "")
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        due_date = (request.form.get("due_date") or "").strip() or None
        status = ((request.form.get("status") or "PENDING").strip().upper() or "PENDING")

        if not driver_id:
            flash("السائق مطلوب.", "danger")
            return redirect(url_for("portal.transport_task_edit", task_id=task_id))
        if not title:
            flash("عنوان المهمة مطلوب.", "danger")
            return redirect(url_for("portal.transport_task_edit", task_id=task_id))

        row.driver_id = driver_id
        row.permit_id = permit_id
        row.trip_id = trip_id
        row.title = title
        row.description = description
        row.due_date = due_date
        row.status = status if status in ("PENDING", "IN_PROGRESS", "DONE", "CANCELLED") else "PENDING"

        if row.status == "DONE" and not row.completed_at:
            row.completed_at = datetime.utcnow()
        if row.status != "DONE":
            row.completed_at = None

        _audit("TRANSPORT_TASK_UPDATE", f"تعديل مهمة سائق: #{row.id}", target_type="TRANSPORT_TASK", target_id=row.id)
        db.session.commit()
        flash("تم تحديث المهمة.", "success")
        return redirect(url_for("portal.transport_tasks"))

    return render_template("portal/transport/task_form.html", item=row, drivers=drivers, permits=permits, trips=trips)


@portal_bp.route("/transport/tasks/<int:task_id>/cancel", methods=["POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_task_cancel(task_id: int):
    row = TransportDriverTask.query.get_or_404(task_id)
    if row.status == "CANCELLED":
        flash("المهمة ملغاة مسبقاً.", "info")
        return redirect(url_for("portal.transport_tasks"))

    row.status = "CANCELLED"
    row.completed_at = None

    _audit("TRANSPORT_TASK_CANCEL", f"إلغاء مهمة سائق: #{row.id}", target_type="TRANSPORT_TASK", target_id=row.id)
    db.session.commit()
    flash("تم إلغاء المهمة.", "success")
    return redirect(url_for("portal.transport_tasks"))






# -------------------------
# Lookups (optional - if HR lookup categories exist)
# -------------------------
TRANSPORT_LOOKUP_GARAGE = "TRANSPORT_GARAGE"
TRANSPORT_LOOKUP_MAINT_TYPE = "TRANSPORT_MAINT_TYPE"
TRANSPORT_LOOKUP_PAYMENT_METHOD = "TRANSPORT_PAYMENT_METHOD"
TRANSPORT_LOOKUP_FUEL_STATION = "TRANSPORT_FUEL_STATION"


def _lookup_items(category: str):
    try:
        return (
            HRLookupItem.query.filter(HRLookupItem.category == category, HRLookupItem.is_active == True)  # noqa: E712
            .order_by(HRLookupItem.sort_order.asc(), HRLookupItem.id.asc())
            .all()
        )
    except Exception:
        return []


# -------------------------
# Destinations
# -------------------------
@portal_bp.route("/transport/destinations")
@login_required
@perm_required("TRANSPORT_READ")
def transport_destinations():
    q = (request.args.get("q") or "").strip()
    active = (request.args.get("active") or "").strip()

    qry = TransportDestination.query
    if active in ("1", "0"):
        qry = qry.filter(TransportDestination.is_active == (active == "1"))
    if q:
        like = f"%{q}%"
        qry = qry.filter(TransportDestination.name.ilike(like))

    items = qry.order_by(TransportDestination.is_active.desc(), TransportDestination.name.asc()).all()

    can_create = current_user.has_perm("TRANSPORT_CREATE")
    can_edit = current_user.has_perm("TRANSPORT_UPDATE")
    can_delete = current_user.has_perm("TRANSPORT_DELETE")
    return render_template(
        "portal/transport/destinations_list.html",
        items=items,
        q=q,
        active=active,
        can_create=can_create,
        can_edit=can_edit,
        can_delete=can_delete,
    )


@portal_bp.route("/transport/destinations/new", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_CREATE")
def transport_destination_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        is_active = bool(request.form.get("is_active") == "1")
        notes = (request.form.get("notes") or "").strip() or None

        if not name:
            flash("اسم الوجهة مطلوب.", "danger")
            return redirect(url_for("portal.transport_destination_new"))

        row = TransportDestination(name=name, is_active=is_active, notes=notes, created_by_id=current_user.id)
        db.session.add(row)
        db.session.flush()
        _audit("TRANSPORT_DEST_CREATE", f"إضافة وجهة: {row.name}", target_type="TRANSPORT_DEST", target_id=row.id)
        db.session.commit()
        flash("تمت إضافة الوجهة.", "success")
        return redirect(url_for("portal.transport_destinations"))

    return render_template("portal/transport/destination_form.html", item=None)


@portal_bp.route("/transport/destinations/<int:dest_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_destination_edit(dest_id: int):
    row = TransportDestination.query.get_or_404(dest_id)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        is_active = bool(request.form.get("is_active") == "1")
        notes = (request.form.get("notes") or "").strip() or None

        if not name:
            flash("اسم الوجهة مطلوب.", "danger")
            return redirect(url_for("portal.transport_destination_edit", dest_id=dest_id))

        row.name = name
        row.is_active = is_active
        row.notes = notes

        _audit("TRANSPORT_DEST_UPDATE", f"تعديل وجهة: {row.name}", target_type="TRANSPORT_DEST", target_id=row.id)
        db.session.commit()
        flash("تم حفظ التعديل.", "success")
        return redirect(url_for("portal.transport_destinations"))

    return render_template("portal/transport/destination_form.html", item=row)


@portal_bp.route("/transport/destinations/<int:dest_id>/delete", methods=["POST"])
@login_required
@perm_required("TRANSPORT_DELETE")
def transport_destination_delete(dest_id: int):
    row = TransportDestination.query.get_or_404(dest_id)
    name = row.name
    try:
        db.session.delete(row)
        _audit("TRANSPORT_DEST_DELETE", f"حذف وجهة: {name}", target_type="TRANSPORT_DEST", target_id=dest_id)
        db.session.commit()
        flash("تم حذف الوجهة.", "success")
    except Exception:
        db.session.rollback()
        flash("لا يمكن حذف الوجهة لأنها مرتبطة برحلات.", "danger")
    return redirect(url_for("portal.transport_destinations"))


# -------------------------
# Maintenance
# -------------------------
@portal_bp.route("/transport/maintenance")
@login_required
@perm_required("TRANSPORT_READ")
def transport_maintenance_list():
    q = (request.args.get("q") or "").strip()
    vehicle_id = (request.args.get("vehicle_id") or "").strip()

    qry = TransportMaintenance.query
    if vehicle_id:
        try:
            vid = int(vehicle_id)
            qry = qry.filter(TransportMaintenance.vehicle_id == vid)
        except Exception:
            pass
    if q:
        like = f"%{q}%"
        qry = qry.join(TransportVehicle, TransportMaintenance.vehicle_id == TransportVehicle.id).filter(
            (TransportMaintenance.invoice_no.ilike(like))
            | (TransportMaintenance.notes.ilike(like))
            | (TransportVehicle.plate_no.ilike(like))
        )

    items = qry.order_by(TransportMaintenance.id.desc()).all()

    can_create = current_user.has_perm("TRANSPORT_CREATE")
    can_edit = current_user.has_perm("TRANSPORT_UPDATE")
    can_delete = current_user.has_perm("TRANSPORT_DELETE")

    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()
    return render_template(
        "portal/transport/maintenance_list.html",
        items=items,
        q=q,
        vehicle_id=vehicle_id,
        vehicles=vehicles,
        can_create=can_create,
        can_edit=can_edit,
        can_delete=can_delete,
    )


@portal_bp.route("/transport/maintenance/new", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_CREATE")
def transport_maintenance_new():
    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()
    garages = _lookup_items(TRANSPORT_LOOKUP_GARAGE)

    if request.method == "POST":
        vehicle_id = _to_int(request.form.get("vehicle_id") or "")
        invoice_no = (request.form.get("invoice_no") or "").strip() or None
        invoice_day = (request.form.get("invoice_day") or "").strip() or None
        garage_lookup_id = _to_int(request.form.get("garage_lookup_id") or "")
        notes = (request.form.get("notes") or "").strip() or None

        if not vehicle_id:
            flash("السيارة مطلوبة.", "danger")
            return redirect(url_for("portal.transport_maintenance_new"))

        row = TransportMaintenance(
            vehicle_id=vehicle_id,
            invoice_no=invoice_no,
            invoice_day=invoice_day,
            garage_lookup_id=garage_lookup_id,
            notes=notes,
            created_by_id=current_user.id,
        )
        db.session.add(row)
        db.session.flush()
        _audit("TRANSPORT_MAINT_CREATE", f"إنشاء صيانة: #{row.id}", target_type="TRANSPORT_MAINT", target_id=row.id)
        db.session.commit()
        flash("تم إنشاء سجل الصيانة.", "success")
        return redirect(url_for("portal.transport_maintenance_view", maint_id=row.id))

    return render_template("portal/transport/maintenance_form.html", item=None, vehicles=vehicles, garages=garages)


@portal_bp.route("/transport/maintenance/<int:maint_id>", methods=["GET"])
@login_required
@perm_required("TRANSPORT_READ")
def transport_maintenance_view(maint_id: int):
    row = TransportMaintenance.query.get_or_404(maint_id)
    items = TransportMaintenanceItem.query.filter(TransportMaintenanceItem.maintenance_id == maint_id).order_by(TransportMaintenanceItem.id.asc()).all()
    can_edit = current_user.has_perm("TRANSPORT_UPDATE")
    can_delete = current_user.has_perm("TRANSPORT_DELETE")
    maint_types = _lookup_items(TRANSPORT_LOOKUP_MAINT_TYPE)
    return render_template(
        "portal/transport/maintenance_view.html",
        item=row,
        items=items,
        can_edit=can_edit,
        can_delete=can_delete,
        maint_types=maint_types,
    )


@portal_bp.route("/transport/maintenance/<int:maint_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_maintenance_edit(maint_id: int):
    row = TransportMaintenance.query.get_or_404(maint_id)
    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()
    garages = _lookup_items(TRANSPORT_LOOKUP_GARAGE)

    if request.method == "POST":
        vehicle_id = _to_int(request.form.get("vehicle_id") or "")
        invoice_no = (request.form.get("invoice_no") or "").strip() or None
        invoice_day = (request.form.get("invoice_day") or "").strip() or None
        garage_lookup_id = _to_int(request.form.get("garage_lookup_id") or "")
        notes = (request.form.get("notes") or "").strip() or None

        if not vehicle_id:
            flash("السيارة مطلوبة.", "danger")
            return redirect(url_for("portal.transport_maintenance_edit", maint_id=maint_id))

        row.vehicle_id = vehicle_id
        row.invoice_no = invoice_no
        row.invoice_day = invoice_day
        row.garage_lookup_id = garage_lookup_id
        row.notes = notes

        _audit("TRANSPORT_MAINT_UPDATE", f"تعديل صيانة: #{row.id}", target_type="TRANSPORT_MAINT", target_id=row.id)
        db.session.commit()
        flash("تم حفظ التعديل.", "success")
        return redirect(url_for("portal.transport_maintenance_view", maint_id=row.id))

    return render_template("portal/transport/maintenance_form.html", item=row, vehicles=vehicles, garages=garages)


@portal_bp.route("/transport/maintenance/<int:maint_id>/delete", methods=["POST"])
@login_required
@perm_required("TRANSPORT_DELETE")
def transport_maintenance_delete(maint_id: int):
    row = TransportMaintenance.query.get_or_404(maint_id)
    try:
        db.session.delete(row)
        _audit("TRANSPORT_MAINT_DELETE", f"حذف صيانة: #{maint_id}", target_type="TRANSPORT_MAINT", target_id=maint_id)
        db.session.commit()
        flash("تم حذف سجل الصيانة.", "success")
    except Exception:
        db.session.rollback()
        flash("تعذر حذف سجل الصيانة (قد يكون مرتبطاً بعناصر).", "danger")
    return redirect(url_for("portal.transport_maintenance_list"))


@portal_bp.route("/transport/maintenance/<int:maint_id>/items/new", methods=["POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_maintenance_item_new(maint_id: int):
    TransportMaintenance.query.get_or_404(maint_id)

    maintenance_type_lookup_id = _to_int(request.form.get("maintenance_type_lookup_id") or "")
    quantity = _to_float(request.form.get("quantity") or "")
    unit_price = _to_float(request.form.get("unit_price") or "")
    total_price = _to_float(request.form.get("total_price") or "")
    note = (request.form.get("note") or "").strip() or None

    if total_price is None and quantity is not None and unit_price is not None:
        total_price = quantity * unit_price

    row = TransportMaintenanceItem(
        maintenance_id=maint_id,
        maintenance_type_lookup_id=maintenance_type_lookup_id,
        quantity=quantity,
        unit_price=unit_price,
        total_price=total_price,
        note=note,
    )
    db.session.add(row)
    db.session.flush()
    _audit("TRANSPORT_MAINT_ITEM_CREATE", f"إضافة بند صيانة: #{row.id}", target_type="TRANSPORT_MAINT_ITEM", target_id=row.id)
    db.session.commit()
    flash("تمت إضافة البند.", "success")
    return redirect(url_for("portal.transport_maintenance_view", maint_id=maint_id))


@portal_bp.route("/transport/maintenance/items/<int:item_id>/delete", methods=["POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_maintenance_item_delete(item_id: int):
    row = TransportMaintenanceItem.query.get_or_404(item_id)
    mid = row.maintenance_id
    db.session.delete(row)
    _audit("TRANSPORT_MAINT_ITEM_DELETE", f"حذف بند صيانة: #{item_id}", target_type="TRANSPORT_MAINT_ITEM", target_id=item_id)
    db.session.commit()
    flash("تم حذف البند.", "success")
    return redirect(url_for("portal.transport_maintenance_view", maint_id=mid))


# -------------------------
# Fuel Fill
# -------------------------
@portal_bp.route("/transport/fuel")
@login_required
@perm_required("TRANSPORT_READ")
def transport_fuel_list():
    q = (request.args.get("q") or "").strip()
    vehicle_id = (request.args.get("vehicle_id") or "").strip()

    qry = TransportFuelFill.query
    if vehicle_id:
        try:
            vid = int(vehicle_id)
            qry = qry.filter(TransportFuelFill.vehicle_id == vid)
        except Exception:
            pass
    if q:
        like = f"%{q}%"
        qry = qry.join(TransportVehicle, TransportFuelFill.vehicle_id == TransportVehicle.id).filter(
            (TransportFuelFill.invoice_no.ilike(like))
            | (TransportFuelFill.notes.ilike(like))
            | (TransportVehicle.plate_no.ilike(like))
        )

    items = qry.order_by(TransportFuelFill.id.desc()).all()

    can_create = current_user.has_perm("TRANSPORT_CREATE")
    can_edit = current_user.has_perm("TRANSPORT_UPDATE")
    can_delete = current_user.has_perm("TRANSPORT_DELETE")

    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()
    return render_template(
        "portal/transport/fuel_list.html",
        items=items,
        q=q,
        vehicle_id=vehicle_id,
        vehicles=vehicles,
        can_create=can_create,
        can_edit=can_edit,
        can_delete=can_delete,
    )


@portal_bp.route("/transport/fuel/new", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_CREATE")
def transport_fuel_new():
    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()
    payment_methods = _lookup_items(TRANSPORT_LOOKUP_PAYMENT_METHOD)
    stations = _lookup_items(TRANSPORT_LOOKUP_FUEL_STATION)

    if request.method == "POST":
        vehicle_id = _to_int(request.form.get("vehicle_id") or "")
        fill_day = (request.form.get("fill_day") or "").strip() or None
        invoice_no = (request.form.get("invoice_no") or "").strip() or None
        liters = _to_float(request.form.get("liters") or "")
        amount = _to_float(request.form.get("amount") or "")
        odometer_value = _to_float(request.form.get("odometer_value") or "")
        payment_method_lookup_id = _to_int(request.form.get("payment_method_lookup_id") or "")
        station_lookup_id = _to_int(request.form.get("station_lookup_id") or "")
        notes = (request.form.get("notes") or "").strip() or None

        if not vehicle_id:
            flash("السيارة مطلوبة.", "danger")
            return redirect(url_for("portal.transport_fuel_new"))

        row = TransportFuelFill(
            vehicle_id=vehicle_id,
            fill_day=fill_day,
            payment_method_lookup_id=payment_method_lookup_id,
            invoice_no=invoice_no,
            liters=liters,
            amount=amount,
            station_lookup_id=station_lookup_id,
            notes=notes,
            odometer_value=odometer_value,
            created_by_id=current_user.id,
        )
        db.session.add(row)
        db.session.flush()
        _audit("TRANSPORT_FUEL_CREATE", f"تعبئة وقود: #{row.id}", target_type="TRANSPORT_FUEL", target_id=row.id)

        # Update vehicle odometer if provided
        try:
            v = TransportVehicle.query.get(vehicle_id)
            if v and odometer_value is not None:
                v.current_odometer = float(odometer_value)
        except Exception:
            pass

        db.session.commit()
        flash("تم تسجيل التعبئة.", "success")
        return redirect(url_for("portal.transport_fuel_list"))

    return render_template(
        "portal/transport/fuel_form.html",
        item=None,
        vehicles=vehicles,
        payment_methods=payment_methods,
        stations=stations,
    )


@portal_bp.route("/transport/fuel/<int:fuel_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_UPDATE")
def transport_fuel_edit(fuel_id: int):
    row = TransportFuelFill.query.get_or_404(fuel_id)
    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()
    payment_methods = _lookup_items(TRANSPORT_LOOKUP_PAYMENT_METHOD)
    stations = _lookup_items(TRANSPORT_LOOKUP_FUEL_STATION)

    if request.method == "POST":
        vehicle_id = _to_int(request.form.get("vehicle_id") or "")
        fill_day = (request.form.get("fill_day") or "").strip() or None
        invoice_no = (request.form.get("invoice_no") or "").strip() or None
        liters = _to_float(request.form.get("liters") or "")
        amount = _to_float(request.form.get("amount") or "")
        odometer_value = _to_float(request.form.get("odometer_value") or "")
        payment_method_lookup_id = _to_int(request.form.get("payment_method_lookup_id") or "")
        station_lookup_id = _to_int(request.form.get("station_lookup_id") or "")
        notes = (request.form.get("notes") or "").strip() or None

        if not vehicle_id:
            flash("السيارة مطلوبة.", "danger")
            return redirect(url_for("portal.transport_fuel_edit", fuel_id=fuel_id))

        row.vehicle_id = vehicle_id
        row.fill_day = fill_day
        row.invoice_no = invoice_no
        row.liters = liters
        row.amount = amount
        row.odometer_value = odometer_value
        row.payment_method_lookup_id = payment_method_lookup_id
        row.station_lookup_id = station_lookup_id
        row.notes = notes

        _audit("TRANSPORT_FUEL_UPDATE", f"تعديل تعبئة وقود: #{row.id}", target_type="TRANSPORT_FUEL", target_id=row.id)

        # Update vehicle odometer if provided
        try:
            v = TransportVehicle.query.get(vehicle_id)
            if v and odometer_value is not None:
                v.current_odometer = float(odometer_value)
        except Exception:
            pass

        db.session.commit()
        flash("تم حفظ التعديل.", "success")
        return redirect(url_for("portal.transport_fuel_list"))

    return render_template(
        "portal/transport/fuel_form.html",
        item=row,
        vehicles=vehicles,
        payment_methods=payment_methods,
        stations=stations,
    )


@portal_bp.route("/transport/fuel/<int:fuel_id>/delete", methods=["POST"])
@login_required
@perm_required("TRANSPORT_DELETE")
def transport_fuel_delete(fuel_id: int):
    row = TransportFuelFill.query.get_or_404(fuel_id)
    db.session.delete(row)
    _audit("TRANSPORT_FUEL_DELETE", f"حذف تعبئة وقود: #{fuel_id}", target_type="TRANSPORT_FUEL", target_id=fuel_id)
    db.session.commit()
    flash("تم حذف التعبئة.", "success")
    return redirect(url_for("portal.transport_fuel_list"))



# -------------------------
# Tracking Settings (Option C placeholder UI)
# -------------------------
@portal_bp.route("/transport/tracking/settings", methods=["GET", "POST"])
@login_required
@perm_required("TRANSPORT_TRACKING_READ")
def transport_tracking_settings():
    # Allow managing only for users with MANAGE
    can_manage = current_user.has_perm("TRANSPORT_TRACKING_MANAGE")

    keys = {
        "enabled": "TRANSPORT_TRACKING_ENABLED",
        "provider": "TRANSPORT_TRACKING_PROVIDER",
        "base_url": "TRANSPORT_TRACKING_BASE_URL",
        "token": "TRANSPORT_TRACKING_TOKEN",
        "sync_sec": "TRANSPORT_TRACKING_SYNC_SECONDS",
    }

    if request.method == "POST":
        if not can_manage:
            flash("ليس لديك صلاحية تعديل إعدادات التتبع.", "danger")
            return redirect(url_for("portal.transport_tracking_settings"))

        enabled = "1" if request.form.get("enabled") == "1" else "0"
        provider = (request.form.get("provider") or "").strip() or ""
        base_url = (request.form.get("base_url") or "").strip() or ""
        token = (request.form.get("token") or "").strip() or ""
        sync_sec = (request.form.get("sync_sec") or "").strip() or "60"

        _set_setting(keys["enabled"], enabled)
        _set_setting(keys["provider"], provider)
        _set_setting(keys["base_url"], base_url)
        _set_setting(keys["token"], token[:255])  # SystemSetting.value is 255 by default
        _set_setting(keys["sync_sec"], sync_sec)

        # Per-vehicle tracking config
        try:
            for v in TransportVehicle.query.all():
                dev_key = f"veh_{v.id}_device"
                en_key = f"veh_{v.id}_enabled"
                v.tracking_device_uid = (request.form.get(dev_key) or "").strip() or None
                v.tracking_enabled = bool(request.form.get(en_key) == "1")
        except Exception:
            pass

        _audit("TRANSPORT_TRACKING_SETTINGS_UPDATE", "تحديث إعدادات تتبع المركبات (الخيار C)")
        db.session.commit()
        flash("تم حفظ الإعدادات.", "success")
        return redirect(url_for("portal.transport_tracking_settings"))

    settings = {
        "enabled": _get_setting(keys["enabled"], "0"),
        "provider": _get_setting(keys["provider"], ""),
        "base_url": _get_setting(keys["base_url"], ""),
        "token": _get_setting(keys["token"], ""),
        "sync_sec": _get_setting(keys["sync_sec"], "60"),
    }
    vehicles = TransportVehicle.query.order_by(TransportVehicle.plate_no.asc()).all()

    return render_template(
        "portal/transport/tracking_settings.html",
        settings=settings,
        vehicles=vehicles,
        can_manage=can_manage,
    )
