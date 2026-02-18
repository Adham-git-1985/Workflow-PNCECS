# -*- coding: utf-8 -*-
r"""
Import Employees from Excel into Workflow_PNCECS (Portal HR Employee File).

✅ Features
- Reads the provided Excel and imports/updates:
  - users
  - employee_file
  - employee_qualification (degree + specialization)
- Auto-creates missing masterdata:
  - Organization / Directorate / Unit / Department / Section / Division (as needed from "التسكين")
  - HR lookup items (EMP_STATUS, WORK_LOCATION, APPOINTMENT_TYPE, JOB_TITLE, ADMIN_TITLE, JOB_GRADE, JOB_CATEGORY, QUAL_DEGREE, QUAL_SPECIALIZATION)

⚠️ Safe by default
- Supports --dry-run (no DB writes)
- Generates a credentials CSV for newly created users (if passwords are random)

How to run (Windows / PowerShell):
  ./.venv/Scripts/python.exe tools/import_employees_excel.py --excel "C:/path/قائمة الموظفين معدلة.xlsx"

Notes:
- This script assumes you're running it inside the project root (same folder as app.py).
- DB is the same instance/workflow.db used by the Flask app.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

import pandas as pd
from werkzeug.security import generate_password_hash

# Import the Flask app + db + models from the project

# Make project root importable even when running this script from tools/
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(THIS_DIR, os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask

from extensions import db  # type: ignore

# Minimal Flask app for DB access (avoids importing full app.py and its extra deps)
INSTANCE_DIR = os.path.join(PROJECT_ROOT, 'instance')
os.makedirs(INSTANCE_DIR, exist_ok=True)
DB_PATH = os.path.join(INSTANCE_DIR, 'workflow.db')

app = Flask('import_tool', instance_path=INSTANCE_DIR)
app.config['SECRET_KEY'] = 'import-tool'
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{DB_PATH}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'connect_args': {
        'timeout': 30,
        'check_same_thread': False,
    },
}

db.init_app(app)


from models import (  # type: ignore
    User,
    EmployeeFile,
    EmployeeQualification,
    HRLookupItem,
    Organization,
    Directorate,
    Unit,
    Department,
    Section,
    Division,
)


# ----------------------------
# Helpers
# ----------------------------
def _s(v) -> str:
    return ("" if v is None else str(v)).strip()

def norm_ar(s: str) -> str:
    """Normalize Arabic-ish strings for matching (lightweight)."""
    s = _s(s)
    if not s:
        return ""
    # unify spaces
    s = re.sub(r"\s+", " ", s).strip()
    # remove invisible chars
    s = s.replace("\u200f", "").replace("\u200e", "")
    return s

def make_code(category: str, name_ar: str) -> str:
    """Stable code for HRLookupItem unique(category, code)."""
    raw = (category.upper().strip() + "|" + norm_ar(name_ar)).encode("utf-8", errors="ignore")
    h = hashlib.md5(raw).hexdigest()[:10].upper()
    return f"A_{h}"

def parse_date_to_ymd(v) -> Optional[str]:
    """Parse Excel date/string to YYYY-MM-DD. Accepts dd/mm/yyyy common in Arabic sheets."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (pd.Timestamp, )):
        d = v.to_pydatetime().date()
        return d.strftime("%Y-%m-%d")
    s = _s(v)
    if not s:
        return None
    # handle already YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    # Try pandas parser with dayfirst
    try:
        d = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if pd.isna(d):
            return None
        return d.date().strftime("%Y-%m-%d")
    except Exception:
        return None



# ----------------------------
# Email helpers (supports gmail.com, pncecs.plo.ps, etc.)
# ----------------------------
EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.I)

def clean_email(v) -> str:
    s = _s(v).strip()
    if not s:
        return ""
    s = s.replace("mailto:", "").strip()
    s = re.sub(r"\s+", "", s)
    return s.lower()

def is_valid_email(s: str) -> bool:
    s = clean_email(s)
    return bool(s and EMAIL_RE.match(s))

USERNAME_RE = re.compile(r"^[A-Z0-9._%+\-]+$", re.I)

