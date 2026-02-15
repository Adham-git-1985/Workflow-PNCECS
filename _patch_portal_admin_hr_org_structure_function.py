from pathlib import Path
import re

path = Path('/mnt/data/work_units_v2/portal/routes.py')
text = path.read_text(encoding='utf-8')

start_pat = r"@portal_bp\.route\(\"/admin/hr/org-structure\", methods=\[\"GET\", \"POST\"\]\)"
end_pat = r"\ndef _import_org_structure_excel\(kind: str, file_storage\):"

start_m = re.search(start_pat, text)
end_m = re.search(end_pat, text)
if not start_m or not end_m:
    raise SystemExit('Could not locate portal_admin_hr_org_structure block')
start = start_m.start()
end = end_m.start()

replacement = r'''
@portal_bp.route("/admin/hr/org-structure", methods=["GET", "POST"])
@login_required
@_perm(PORTAL_ADMIN_PERMISSIONS_MANAGE)
def portal_admin_hr_org_structure():
    """Full org-structure CRUD + Excel import/export (Portal Admin).

    Tabs:
      - orgs: Organizations
      - dirs: Directorates (belongs to Organization)
      - units: Units (belongs to Directorate)
      - depts: Departments/Circles (belongs to either Directorate OR Unit - exactly one)
      - secs: Sections (belongs to either Department OR Directorate OR Unit - exactly one)
      - teams: Teams (belongs to Section)
    """

    tab = (request.args.get("tab") or "orgs").strip().lower()
    kind = (request.form.get("kind") or tab).strip().lower()
    op = (request.form.get("op") or "").strip().lower() if request.method == "POST" else ""

    def to_code(v):
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        # normalize floats like 1.0
        try:
            if s.endswith('.0') and s.replace('.', '', 1).isdigit():
                s = str(int(float(s)))
        except Exception:
            pass
        return s

    def to_int(v):
        try:
            if v is None or str(v).strip() == "":
                return None
            return int(str(v).strip())
        except Exception:
            return None

    # -------------------- Export (multi-sheet) --------------------
    if request.method == "GET" and request.args.get("export"):
        from openpyxl import Workbook

        wb = Workbook()
        wb.remove(wb.active)

        orgs = Organization.query.order_by(Organization.code.asc().nullslast(), Organization.name_ar.asc()).all()
        directorates = Directorate.query.order_by(Directorate.code.asc().nullslast(), Directorate.name_ar.asc()).all()
        units = Unit.query.order_by(Unit.code.asc().nullslast(), Unit.name_ar.asc()).all()
        departments = Department.query.order_by(Department.code.asc().nullslast(), Department.name_ar.asc()).all()
        sections = Section.query.order_by(Section.code.asc().nullslast(), Section.name_ar.asc()).all()
        teams = Team.query.order_by(Team.code.asc().nullslast(), Team.name_ar.asc()).all()

        def add_sheet(title, headers, rows):
            ws = wb.create_sheet(title=title)
            ws.append(headers)
            for r in rows:
                ws.append(r)

        add_sheet(
            "organizations",
            ["code", "name_ar", "name_en", "is_active"],
            [[o.code or "", o.name_ar or "", o.name_en or "", 1 if o.is_active else 0] for o in orgs],
        )

        add_sheet(
            "directorates",
            ["code", "organization_code", "name_ar", "name_en", "is_active"],
            [[d.code or "", (d.organization.code if d.organization else ""), d.name_ar or "", d.name_en or "", 1 if d.is_active else 0] for d in directorates],
        )

        add_sheet(
            "units",
            ["code", "directorate_code", "name_ar", "name_en", "is_active"],
            [[u.code or "", (u.directorate.code if u.directorate else ""), u.name_ar or "", u.name_en or "", 1 if u.is_active else 0] for u in units],
        )

        add_sheet(
            "departments",
            ["code", "directorate_code", "unit_code", "name_ar", "name_en", "is_active"],
            [[
                d.code or "",
                (d.directorate.code if d.directorate else ""),
                (d.unit.code if getattr(d, 'unit', None) else ""),
                d.name_ar or "", d.name_en or "", 1 if d.is_active else 0,
            ] for d in departments],
        )

        add_sheet(
            "sections",
            ["code", "department_code", "directorate_code", "unit_code", "name_ar", "name_en", "is_active"],
            [[
                s.code or "",
                (s.department.code if s.department else ""),
                (s.directorate.code if s.directorate else ""),
                (s.unit.code if getattr(s, 'unit', None) else ""),
                s.name_ar or "", s.name_en or "", 1 if s.is_active else 0,
            ] for s in sections],
        )

        add_sheet(
            "teams",
            ["code", "section_code", "name_ar", "name_en", "is_active"],
            [[t.code or "", (t.section.code if t.section else ""), t.name_ar or "", t.name_en or "", 1 if t.is_active else 0] for t in teams],
        )

        from io import BytesIO
        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)
        return send_file(bio, as_attachment=True, download_name="org_structure.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # -------------------- POST actions --------------------
    if request.method == "POST":
        if kind not in ("orgs", "dirs", "units", "depts", "secs", "teams"):
            flash("تبويب غير معروف.", "warning")
            return redirect(url_for("portal.portal_admin_hr_org_structure", tab=tab))

        if op == "import":
            file = request.files.get("file")
            if not file:
                flash("اختر ملف Excel للاستيراد.", "warning")
                return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))
            ok, msg = _import_org_structure_excel(kind, file)
            flash(msg, "success" if ok else "danger")
            return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))

        # Resolve model
        Model = {"orgs": Organization, "dirs": Directorate, "units": Unit, "depts": Department, "secs": Section, "teams": Team}.get(kind)

        if op == "delete":
            rid = to_int(request.form.get("id"))
            if not rid:
                flash("معرف غير صالح.", "warning")
                return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))
            row = Model.query.get(rid)
            if not row:
                flash("غير موجود.", "warning")
                return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))

            # guards
            if kind == "orgs" and Directorate.query.filter_by(organization_id=row.id).first():
                flash("لا يمكن حذف منظمة مرتبطة بإدارات.", "warning")
                return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))
            if kind == "dirs" and (Unit.query.filter_by(directorate_id=row.id).first() or Department.query.filter_by(directorate_id=row.id).first() or Section.query.filter_by(directorate_id=row.id).first()):
                flash("لا يمكن حذف إدارة مرتبطة بوحدات/دوائر/أقسام.", "warning")
                return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))
            if kind == "units" and (Department.query.filter_by(unit_id=row.id).first() or Section.query.filter_by(unit_id=row.id).first()):
                flash("لا يمكن حذف وحدة مرتبطة بدوائر/أقسام.", "warning")
                return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))
            if kind == "depts" and Section.query.filter_by(department_id=row.id).first():
                flash("لا يمكن حذف دائرة مرتبطة بأقسام.", "warning")
                return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))
            if kind == "secs" and Team.query.filter_by(section_id=row.id).first():
                flash("لا يمكن حذف قسم مرتبط بفرق.", "warning")
                return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))

            try:
                db.session.delete(row)
                db.session.commit()
                flash("تم الحذف.", "success")
            except Exception:
                db.session.rollback()
                flash("تعذر الحذف (قد يكون مرتبطاً ببيانات أخرى).", "danger")
            return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))

        if op == "save":
            rid = to_int(request.form.get("id"))
            row = Model.query.get(rid) if rid else Model()

            # Parent handling
            if kind == "dirs":
                pid = to_int(request.form.get("parent_id"))
                row.organization_id = pid
            elif kind == "units":
                pid = to_int(request.form.get("parent_id"))
                row.directorate_id = pid
            elif kind == "teams":
                pid = to_int(request.form.get("parent_id"))
                row.section_id = pid
            elif kind == "depts":
                ptype = (request.form.get("parent_type") or "directorate").strip().lower()
                pid_dir = to_int(request.form.get("parent_id_dir"))
                pid_unit = to_int(request.form.get("parent_id_unit"))
                if ptype == "unit":
                    row.unit_id = pid_unit
                    row.directorate_id = None
                else:
                    row.directorate_id = pid_dir
                    row.unit_id = None
            elif kind == "secs":
                ptype = (request.form.get("parent_type") or "department").strip().lower()
                pid_dept = to_int(request.form.get("parent_id_dept"))
                pid_dir = to_int(request.form.get("parent_id_dir"))
                pid_unit = to_int(request.form.get("parent_id_unit"))
                row.department_id = None
                row.directorate_id = None
                row.unit_id = None
                if ptype == "directorate":
                    row.directorate_id = pid_dir
                elif ptype == "unit":
                    row.unit_id = pid_unit
                else:
                    row.department_id = pid_dept

            # Common fields
            row.code = to_code(request.form.get("code"))
            row.name_ar = (request.form.get("name_ar") or "").strip() or None
            row.name_en = (request.form.get("name_en") or "").strip() or None
            row.is_active = bool(request.form.get("is_active"))

            # validations (best-effort)
            if kind in ("dirs", "units", "teams"):
                if not getattr(row, {"dirs": "organization_id", "units": "directorate_id", "teams": "section_id"}[kind]):
                    flash("اختر التبعية (Parent).", "warning")
                    return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))
            if kind == "depts":
                if not (row.directorate_id or row.unit_id) or (row.directorate_id and row.unit_id):
                    flash("يجب اختيار تبعية واحدة فقط: إدارة أو وحدة.", "warning")
                    return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))
            if kind == "secs":
                parents = [row.department_id, row.directorate_id, row.unit_id]
                if sum(1 for p in parents if p) != 1:
                    flash("يجب اختيار تبعية واحدة فقط: دائرة أو إدارة أو وحدة.", "warning")
                    return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))

            try:
                db.session.add(row)
                db.session.commit()
                flash("تم الحفظ.", "success")
            except Exception:
                db.session.rollback()
                flash("تعذر الحفظ (تحقق من القيم/التكرار).", "danger")

            return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))

        flash("عملية غير معروفة.", "warning")
        return redirect(url_for("portal.portal_admin_hr_org_structure", tab=kind))

    # -------------------- GET --------------------
    orgs = Organization.query.order_by(Organization.code.asc().nullslast(), Organization.name_ar.asc()).all()
    directorates = Directorate.query.order_by(Directorate.code.asc().nullslast(), Directorate.name_ar.asc()).all()
    units = Unit.query.order_by(Unit.code.asc().nullslast(), Unit.name_ar.asc()).all()
    departments = Department.query.order_by(Department.code.asc().nullslast(), Department.name_ar.asc()).all()
    sections = Section.query.order_by(Section.code.asc().nullslast(), Section.name_ar.asc()).all()
    teams = Team.query.order_by(Team.code.asc().nullslast(), Team.name_ar.asc()).all()

    return render_template(
        "portal/admin/hr_org_structure.html",
        tab=tab,
        orgs=orgs,
        directorates=directorates,
        units=units,
        departments=departments,
        sections=sections,
        teams=teams,
    )
'''

new_text = text[:start] + replacement + text[end:]
path.write_text(new_text, encoding='utf-8')
print('portal_admin_hr_org_structure block replaced')
