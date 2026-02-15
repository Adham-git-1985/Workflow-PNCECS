"""System-wide feature search (مسار + البوابة الإدارية).

This is a lightweight "command palette" style search:
- Searches across key features/screens + help guides.
- Results are permission-aware (items are hidden if user can't access them).

We intentionally keep the registry static and small to avoid heavy DB queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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

    return out


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