def parse_email_cell(v) -> Tuple[str, str]:
    """Return (full_email, token) from a cell.
    - full_email: a valid email like user@gmail.com
    - token: username only like adham.pncecs (no domain)
    """
    s = _s(v).strip()
    if not s:
        return ("", "")
    s = s.replace("mailto:", "").strip()
    s = re.sub(r"\s+", "", s).strip()

    if "@" in s:
        e = s.lower()
        if is_valid_email(e):
            return (e, "")

    # Treat as username token (no domain)
    token = s.lower()
    token = re.sub(r"[^a-z0-9._%+\-]", "", token)
    token = token.strip(".-_")
    if token and USERNAME_RE.match(token):
        return ("", token)
    return ("", "")

def find_email_token_in_row(row) -> Tuple[str, str]:
    """Try to find either a full email or a username token in this row."""
    direct_names = [
        "البريد الإلكتروني", "البريد الالكتروني", "البريد", "الايميل", "الإيميل", "ايميل",
        "Email", "email", "E-mail", "e-mail", "E_mail", "E-Mail",
    ]
    for k in direct_names:
        try:
            v = row.get(k)
        except Exception:
            v = None
        full, token = parse_email_cell(v)
        if full or token:
            return (full, token)

    # fuzzy match any column containing hints
    try:
        keys = list(row.keys())
    except Exception:
        keys = []

    hints = ["email", "e-mail", "mail", "بريد", "ايميل", "إيميل"]
    for k in keys:
        nk = norm_ar(str(k)).lower()
        if any(h.lower() in nk for h in hints):
            try:
                v = row.get(k)
            except Exception:
                v = None
            full, token = parse_email_cell(v)
            if full or token:
                return (full, token)

    return ("", "")

def find_email_in_row(row) -> Optional[str]:
    """Backwards-compatible: return ONLY a full valid email if present."""
    full, _ = find_email_token_in_row(row)
    return full or None

def split_path(path: str) -> List[str]:
    """Split 'التسكين' path on backslash or > and clean segments."""
    p = norm_ar(path)
    if not p:
        return []
    parts = re.split(r"[\\>]+", p)
    parts = [norm_ar(x) for x in parts if norm_ar(x)]
    return parts

def is_role_segment(seg: str) -> bool:
    """Segments that are clearly job/role titles, not org units."""
    s = norm_ar(seg)
    if not s:
        return True
    role_keywords = [
        "الامين العام", "أمين عام", "مساعد", "نائب", "مستشار", "وكيل", "مدير مكتب", "سكرتير",
    ]
    return any(k in s for k in role_keywords)

def guess_node_type(seg: str) -> Optional[str]:
    """Heuristic mapping from Arabic labels to org unit types."""
    s = norm_ar(seg)
    if not s or is_role_segment(s):
        return None
    # Order matters: more specific first
    if s.startswith("شعبة"):
        return "DIVISION"
    if s.startswith("قسم"):
        return "SECTION"
    if s.startswith("دائرة"):
        return "DEPARTMENT"
    if s.startswith("وحدة") or s.startswith("مكتب"):
        return "UNIT"
    if ("مديرية" in s) or s.startswith("المديرية") or s.startswith("مديرية"):
        return "DIRECTORATE"
    if ("الادارة العامة" in s) or s.startswith("إدارة") or s.startswith("ادارة") or ("الإدارة" in s) or ("الادارة" in s):
        # In your DB structure, this maps best to Directorate
        return "DIRECTORATE"
    return None


@dataclass
class Placement:
    organization_id: Optional[int] = None
    directorate_id: Optional[int] = None
    unit_id: Optional[int] = None
    department_id: Optional[int] = None
    section_id: Optional[int] = None
    division_id: Optional[int] = None


class Cache:
    def __init__(self):
        self.org: Dict[str, Organization] = {}
        self.dir: Dict[Tuple[int, str], Directorate] = {}
        self.unit: Dict[Tuple[int, str], Unit] = {}
        self.dept_dir: Dict[Tuple[int, str], Department] = {}
        self.dept_unit: Dict[Tuple[int, str], Department] = {}
        self.sec_dept: Dict[Tuple[int, str], Section] = {}
        self.sec_dir: Dict[Tuple[int, str], Section] = {}
        self.sec_unit: Dict[Tuple[int, str], Section] = {}
        self.div_sec: Dict[Tuple[int, str], Division] = {}
        self.div_dept: Dict[Tuple[int, str], Division] = {}
        self.lookup: Dict[Tuple[str, str], HRLookupItem] = {}

