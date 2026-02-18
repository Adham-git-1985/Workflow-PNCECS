"""System-wide feature search (مسار + البوابة الإدارية).

This is a lightweight "command palette" style search:
- Searches across key features/screens + help guides.
- Results are permission-aware (items are hidden if user can't access them).

We intentionally keep the registry static and small to avoid heavy DB queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from pathlib import Path

import re
import unicodedata

from flask import url_for


_AR_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_PUNCT = re.compile(r"[^0-9A-Za-z\u0600-\u06FF\s]")


def _norm(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    # normalize unicode
    s = unicodedata.normalize("NFKC", s)
    # remove Arabic diacritics and tatweel
    s = _AR_DIACRITICS.sub("", s).replace("ـ", "")
    # normalize Arabic common forms
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    # punctuation → space
    s = _PUNCT.sub(" ", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s.casefold().strip()


def _tokens(q: str) -> List[str]:
    qn = _norm(q)
    if not qn:
        return []
    return [t for t in qn.split(" ") if t]


@dataclass(frozen=True)
class SearchItem:
    id: str
    title: str
    desc: str
    category: str  # e.g., "مسار", "البوابة الإدارية", "الأدلة"
    endpoint: Optional[str] = None
    endpoint_kwargs: Optional[Dict[str, Any]] = None
    url: Optional[str] = None
    keywords: Tuple[str, ...] = ()
    perms_any: Tuple[str, ...] = ()
    roles_any: Tuple[str, ...] = ()

    def href(self) -> str:
        if self.url:
            return self.url
        if self.endpoint:
            try:
                return url_for(self.endpoint, **(self.endpoint_kwargs or {}))
            except Exception:
                return self.url or "#"
        return "#"


def _has_any_perm(user, perms: Tuple[str, ...]) -> bool:
    if not perms:
        return True
    for p in perms:
        try:
            if user.has_perm(p):
                return True
        except Exception:
            continue
    return False


def _has_any_role(user, roles: Tuple[str, ...]) -> bool:
    if not roles:
        return True
    for r in roles:
        try:
            if user.has_role(r):
                return True
        except Exception:
            continue
    return False


def visible_items_for_user(user) -> List[SearchItem]:
    """Registry of key features. Keep it small & high-signal."""

    items: List[SearchItem] = [
        # Guides
        SearchItem(
            id="help_center",
            title="مركز الأدلة",
            desc="جميع أدلة النظام حسب صلاحياتك.",
            category="الأدلة",
            endpoint="users.help_index",
            keywords=("مساعدة", "ادلة", "دليل", "help", "guide"),
        ),
        SearchItem(
            id="help_master",
            title="الدليل الشامل للنظام (ابدأ هنا)",
            desc="مسار + البوابة الإدارية: تعريف بالنظام وروابط الأدلة.",
            category="الأدلة",
            endpoint="users.help_system_master_guide",
            keywords=("ابدأ", "دليل شامل", "system", "master"),
        ),
        SearchItem(
            id="help_employee",
            title="دليل الموظف",
            desc="استخدام مسار: الطلبات والمتابعة والإشعارات.",
            category="الأدلة",
            endpoint="users.help_employee_guide",
            keywords=("موظف", "شرح", "دليل", "employee"),
        ),
        SearchItem(
            id="help_workflow_user",
            title="دليل المستخدم: الطلبات والمسارات",
            desc="My Requests / Inbox / البحث / الطباعة.",
            category="الأدلة",
            endpoint="users.help_workflow_user_guide",
            keywords=("مسارات", "طلبات", "workflow"),
        ),
        SearchItem(
            id="help_org_dynamic",
            title="دليل الهيكلية الموحدة (Dynamic)",
            desc="تعريف المستويات ديناميكيًا + الشجرة + الربط بالمسارات والتوجيه.",
            category="الأدلة",
            endpoint="users.help_org_structure_dynamic_guide",
            keywords=("هيكلية", "مخطط", "شجرة", "org", "dynamic"),
            perms_any=(
                "HR_ORG_DYNAMIC_GUIDE_VIEW",
                "HR_ORGSTRUCTURE_MANAGE",
                "HR_MASTERDATA_MANAGE",
                "PORTAL_ADMIN_PERMISSIONS_MANAGE",
            ),
            roles_any=("HR_ADMIN", "ADMIN", "SUPER_ADMIN", "SUPERADMIN"),
        ),

        # Workflow (Masar)
        SearchItem(
            id="wf_inbox",
            title="صندوق الوارد (مسار)",
            desc="المعاملات الواردة للموافقة/الإجراء.",
            category="مسار",
            endpoint="workflow.inbox",
            keywords=("inbox", "صندوق", "وارد", "مهام"),
        ),
        SearchItem(
            id="wf_my_requests",
            title="طلباتي (My Requests)",
            desc="سجل الطلبات التي أنشأتها وحالتها.",
            category="مسار",
            endpoint="my_requests",
            keywords=("طلباتي", "requests", "my"),
        ),
        SearchItem(
            id="wf_notifications",
            title="الإشعارات (مسار)",
            desc="إشعارات النظام والتنبيهات.",
            category="مسار",
            endpoint="workflow.notifications",
            keywords=("اشعار", "تنبيه", "notifications"),
        ),
        SearchItem(
            id="wf_circulars",
            title="التعميمات (مسار)",
            desc="عرض جميع التعميمات الصادرة.",
            category="مسار",
            endpoint="workflow.circulars_list",
            keywords=("تعميم", "تعميمات", "circular", "اعلان"),
        ),

        # Portal
        SearchItem(
            id="portal_home",
            title="الرئيسية (البوابة الإدارية)",
            desc="لوحة البوابة: الأنظمة والاختصارات.",
            category="البوابة الإدارية",
            endpoint="portal.index",
            perms_any=("PORTAL_READ", "PORTAL_VIEW"),
            keywords=("portal", "بوابة", "الرئيسية"),
        ),
        SearchItem(
            id="portal_circulars",
            title="التعميمات (البوابة الإدارية)",
            desc="عرض التعميمات داخل البوابة.",
            category="البوابة الإدارية",
            endpoint="portal.circulars_list",
            perms_any=("PORTAL_READ", "PORTAL_VIEW"),
            keywords=("تعميم", "تعميمات", "circular", "اعلان", "بوابة"),
        ),
        SearchItem(
            id="portal_circular_new",
            title="إصدار تعميم (إنشاء تعميم)",
            desc="إنشاء تعميم جديد وإرسال إشعار للجميع.",
            category="البوابة الإدارية",
            endpoint="portal.circular_new",
            perms_any=("PORTAL_CIRCULARS_MANAGE",),
            keywords=("انشاء", "إصدار", "تعميم", "new circular", "create"),
        ),

        # Dynamic org masterdata (Portal masterdata area)
        SearchItem(
            id="org_types_dynamic",
            title="أنواع الهيكلية (Dynamic)",
            desc="تعريف مستويات الهيكلية ديناميكيًا + التبعية.",
            category="البوابة الإدارية",
            url="/admin/masterdata/org-node-types",
            perms_any=("MASTERDATA_READ", "MASTERDATA_MANAGE", "HR_MASTERDATA_MANAGE"),
            keywords=("هيكلية", "مستويات", "انواع", "dynamic", "org type"),
        ),
        SearchItem(
            id="org_nodes_dynamic",
            title="عناصر الهيكلية (Dynamic)",
            desc="إضافة عناصر الهيكلية واختيار الأب بالشجرة.",
            category="البوابة الإدارية",
            url="/admin/masterdata/org-nodes",
            perms_any=("MASTERDATA_READ", "MASTERDATA_MANAGE", "HR_MASTERDATA_MANAGE"),
            keywords=("هيكلية", "عناصر", "شجرة", "dynamic", "org node"),
        ),
        SearchItem(
            id="org_assignments",
            title="تعيين الموظفين على الهيكلية الموحدة",
            desc="تحديد تبعية الموظف لعقدة OrgNode (أساسي/ثانوي).",
            category="البوابة الإدارية",
            url="/portal/hr/org-nodes/assignments",
            perms_any=("HR_MASTERDATA_MANAGE", "HR_EMPLOYEE_MANAGE", "HR_ORGSTRUCTURE_MANAGE"),
            keywords=("تعيين", "موظف", "تبعية", "هيكلية", "org"),
        ),

        # Workflow admin
        SearchItem(
            id="wf_templates",
            title="قوالب المسارات (Workflow Templates)",
            desc="إدارة القوالب وخطوات الموافقة.",
            category="مسار",
            endpoint="workflow.templates_list",
            perms_any=("WORKFLOW_TEMPLATES_READ", "WORKFLOW_TEMPLATES_MANAGE"),
            keywords=("templates", "قوالب", "مسارات", "workflow"),
        ),
        SearchItem(
            id="wf_routing",
            title="قواعد التوجيه (Routing Rules)",
            desc="ربط الطلبات بالمسارات حسب الشروط والهيكلية.",
            category="مسار",
            endpoint="admin.workflow_routing_list",
            perms_any=("WORKFLOW_ROUTING_READ", "WORKFLOW_ROUTING_MANAGE"),
            keywords=("توجيه", "routing", "rules", "مسارات", "قواعد"),
        ),
    ]

    # Filter by visibility
    out: List[SearchItem] = []
    for it in items:
        if not _has_any_role(user, it.roles_any):
            continue
        if not _has_any_perm(user, it.perms_any):
            continue
        out.append(it)

    # ------------------------------
    # Portal feature search (dynamic):
    # - Index Portal permission labels (e.g., "إدارة وثائق HR")
    # - Index key HR hub tiles (titles/desc) so users can search by what they see on the UI.
    #
    # IMPORTANT: keep it lightweight (no DB queries).
    # ------------------------------
    try:
        out.extend(_portal_perm_items(user))
    except Exception:
        pass
    try:
        out.extend(_portal_hr_tiles_items(user))
    except Exception:
        pass

    return out


# ------------------------------
# Portal search sources
# ------------------------------


_PORTAL_PERM_ENDPOINTS: Dict[str, str] = {
    # HR docs
    "HR_DOCS_READ": "portal.hr_docs_home",
    "HR_DOCS_MANAGE": "portal.hr_docs_admin",
    # HR employees
    "HR_EMPLOYEE_READ": "portal.hr_employees",
    "HR_EMPLOYEE_MANAGE": "portal.hr_employees",
    "HR_EMPLOYEE_ATTACHMENTS_MANAGE": "portal.hr_employees",
    "HR_EMPLOYEE_FILES_MANAGE": "portal.hr_employees",
    # HR masterdata / org
    "HR_MASTERDATA_MANAGE": "portal.hr_masterdata_index",
    "HR_ORGSTRUCTURE_READ": "portal.hr_org_structure",
    "HR_ORGSTRUCTURE_MANAGE": "portal.hr_org_structure",
    # HR reports
    "HR_REPORTS_VIEW": "portal.hr_reports_home",
    "HR_REPORTS_EXPORT": "portal.hr_reports_home",
    # HR attendance
    "HR_ATTENDANCE_READ": "portal.hr_my_attendance",
    "HR_ATTENDANCE_CREATE": "portal.hr_attendance_import",
    "HR_ATTENDANCE_EXPORT": "portal.hr_my_attendance",
    # HR performance
    "HR_PERFORMANCE_READ": "portal.hr_perf_home",
    "HR_PERFORMANCE_SUBMIT": "portal.hr_perf_home",
    "HR_PERFORMANCE_MANAGE": "portal.portal_admin_hr_perf_dashboard",
    "HR_PERFORMANCE_EXPORT": "portal.portal_admin_hr_perf_dashboard",
    "HR_SYSTEM_EVALUATION_VIEW": "portal.hr_system_eval",
    # HR self service
    "HR_SS_READ": "portal.hr_ss_home",
    "HR_SS_CREATE": "portal.hr_ss_home",
    "HR_SS_APPROVE": "portal.hr_ss_approvals",
    "HR_SS_WORKFLOWS_MANAGE": "portal.portal_admin_hr_ss_workflows",
    # Discipline
    "HR_DISCIPLINE_READ": "portal.hr_discipline_home",
    "HR_DISCIPLINE_MANAGE": "portal.hr_discipline_home",
    # Payslip
    "HR_PAYSLIP_VIEW": "portal.hr_my_payslips",
    # Portal admin
    "PORTAL_ADMIN_PERMISSIONS_MANAGE": "portal.portal_admin_permissions",
    # Circulars
    "PORTAL_CIRCULARS_MANAGE": "portal.circular_new",

    # Corr
    "CORR_READ": "portal.corr_index",
    "CORR_CREATE": "portal.corr_index",
    "CORR_UPDATE": "portal.corr_index",
    "CORR_DELETE": "portal.corr_index",
    "CORR_EXPORT": "portal.corr_index",
    "CORR_LOOKUPS_MANAGE": "portal.corr_index",

    # Store
    "STORE_READ": "portal.store_home",
    "STORE_MANAGE": "portal.store_home",
    "STORE_EXPORT": "portal.store_home",

    # Transport
    "TRANSPORT_READ": "portal.transport_home",
    "TRANSPORT_CREATE": "portal.transport_home",
    "TRANSPORT_UPDATE": "portal.transport_home",
    "TRANSPORT_DELETE": "portal.transport_home",
    "TRANSPORT_APPROVE": "portal.transport_home",
    "TRANSPORT_TRACKING_READ": "portal.transport_home",
    "TRANSPORT_TRACKING_MANAGE": "portal.transport_home",
}


def _portal_perm_items(user) -> List[SearchItem]:
    """Index Portal permission labels as searchable features.

    This solves cases like: searching for "إدارة وثائق HR" should yield the actual screen.
    """
    try:
        from portal.perm_defs import PERMS as _PORTAL_PERMS
    except Exception:
        return []

    items: List[SearchItem] = []
    for grp_name, defs in (_PORTAL_PERMS or {}).items():
        for p in (defs or []):
            key = (getattr(p, "key", "") or "").strip().upper()
            label = (getattr(p, "label", "") or "").strip()
            desc = (getattr(p, "desc", "") or "").strip()
            module = (getattr(p, "module", "") or "").strip()
            if not key or not label:
                continue

            # Only show what the user can access (permission-aware search)
            try:
                if not user.has_perm(key):
                    continue
            except Exception:
                continue

            endpoint = _PORTAL_PERM_ENDPOINTS.get(key)

            # Fallbacks so results always navigate somewhere useful
            if not endpoint:
                try:
                    if key.startswith("HR_"):
                        # Admin HR hub vs employee HR self-service
                        manage_like = any(
                            user.has_perm(k)
                            for k in (
                                "HR_MASTERDATA_MANAGE",
                                "HR_EMPLOYEE_MANAGE",
                                "HR_EMPLOYEE_ATTACHMENTS_MANAGE",
                                "HR_DOCS_MANAGE",
                                "HR_REQUESTS_VIEW_ALL",
                                "HR_PERFORMANCE_MANAGE",
                            )
                        )
                        endpoint = "portal.hr_home" if manage_like else "portal.hr_me_home"
                    elif key.startswith("CORR_"):
                        endpoint = "portal.corr_index"
                    elif key.startswith("STORE_"):
                        endpoint = "portal.store_home"
                    elif key.startswith("TRANSPORT_"):
                        endpoint = "portal.transport_home"
                    else:
                        endpoint = "portal.index"
                except Exception:
                    endpoint = "portal.index"
            cat = f"البوابة الإدارية / {grp_name}" if grp_name else "البوابة الإدارية"
            kw = (
                key,
                module,
                grp_name,
                "portal",
                "بوابة",
                "صلاحيات",
            )
            items.append(
                SearchItem(
                    id=f"portal_perm_{key}",
                    title=label,
                    desc=desc,
                    category=cat,
                    endpoint=endpoint,
                    keywords=tuple([k for k in kw if k]),
                    perms_any=(key,),
                )
            )

    return items


_CACHED_PORTAL_HR_TILES: Optional[List[Dict[str, str]]] = None


def _load_portal_hr_tiles() -> List[Dict[str, str]]:
    """Parse portal/routes.py to extract HR hub tiles.

    We do this once per process (cached) to keep global search comprehensive
    without manual maintenance.
    """
    global _CACHED_PORTAL_HR_TILES
    if _CACHED_PORTAL_HR_TILES is not None:
        return _CACHED_PORTAL_HR_TILES

    out: List[Dict[str, str]] = []
    try:
        base = Path(__file__).resolve().parents[1]
        routes_path = base / "portal" / "routes.py"
        txt = routes_path.read_text(encoding="utf-8", errors="ignore")

        # Pattern 1: add_item(PERM, "Title", "Desc", "icon", "endpoint", "Section")
        re_add = re.compile(
            r"add_item\(\s*([A-Z0-9_]+)\s*,\s*\"([^\"]+)\"\s*,\s*\"([^\"]*)\"\s*,\s*\"[^\"]*\"\s*,\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"",
            re.MULTILINE,
        )
        for m in re_add.finditer(txt):
            perm_key, title, desc, endpoint, section = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            out.append({
                "perm": (perm_key or "").strip(),
                "title": (title or "").strip(),
                "desc": (desc or "").strip(),
                "endpoint": (endpoint or "").strip(),
                "section": (section or "").strip(),
            })

        # Pattern 2: _sec_map["X"].append({ "title": "...", "desc": "...", ... "url": url_for("endpoint") })
        re_sec = re.compile(
            r"_sec_map\[\"([^\"]+)\"\]\.append\(\{\s*\n\s*\"title\"\s*:\s*\"([^\"]+)\"\s*,\s*\n\s*\"desc\"\s*:\s*\"([^\"]*)\"\s*,[\s\S]*?url_for\(\"([^\"]+)\"\)",
            re.MULTILINE,
        )
        for m in re_sec.finditer(txt):
            section, title, desc, endpoint = m.group(1), m.group(2), m.group(3), m.group(4)
            out.append({
                "perm": "",
                "title": (title or "").strip(),
                "desc": (desc or "").strip(),
                "endpoint": (endpoint or "").strip(),
                "section": (section or "").strip(),
            })

    except Exception:
        out = []

    # Deduplicate by (endpoint,title)
    seen = set()
    dedup: List[Dict[str, str]] = []
    for r in out:
        k = (r.get("endpoint", ""), r.get("title", ""))
        if k in seen:
            continue
        seen.add(k)
        dedup.append(r)

    _CACHED_PORTAL_HR_TILES = dedup
    return dedup


def _portal_hr_tiles_items(user) -> List[SearchItem]:
    """Search items based on the HR hub tiles (what users actually see)."""
    tiles = _load_portal_hr_tiles()
    items: List[SearchItem] = []
    for t in (tiles or []):
        title = (t.get("title") or "").strip()
        if not title:
            continue
        perm = (t.get("perm") or "").strip().upper()
        endpoint = (t.get("endpoint") or "").strip()
        desc = (t.get("desc") or "").strip()
        section = (t.get("section") or "").strip()

        if perm:
            try:
                if not user.has_perm(perm):
                    continue
            except Exception:
                continue

        cat = "البوابة الإدارية / الموارد البشرية"
        if section:
            cat = f"{cat} / {section}"

        kw = (
            perm,
            section,
            "hr",
            "human resources",
            "portal",
            "بوابة",
            "موارد بشرية",
        )
        items.append(
            SearchItem(
                id=f"portal_hr_tile_{_norm(title)}_{endpoint}",
                title=title,
                desc=desc,
                category=cat,
                endpoint=endpoint or None,
                keywords=tuple([k for k in kw if k]),
                perms_any=(perm,) if perm else (),
            )
        )

    return items


def search(user, q: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Return permission-aware search results."""

    items = visible_items_for_user(user)
    qn = _norm(q)
    toks = _tokens(q)

    # When empty query → return a small set of top items
    if not qn:
        pref = [
            "help_master",
            "help_center",
            "wf_inbox",
            "wf_my_requests",
            "wf_circulars",
            "portal_home",
            "portal_circulars",
        ]
        ranked = sorted(items, key=lambda x: (pref.index(x.id) if x.id in pref else 999))
        ranked = ranked[: min(limit, 12)]
        return [
            {
                "id": it.id,
                "title": it.title,
                "desc": it.desc,
                "category": it.category,
                "href": it.href(),
            }
            for it in ranked
        ]

    scored: List[Tuple[float, SearchItem]] = []
    for it in items:
        blob = " ".join([it.title, it.desc, " ".join(it.keywords)])
        bn = _norm(blob)

        score = 0.0
        # exact substring gets strong weight
        if qn and qn in bn:
            score += 8.0

        # token matches
        if toks:
            hit = 0
            for t in toks:
                if t in bn:
                    hit += 1
            if hit == len(toks):
                score += 6.0
            elif hit > 0:
                score += 2.0 + hit

        # slight bonus for title match
        if qn and qn in _norm(it.title):
            score += 2.5

        if score > 0:
            scored.append((score, it))

    scored.sort(key=lambda x: (-x[0], x[1].category, x[1].title))
    scored = scored[:limit]

    return [
        {
            "id": it.id,
            "title": it.title,
            "desc": it.desc,
            "category": it.category,
            "href": it.href(),
        }
        for _, it in scored
    ]