CACHE = Cache()


def preload_cache():
    # Organizations
    for o in Organization.query.all():
        CACHE.org[norm_ar(o.name_ar)] = o
    # Directorates
    for d in Directorate.query.all():
        CACHE.dir[(int(d.organization_id), norm_ar(d.name_ar))] = d
    # Units
    for u in Unit.query.all():
        if u.organization_id:
            CACHE.unit[(int(u.organization_id), norm_ar(u.name_ar))] = u
    # Departments
    for dep in Department.query.all():
        if dep.directorate_id:
            CACHE.dept_dir[(int(dep.directorate_id), norm_ar(dep.name_ar))] = dep
        if dep.unit_id:
            CACHE.dept_unit[(int(dep.unit_id), norm_ar(dep.name_ar))] = dep
    # Sections
    for s in Section.query.all():
        key = norm_ar(s.name_ar)
        if s.department_id:
            CACHE.sec_dept[(int(s.department_id), key)] = s
        if s.directorate_id:
            CACHE.sec_dir[(int(s.directorate_id), key)] = s
        if s.unit_id:
            CACHE.sec_unit[(int(s.unit_id), key)] = s
    # Divisions
    for v in Division.query.all():
        key = norm_ar(v.name_ar)
        if v.section_id:
            CACHE.div_sec[(int(v.section_id), key)] = v
        if v.department_id:
            CACHE.div_dept[(int(v.department_id), key)] = v
    # Lookups
    for it in HRLookupItem.query.all():
        CACHE.lookup[(it.category.upper().strip(), norm_ar(it.name_ar))] = it


def get_or_create_lookup(category: str, name_ar: str, *, dry_run: bool) -> Optional[int]:
    category = (category or "").upper().strip()
    name = norm_ar(name_ar)
    if not category or not name:
        return None

    key = (category, name)
    it = CACHE.lookup.get(key)
    if it:
        return int(it.id)

    if dry_run:
        return None

    code = make_code(category, name)
    it = HRLookupItem(category=category, code=code, name_ar=name, name_en=None, sort_order=0, is_active=True)
    db.session.add(it)
    db.session.flush()
    CACHE.lookup[key] = it
    return int(it.id)


def get_or_create_org(name_ar: str, *, dry_run: bool) -> Optional[int]:
    name = norm_ar(name_ar)
    if not name:
        return None
    o = CACHE.org.get(name)
    if o:
        return int(o.id)
    if dry_run:
        return None
    o = Organization(name_ar=name, name_en=None, code=None, is_active=True)
    db.session.add(o)
    db.session.flush()
    CACHE.org[name] = o
    return int(o.id)


def get_or_create_directorate(org_id: int, name_ar: str, *, dry_run: bool) -> Optional[int]:
    name = norm_ar(name_ar)
    if not org_id or not name:
        return None
    key = (int(org_id), name)
    d = CACHE.dir.get(key)
    if d:
        return int(d.id)
    if dry_run:
        return None
    d = Directorate(organization_id=int(org_id), name_ar=name, name_en=None, code=None, is_active=True)
    db.session.add(d)
    db.session.flush()
    CACHE.dir[key] = d
    return int(d.id)


def get_or_create_unit(org_id: int, name_ar: str, *, dry_run: bool) -> Optional[int]:
    name = norm_ar(name_ar)
    if not org_id or not name:
        return None
    key = (int(org_id), name)
    u = CACHE.unit.get(key)
    if u:
        return int(u.id)
    if dry_run:
        return None
    u = Unit(organization_id=int(org_id), name_ar=name, name_en=None, code=None, is_active=True)
    db.session.add(u)
    db.session.flush()
    CACHE.unit[key] = u
    return int(u.id)


def get_or_create_department(*, directorate_id: Optional[int], unit_id: Optional[int], name_ar: str, dry_run: bool) -> Optional[int]:
    name = norm_ar(name_ar)
    if not name:
        return None
    if directorate_id:
        key = (int(directorate_id), name)
        dep = CACHE.dept_dir.get(key)
        if dep:
            return int(dep.id)
        if dry_run:
            return None
        dep = Department(directorate_id=int(directorate_id), unit_id=None, name_ar=name, name_en=None, code=None, is_active=True)
        db.session.add(dep)
        db.session.flush()
        CACHE.dept_dir[key] = dep
        return int(dep.id)
    if unit_id:
        key = (int(unit_id), name)
        dep = CACHE.dept_unit.get(key)
        if dep:
            return int(dep.id)
        if dry_run:
            return None
        dep = Department(directorate_id=None, unit_id=int(unit_id), name_ar=name, name_en=None, code=None, is_active=True)
        db.session.add(dep)
        db.session.flush()
        CACHE.dept_unit[key] = dep
        return int(dep.id)
    # no parent → cannot create department safely
    return None


def get_or_create_section(*, department_id: Optional[int], directorate_id: Optional[int], unit_id: Optional[int], name_ar: str, dry_run: bool) -> Optional[int]:
    name = norm_ar(name_ar)
    if not name:
        return None
    if department_id:
        key = (int(department_id), name)
        sec = CACHE.sec_dept.get(key)
        if sec:
            return int(sec.id)
        if dry_run:
            return None
        sec = Section(department_id=int(department_id), directorate_id=None, unit_id=None, name_ar=name, name_en=None, code=None, is_active=True)
        db.session.add(sec)
        db.session.flush()
        CACHE.sec_dept[key] = sec
        return int(sec.id)
    if directorate_id:
        key = (int(directorate_id), name)
        sec = CACHE.sec_dir.get(key)
        if sec:
            return int(sec.id)
        if dry_run:
            return None
        sec = Section(department_id=None, directorate_id=int(directorate_id), unit_id=None, name_ar=name, name_en=None, code=None, is_active=True)
        db.session.add(sec)
        db.session.flush()
        CACHE.sec_dir[key] = sec
        return int(sec.id)
    if unit_id:
        key = (int(unit_id), name)
        sec = CACHE.sec_unit.get(key)
        if sec:
            return int(sec.id)
        if dry_run:
            return None
        sec = Section(department_id=None, directorate_id=None, unit_id=int(unit_id), name_ar=name, name_en=None, code=None, is_active=True)
        db.session.add(sec)
        db.session.flush()
        CACHE.sec_unit[key] = sec
        return int(sec.id)
    return None


def get_or_create_division(*, section_id: Optional[int], department_id: Optional[int], name_ar: str, dry_run: bool) -> Optional[int]:
    name = norm_ar(name_ar)
    if not name:
        return None
    if section_id:
        key = (int(section_id), name)
        v = CACHE.div_sec.get(key)
        if v:
            return int(v.id)
        if dry_run:
            return None
        v = Division(section_id=int(section_id), department_id=None, name_ar=name, name_en=None, code=None, is_active=True)
        db.session.add(v)
        db.session.flush()
        CACHE.div_sec[key] = v
        return int(v.id)
    if department_id:
        key = (int(department_id), name)
        v = CACHE.div_dept.get(key)
        if v:
            return int(v.id)
        if dry_run:
            return None
        v = Division(section_id=None, department_id=int(department_id), name_ar=name, name_en=None, code=None, is_active=True)
        db.session.add(v)
        db.session.flush()
        CACHE.div_dept[key] = v
        return int(v.id)
    return None


def resolve_placement(path_value: str, *, dry_run: bool) -> Placement:
    parts = split_path(path_value)
    if not parts:
        return Placement()

    # first segment is the Organization name in your file
    org_name = parts[0]
    org_id = get_or_create_org(org_name, dry_run=dry_run)

    placement = Placement(organization_id=org_id)

    # walk other segments, skipping role segments
    for seg in parts[1:]:
        seg = norm_ar(seg)
        if not seg or is_role_segment(seg):
            continue

        typ = guess_node_type(seg)
        if typ is None:
            # unknown segments are ignored to avoid polluting masterdata
            continue

        if typ == "DIRECTORATE":
            if placement.organization_id:
                placement.directorate_id = get_or_create_directorate(int(placement.organization_id), seg, dry_run=dry_run)
            continue

        if typ == "UNIT":
            if placement.organization_id:
                placement.unit_id = get_or_create_unit(int(placement.organization_id), seg, dry_run=dry_run)
            continue

        if typ == "DEPARTMENT":
            placement.department_id = get_or_create_department(
                directorate_id=placement.directorate_id,
                unit_id=placement.unit_id,
                name_ar=seg,
                dry_run=dry_run,
            )
            continue

        if typ == "SECTION":
            placement.section_id = get_or_create_section(
                department_id=placement.department_id,
                directorate_id=placement.directorate_id,
                unit_id=placement.unit_id,
                name_ar=seg,
                dry_run=dry_run,
            )
            continue

        if typ == "DIVISION":
            placement.division_id = get_or_create_division(
                section_id=placement.section_id,
                department_id=placement.department_id,
                name_ar=seg,
                dry_run=dry_run,
            )
            continue

    return placement


def _set_user_password(user: User, plain: str) -> None:
    """Best-effort password setter (supports different User implementations)."""
    if hasattr(user, "set_password") and callable(getattr(user, "set_password")):
        user.set_password(plain)  # type: ignore
        return
    if hasattr(user, "password_hash"):
        setattr(user, "password_hash", generate_password_hash(plain))
        return
    if hasattr(user, "password"):
        setattr(user, "password", generate_password_hash(plain))
        return
    setattr(user, "password_hash", generate_password_hash(plain))

def _choose_domain_for_token(token: str, default_domain: str, internal_domain: str, internal_hints: str) -> str:
    token_l = (token or "").lower()
    hints = [h.strip().lower() for h in (internal_hints or "").split(",") if h.strip()]
    if internal_domain and hints:
        if any(h in token_l for h in hints):
            return internal_domain
    return default_domain

def ensure_employee_user(
    row,
    *,
    email_domain: str,
    internal_domain: str,
    internal_hints: str,
    role: str,
    dry_run: bool,
    password_mode: str,
    static_password: str,
    prefer_excel_email: bool = True,
    update_email: bool = False,
    reset_password: bool = False,
    creds_out: list = None,
) -> User:
    """Find existing user by employee_no/national_id, else create."""
    employee_no = norm_ar(row.get("الرقم الوظيفي"))
    national_id = norm_ar(row.get("رقم الهوية"))
    full_name = norm_ar(row.get("اسم الموظف"))

    full_email = ""
    token = ""
    if prefer_excel_email:
        full_email, token = find_email_token_in_row(row)

    desired_email = ""
    if full_email:
        desired_email = full_email
    elif token:
        dom = _choose_domain_for_token(token, email_domain, internal_domain, internal_hints)
        desired_email = f"{token}@{dom}".lower()

    q = (
        db.session.query(User)
        .join(EmployeeFile, EmployeeFile.user_id == User.id, isouter=True)
        .filter(
            (EmployeeFile.employee_no == employee_no) |
            (EmployeeFile.national_id == national_id)
        )
    )
    user = q.first()

    if not user and full_name:
        user = User.query.filter(User.name == full_name).first()

    if user:
        if not dry_run:
            if update_email and desired_email:
                conflict = User.query.filter(User.email == desired_email, User.id != user.id).first()
                if not conflict and user.email != desired_email:
                    user.email = desired_email

            jt = norm_ar(row.get("المسمى الوظيفي")) or None
            if hasattr(user, "job_title") and jt and (not getattr(user, "job_title", None)):
                setattr(user, "job_title", jt)

            if password_mode == "static" and reset_password:
                _set_user_password(user, static_password)

            db.session.flush()
        return user

    if dry_run:
        user = User(email="dryrun@example.local", name=full_name, job_title=None, password_hash="x", role=role)
        return user

    email = desired_email
    if email:
        if User.query.filter_by(email=email).first():
            email = ""

    if not email:
        base = f"emp{employee_no or national_id or hashlib.md5(full_name.encode('utf-8')).hexdigest()[:6]}"
        email = f"{base}@{email_domain}".lower()
        if User.query.filter_by(email=email).first():
            suffix = (national_id[-4:] if national_id else hashlib.md5(full_name.encode("utf-8")).hexdigest()[:4])
            email = f"{base}.{suffix}@{email_domain}".lower()

    if password_mode == "static":
        pwd = static_password
    else:
        raw = f"{employee_no}|{national_id}|{full_name}|{email}".encode("utf-8")
        pwd = hashlib.sha256(raw).hexdigest()[:12] + "!"

    user = User(
        email=email,
        name=full_name,
        job_title=norm_ar(row.get("المسمى الوظيفي")) or None,
        password_hash="x",
        role=role,
    )
    _set_user_password(user, pwd)

    db.session.add(user)
    db.session.flush()

    if creds_out is not None:
        creds_out.append({
            "user_id": int(user.id),
            "employee_no": employee_no,
            "national_id": national_id,
            "name": full_name,
            "email": email,
            "password": pwd,
        })

    return user


def upsert_employee_file(user: User, row, placement: Placement, *, dry_run: bool):
    if dry_run:
        return

    ef = EmployeeFile.query.filter_by(user_id=int(user.id)).first()
    if not ef:
        ef = EmployeeFile(user_id=int(user.id))
        db.session.add(ef)
        db.session.flush()

    # Lookups
    emp_status_id = get_or_create_lookup("EMP_STATUS", row.get("حالة الموظف"), dry_run=dry_run)
    work_loc_id = get_or_create_lookup("WORK_LOCATION", row.get("موقع العمل"), dry_run=dry_run)
    appt_type_id = get_or_create_lookup("APPOINTMENT_TYPE", row.get("نوع العقد"), dry_run=dry_run)

    job_title_id = get_or_create_lookup("JOB_TITLE", row.get("المسمى الوظيفي"), dry_run=dry_run)
    admin_title_id = get_or_create_lookup("ADMIN_TITLE", row.get("المسمى الاداري"), dry_run=dry_run)

    job_grade_id = get_or_create_lookup("JOB_GRADE", row.get("الدرجة"), dry_run=dry_run)
    job_cat_id = get_or_create_lookup("JOB_CATEGORY", row.get("الفئة"), dry_run=dry_run)

    ef.employee_no = norm_ar(row.get("الرقم الوظيفي")) or ef.employee_no
    ef.full_name_quad = norm_ar(row.get("اسم الموظف")) or ef.full_name_quad
    ef.national_id = norm_ar(row.get("رقم الهوية")) or ef.national_id

    ef.birth_date = parse_date_to_ymd(row.get("تاريخ الميلاد")) or ef.birth_date
    ef.hire_date = parse_date_to_ymd(row.get("تاريخ التعيين")) or ef.hire_date

    ef.employee_status_lookup_id = emp_status_id or ef.employee_status_lookup_id
    ef.work_location_lookup_id = work_loc_id or ef.work_location_lookup_id
    ef.appointment_type_lookup_id = appt_type_id or ef.appointment_type_lookup_id

    ef.job_title_lookup_id = job_title_id or ef.job_title_lookup_id
    ef.admin_title_lookup_id = admin_title_id or ef.admin_title_lookup_id

    ef.job_grade_lookup_id = job_grade_id or ef.job_grade_lookup_id
    ef.job_category_lookup_id = job_cat_id or ef.job_category_lookup_id

    # Placement
    ef.organization_id = placement.organization_id or ef.organization_id
    ef.directorate_id = placement.directorate_id or ef.directorate_id
    ef.department_id = placement.department_id or ef.department_id
    ef.division_id = placement.division_id or ef.division_id

    # Mirror placement into User (used by permissions / reports)
    try:
        user.directorate_id = placement.directorate_id or user.directorate_id
        user.unit_id = placement.unit_id or user.unit_id
        user.section_id = placement.section_id or user.section_id
        user.division_id = placement.division_id or user.division_id
        # legacy department_id
        user.department_id = placement.department_id or user.department_id
    except Exception:
        pass


def upsert_qualification(user: User, row, *, dry_run: bool):
    degree = norm_ar(row.get("المؤهل العلمي"))
    spec = norm_ar(row.get("التخصص"))
    if not degree and not spec:
        return
    if dry_run:
        return

    degree_id = get_or_create_lookup("QUAL_DEGREE", degree, dry_run=dry_run) if degree else None
    spec_id = get_or_create_lookup("QUAL_SPECIALIZATION", spec, dry_run=dry_run) if spec else None

    # if already exists same combo → skip
    q = EmployeeQualification.query.filter_by(user_id=int(user.id))
    if degree_id:
        q = q.filter(EmployeeQualification.degree_lookup_id == int(degree_id))
    if spec_id:
        q = q.filter(EmployeeQualification.specialization_lookup_id == int(spec_id))
    if q.first():
        return

    eq = EmployeeQualification(
        user_id=int(user.id),
        degree_lookup_id=int(degree_id) if degree_id else None,
        specialization_lookup_id=int(spec_id) if spec_id else None,
        grade_lookup_id=None,
        qualification_date=None,
        university_lookup_id=None,
        country_lookup_id=None,
        notes=None,
    )
    db.session.add(eq)


def main():
    parser = argparse.ArgumentParser(description="Import employees from Excel into Workflow_PNCECS")
    parser.add_argument("--excel", required=True, help="Path to Excel file")
    parser.add_argument("--db", default=None, help="Path to SQLite .db file OR full SQLAlchemy URI. Default: auto-detect instance/*.db (prefers instance/workflow.db).")
    parser.add_argument("--sheet", default=0, help="Sheet name or index (default: 0)")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    parser.add_argument("--email-domain", default="pncecs.plo.ps", help="Default domain used when building emails (token-only) or generating emails")
    parser.add_argument("--internal-domain", default="pncecs.plo.ps", help="Domain for internal emails when token contains internal hint(s)")
    parser.add_argument("--internal-hints", default="pncecs", help="Comma-separated hints; if token contains any => internal-domain (default: pncecs)")
    parser.add_argument("--update-email", action="store_true", help="Update existing users\' emails if an email/token is provided in Excel")
    parser.add_argument("--reset-password", action="store_true", help="Reset password for existing users (works with --password-mode static or --password)")
    parser.add_argument("--password", default=None, help="Shortcut for --password-mode static --static-password <value>")

    parser.add_argument("--no-prefer-excel-email", dest="prefer_excel_email", action="store_false", help="Do not use email from Excel even if present (force generated emails)")
    parser.set_defaults(prefer_excel_email=True)
    parser.add_argument("--role", default="user", help="Role for imported users (default: user)")
    parser.add_argument("--password-mode", choices=["random", "static"], default="random", help="random (default) or static")
    parser.add_argument("--static-password", default="ChangeMe123!", help="Used when --password-mode static")
    parser.add_argument("--out-dir", default="instance/import_reports", help="Output directory for reports")
    args = parser.parse_args()

    # If --password is provided, force static mode
    if args.password is not None:
        args.password_mode = "static"
        args.static_password = str(args.password)


    # --- DB selection / auto-detect ---
    def _to_db_uri(v: str) -> str:
        v = (v or "").strip().strip('"').strip("'")
        if not v:
            return ""
        # if looks like a full URI, keep it
        if "://" in v:
            return v
        # else treat as file path
        v_abs = os.path.abspath(v)
        return f"sqlite:///{v_abs}"

    db_uri = ""
    if args.db:
        # explicit
        if "://" not in args.db and not os.path.exists(args.db):
            raise SystemExit(f"DB file not found: {args.db}")
        db_uri = _to_db_uri(args.db)
    else:
        # auto: prefer instance/workflow.db if exists, else newest *.db in instance
        preferred = os.path.join(INSTANCE_DIR, "workflow.db")
        if os.path.exists(preferred):
            db_uri = _to_db_uri(preferred)
        else:
            db_files = []
            try:
                for fn in os.listdir(INSTANCE_DIR):
                    if fn.lower().endswith(".db"):
                        full = os.path.join(INSTANCE_DIR, fn)
                        db_files.append(full)
            except Exception:
                db_files = []
            if db_files:
                # pick newest by modified time
                db_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                db_uri = _to_db_uri(db_files[0])
            else:
                raise SystemExit(
                    "No SQLite database found under instance/. "
                    "Run the system once (or apply migrations) to create the DB, "
                    "or pass --db \"C:\\path\\to\\workflow.db\"."
                )

    # apply to app config BEFORE any DB usage
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri

    # Sanity check: ensure expected tables exist (helps avoid importing into a blank DB file)
    if db_uri.startswith("sqlite:///"):
        import sqlite3
        db_file = db_uri.replace("sqlite:///", "", 1)
        try:
            con = sqlite3.connect(db_file)
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cur.fetchall()}
            con.close()
            expected_any = {"users", "user", "employee_files", "employee_file"}
            if tables and expected_any.isdisjoint(tables):
                # DB has tables but not ours
                raise SystemExit(
                    f"DB loaded but doesn't look like Workflow_PNCECS schema. Tables found: {sorted(list(tables))[:10]}..."
                )
            if not tables:
                raise SystemExit(
                    "DB file exists but has no tables. Did you run migrations / init_db? "
                    "Run 'flask db upgrade' (or init_db.py) then retry."
                )
        except SystemExit:
            raise
        except Exception as e:
            # don't hard-fail; just warn
            print(f"[WARN] Could not validate DB schema: {e}")


    excel_path = args.excel
    if not os.path.exists(excel_path):
        print(f"Excel not found: {excel_path}")
        sys.exit(2)

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)

    with app.app_context():
        preload_cache()

        df = pd.read_excel(excel_path, sheet_name=args.sheet)
        # drop Unnamed columns
        df = df[[c for c in df.columns if not str(c).startswith("Unnamed")]].copy()

        required = ["اسم الموظف", "الرقم الوظيفي", "رقم الهوية"]
        for col in required:
            if col not in df.columns:
                print(f"Missing required column: {col}")
                print("Columns found:", list(df.columns))
                sys.exit(3)

        creds = []
        stats = {
            "rows_total": int(len(df)),
            "processed": 0,
            "created_users": 0,
            "updated_users": 0,
            "skipped_empty": 0,
            "errors": 0,
        }
        errors = []

        for idx, row in df.iterrows():
            try:
                name = norm_ar(row.get("اسم الموظف"))
                emp_no = norm_ar(row.get("الرقم الوظيفي"))
                nat_id = norm_ar(row.get("رقم الهوية"))
                if not (name and (emp_no or nat_id)):
                    stats["skipped_empty"] += 1
                    continue

                placement = resolve_placement(row.get("التسكين") or "", dry_run=args.dry_run)

                # user
                before = None
                # check if exists to count created/updated
                q = (
                    db.session.query(User)
                    .join(EmployeeFile, EmployeeFile.user_id == User.id, isouter=True)
                    .filter(
                        (EmployeeFile.employee_no == emp_no) |
                        (EmployeeFile.national_id == nat_id)
                    )
                )
                before = q.first()

                user = ensure_employee_user(
                    row,
                    email_domain=args.email_domain,
                    internal_domain=args.internal_domain,
                    internal_hints=args.internal_hints,
                    role=args.role,
                    dry_run=args.dry_run,
                    password_mode=args.password_mode,
                    static_password=args.static_password,
                    update_email=args.update_email,
                    reset_password=args.reset_password,
                    prefer_excel_email=args.prefer_excel_email,
                    creds_out=creds,
                )

                if before:
                    stats["updated_users"] += 1
                else:
                    stats["created_users"] += (0 if args.dry_run else 1)

                upsert_employee_file(user, row, placement, dry_run=args.dry_run)
                upsert_qualification(user, row, dry_run=args.dry_run)

                stats["processed"] += 1

                # commit in batches for safety
                if not args.dry_run and (stats["processed"] % 20 == 0):
                    db.session.commit()

            except Exception as e:
                stats["errors"] += 1
                errors.append({"row": int(idx) + 2, "error": str(e), "name": _s(row.get("اسم الموظف")), "emp_no": _s(row.get("الرقم الوظيفي"))})
                db.session.rollback()

        if not args.dry_run:
            db.session.commit()

        # Write reports
        summary_path = os.path.join(out_dir, "import_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            import json
            json.dump({"stats": stats, "errors": errors}, f, ensure_ascii=False, indent=2)

        if creds and not args.dry_run:
            creds_df = pd.DataFrame(creds)
            creds_path = os.path.join(out_dir, "new_users_credentials.csv")
            creds_df.to_csv(creds_path, index=False, encoding="utf-8-sig")

        print("Done.")
        print(stats)
        print(f"Summary: {summary_path}")
        if creds and not args.dry_run:
            print(f"New credentials CSV: {os.path.join(out_dir, 'new_users_credentials.csv')}")
        if errors:
            print(f"Errors: {len(errors)} (see summary json)")

if __name__ == "__main__":
    main()