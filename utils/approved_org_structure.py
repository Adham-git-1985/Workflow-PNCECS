from __future__ import annotations

from datetime import datetime
import re
import unicodedata
import zlib

from sqlalchemy import inspect, text as sa_text

from extensions import db
from models import (
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    SystemSetting,
)


APPROVED_STRUCTURE_VERSION = "2023-05-08:v2"
APPROVED_LEGACY_TYPE = "APPROVED_ORG_2023"


def _n(key: str, type_code: str, name_ar: str, children=(), aliases=()):
    return {
        "key": key,
        "type": type_code,
        "name_ar": name_ar,
        "children": tuple(children),
        "aliases": tuple(aliases),
    }


APPROVED_TYPE_SPECS = (
    ("ORGANIZATION", "مؤسسة", "Organization", 10, (), True),
    ("CHAIRPERSON", "رئيس اللجنة", "Chairperson", 15, ("ORGANIZATION",), True),
    ("SECRETARY_GENERAL", "الأمين العام", "Secretary General", 20, ("CHAIRPERSON", "ORGANIZATION"), True),
    ("GENERAL_DIRECTOR", "مدير عام", "General Director", 25, ("CHAIRPERSON", "SECRETARY_GENERAL"), True),
    ("ADVISOR", "مستشار", "Advisor", 28, ("SECRETARY_GENERAL",), True),
    ("SEC_GEN_ASSIST", "مساعد الأمين العام", "Assistant Secretary General", 30, ("SECRETARY_GENERAL",), True),
    ("COMMITTEE_GROUP", "لجان", "Committees", 32, ("SECRETARY_GENERAL",), True),
    ("COUNCIL", "مجلس", "Council", 34, ("SECRETARY_GENERAL",), True),
    ("OFFICE", "مكتب", "Office", 36, ("CHAIRPERSON", "SECRETARY_GENERAL", "GENERAL_DIRECTOR"), True),
    ("UNIT", "وحدة", "Unit", 40, ("SECRETARY_GENERAL",), True),
    ("DIRECTORATE", "إدارة عامة", "General Directorate", 50, ("SECRETARY_GENERAL", "SEC_GEN_ASSIST"), True),
    ("DEPARTMENT", "دائرة", "Department", 60, ("DIRECTORATE", "UNIT", "GENERAL_DIRECTOR"), True),
    ("SECTION", "قسم", "Section", 70, ("DEPARTMENT", "DIRECTORATE", "UNIT", "GENERAL_DIRECTOR", "OFFICE"), True),
    ("DIVISION", "شعبة", "Division", 80, ("SECTION",), True),
    # Teams are an operational extension and are intentionally outside the approved 2023 chart.
    ("TEAM", "فريق", "Team", 90, ("SECTION", "DIVISION"), False),
)


APPROVED_ORG_STRUCTURE = _n(
    "ORG_PNCECS",
    "ORGANIZATION",
    "اللجنة الوطنية الفلسطينية للتربية والثقافة والعلوم",
    aliases=(
        "اللجنة الوطنية للتربية وللثقافة والعلوم",
        "اللجنة الوطنية للتربية والثقافة والعلوم",
        "اللجنة الوطنية للتربية وللثقافة والعلوم > اللجنة الوطنية للتربية وللثقافة والعلوم",
    ),
    children=(
        _n(
            "ROLE_CHAIR",
            "CHAIRPERSON",
            "رئيس اللجنة",
            children=(
                _n(
                    "DG_CHAIR_OFFICE",
                    "GENERAL_DIRECTOR",
                    "مدير عام ديوان رئيس اللجنة",
                    children=(
                        _n("OFFICE_CHAIR", "OFFICE", "مدير مكتب رئيس اللجنة"),
                        _n("DEP_PROTOCOL", "DEPARTMENT", "دائرة البروتوكول"),
                        _n("SEC_SECRETARIAT", "SECTION", "قسم السكرتاريا"),
                    ),
                ),
                _n(
                    "ROLE_SG",
                    "SECRETARY_GENERAL",
                    "الأمين العام",
                    aliases=("الامين العام",),
                    children=(
                        _n(
                            "DG_SECRETARIAT",
                            "GENERAL_DIRECTOR",
                            "مدير عام الأمانة العامة",
                            aliases=("مدير عام الامانة العامة",),
                            children=(
                                _n("OFFICE_SG", "OFFICE", "مدير مكتب الأمين العام", aliases=("مكتب الأمين العام", "مكتب الامين العام")),
                                _n(
                                    "DEP_FOLLOWUP",
                                    "DEPARTMENT",
                                    "دائرة المتابعة والتنسيق",
                                    children=(
                                        _n("SEC_FOLLOWUP_INT", "SECTION", "قسم المتابعة الداخلية"),
                                        _n("SEC_FOLLOWUP_EXT", "SECTION", "قسم المتابعة الخارجية"),
                                    ),
                                ),
                            ),
                        ),
                        _n("ADVISOR_48", "ADVISOR", "مستشار لشؤون 48", aliases=("مستشار ٤٨",)),
                        _n("GROUP_COMMITTEES", "COMMITTEE_GROUP", "اللجان الدائمة والأساسية"),
                        _n("COUNCIL_EXEC", "COUNCIL", "المجلس التنفيذي"),
                        _n(
                            "UNIT_GENDER",
                            "UNIT",
                            "وحدة النوع الاجتماعي",
                            aliases=("وحدة النوع الإجتماعي",),
                            children=(
                                _n(
                                    "DEP_GENDER_AUDIT",
                                    "DEPARTMENT",
                                    "دائرة التدقيق والمتابعة من منظور النوع الاجتماعي",
                                    aliases=("دائرة التدقيق من منظور (النوع الاجتماعي)", "دائرة التدقيق من منظور  (النوع الإجتماعي)"),
                                ),
                                _n(
                                    "DEP_GENDER_DEV",
                                    "DEPARTMENT",
                                    "دائرة تطوير وإدماج النوع الاجتماعي",
                                    aliases=("دائرة تطوير وادماج النوع الاجتماعي", "دائرة تطوير وإدماج (النوع الإجتماعي)"),
                                ),
                            ),
                        ),
                        _n(
                            "UNIT_PR_MEDIA",
                            "UNIT",
                            "وحدة العلاقات العامة والإعلام",
                            aliases=("وحدة العلاقات العامة والاعلام",),
                            children=(
                                _n("DEP_MEDIA", "DEPARTMENT", "دائرة الإعلام", aliases=("دائرة الاعلام",)),
                                _n("DEP_PUBLIC_REL", "DEPARTMENT", "دائرة العلاقات العامة"),
                                _n("DEP_TRANSLATION", "DEPARTMENT", "دائرة الترجمة"),
                            ),
                        ),
                        _n(
                            "UNIT_LEGAL",
                            "UNIT",
                            "وحدة الشؤون القانونية",
                            children=(
                                _n("DEP_AGREEMENTS", "DEPARTMENT", "دائرة الاتفاقيات", aliases=("دائرة الإتفاقيات",)),
                                _n(
                                    "DEP_LEGAL_SUPPORT",
                                    "DEPARTMENT",
                                    "دائرة الاستشارات والإسناد القانوني",
                                    aliases=("دائرة الإستشارات والإسناد القانوني", "دائرة الاستشارات القانونية"),
                                ),
                            ),
                        ),
                        _n(
                            "UNIT_INTERNAL_AUDIT",
                            "UNIT",
                            "وحدة الرقابة الداخلية",
                            children=(
                                _n("DEP_FIN_AUDIT", "DEPARTMENT", "دائرة الرقابة المالية"),
                                _n("DEP_ADMIN_AUDIT", "DEPARTMENT", "دائرة الرقابة الإدارية وتقويم الأداء"),
                            ),
                        ),
                        _n("OFFICE_JERUSALEM", "OFFICE", "مكتب القدس"),
                        _n("OFFICE_GAZA", "OFFICE", "مكتب غزة"),
                        _n("OFFICE_WB_NORTH", "OFFICE", "مكتب شمال الضفة"),
                        _n("OFFICE_WB_SOUTH", "OFFICE", "مكتب جنوب الضفة"),
                        _n("OFFICE_BEIRUT", "OFFICE", "مكتب بيروت"),
                        _n(
                            "ASST_SUPPORT",
                            "SEC_GEN_ASSIST",
                            "مساعد الأمين العام للتخطيط والخدمات المساندة (الموارد البشرية والمالية والمعلوماتية)",
                            aliases=("مساعد الأمين العام للتخطيط والخدمات المساندة",),
                            children=(
                                _n(
                                    "DIR_INFO_PUB",
                                    "DIRECTORATE",
                                    "الإدارة العامة للمعلوماتية والمطبوعات",
                                    aliases=("الادارة العامة للمعلوماتية والمطبوعات",),
                                    children=(
                                        _n(
                                            "DEP_INFORMATION",
                                            "DEPARTMENT",
                                            "دائرة المعلومات",
                                            children=(
                                                _n("SEC_INFORMATION", "SECTION", "قسم المعلومات"),
                                                _n("SEC_ANALYTICS", "SECTION", "قسم التحليل والإحصاء"),
                                                _n("SEC_E_ARCHIVE", "SECTION", "قسم الأرشفة الإلكترونية"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_TECH_SUPPORT",
                                            "DEPARTMENT",
                                            "دائرة الدعم الفني",
                                            children=(
                                                _n("SEC_PROGRAMMING_SUPPORT", "SECTION", "قسم البرمجة والدعم الفني"),
                                                _n("SEC_NETWORK_MAINT", "SECTION", "قسم الشبكات والصيانة"),
                                                _n("SEC_DESIGN_WEB", "SECTION", "قسم التصميم والصفحة الإلكترونية"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_PUBLICATIONS",
                                            "DEPARTMENT",
                                            "دائرة المطبوعات",
                                            children=(
                                                _n("SEC_PUBLISHING", "SECTION", "قسم المطبوعات والنشر"),
                                                _n("SEC_LANGUAGE_EDIT", "SECTION", "قسم التحرير اللغوي", aliases=("قسم التعليق اللغوي",)),
                                                _n("SEC_PRINT_TECH", "SECTION", "قسم التقنية"),
                                            ),
                                        ),
                                    ),
                                ),
                                _n(
                                    "DIR_PLAN_HR_FIN",
                                    "DIRECTORATE",
                                    "الإدارة العامة للتخطيط والموارد البشرية والمالية",
                                    aliases=("الإدارة العامة للشؤون الإدارية والمالية",),
                                    children=(
                                        _n(
                                            "DEP_HR",
                                            "DEPARTMENT",
                                            "دائرة الموارد البشرية",
                                            children=(
                                                _n("SEC_IN_OUT", "SECTION", "قسم الصادر والوارد"),
                                                _n("SEC_ADMIN_AFFAIRS", "SECTION", "قسم الشؤون الإدارية"),
                                                _n("SEC_PROCUREMENT", "SECTION", "قسم اللوازم والمشتريات"),
                                                _n("SEC_TRAINING", "SECTION", "قسم التدريب وبناء الخبرات"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_RES_FIN",
                                            "DEPARTMENT",
                                            "دائرة الموارد والشؤون المالية",
                                            children=(
                                                _n("SEC_ACCOUNTING", "SECTION", "قسم المحاسبة"),
                                                _n("SEC_CASH", "SECTION", "قسم الصندوق"),
                                                _n("SEC_BUDGETS", "SECTION", "قسم الموازنات"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_PLANNING",
                                            "DEPARTMENT",
                                            "دائرة التخطيط والسياسات",
                                            children=(
                                                _n("SEC_PLANNING", "SECTION", "قسم التخطيط والسياسات"),
                                                _n("SEC_STUDIES", "SECTION", "قسم الدراسات والأبحاث"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_PROJECT_FIN",
                                            "DEPARTMENT",
                                            "دائرة الموارد والشؤون المالية للمشاريع",
                                            aliases=("دائرة مالية المشاريع",),
                                            children=(
                                                _n("SEC_PROJECT_ACCOUNTING", "SECTION", "قسم المحاسبة"),
                                                _n("SEC_PROJECT_CASH", "SECTION", "قسم الصندوق"),
                                                _n("SEC_PROJECT_PROCUREMENT", "SECTION", "قسم اللوازم والمشتريات"),
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                        _n(
                            "ASST_PROGRAMS",
                            "SEC_GEN_ASSIST",
                            "مساعد الأمين العام للمنظمات والبرامج والمشاريع (الإدارات التخصصية)",
                            aliases=("مساعد الامين العام للمنظمات والبرامج والمشاريع (الادارات التخصصية)",),
                            children=(
                                _n(
                                    "DIR_SPECIALIZED",
                                    "DIRECTORATE",
                                    "الإدارة العامة للدوائر المتخصصة",
                                    aliases=("الادارة العامة للدوائر المتخصصة", "الإدارة العامة المتخصصة"),
                                    children=(
                                        _n(
                                            "DEP_COMM_IT",
                                            "DEPARTMENT",
                                            "دائرة الاتصالات وتكنولوجيا المعلومات",
                                            aliases=("دائرة الإتصالات", "دائرة الاتصالات"),
                                            children=(
                                                _n("SEC_DIGITAL_TECH", "SECTION", "قسم التكنولوجيا الرقمية", aliases=("قسم تكنولوجيا الرقمية",)),
                                                _n("SEC_TECH_ENGINEERING", "SECTION", "قسم الهندسة التكنولوجية"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_CULTURE",
                                            "DEPARTMENT",
                                            "دائرة الثقافة",
                                            children=(
                                                _n("SEC_CULTURAL_DEV", "SECTION", "قسم التنمية الثقافية"),
                                                _n(
                                                    "SEC_HERITAGE",
                                                    "SECTION",
                                                    "قسم حماية التراث",
                                                    children=(
                                                        _n("DIV_HERITAGE_MAT", "DIVISION", "شعبة التراث المادي"),
                                                        _n("DIV_HERITAGE_IMMAT", "DIVISION", "شعبة التراث غير المادي"),
                                                    ),
                                                ),
                                            ),
                                        ),
                                        _n(
                                            "DEP_EDUCATION",
                                            "DEPARTMENT",
                                            "دائرة التربية والتعليم العالي",
                                            aliases=("دائرة التربية",),
                                            children=(
                                                _n(
                                                    "SEC_PRE_UNI",
                                                    "SECTION",
                                                    "قسم التعليم ما قبل الجامعة",
                                                    aliases=("قسم التعليم ماقبل الجامعة",),
                                                    children=(_n("DIV_TECH_VOCATIONAL", "DIVISION", "شعبة التدريس الفني والمهني"),),
                                                ),
                                                _n(
                                                    "SEC_HIGH_ED",
                                                    "SECTION",
                                                    "قسم التعليم العالي",
                                                    children=(_n("DIV_COLLEGES_UNIS", "DIVISION", "شعبة الكليات والمعاهد والجامعات"),),
                                                ),
                                                _n(
                                                    "SEC_ADULT_SPECIAL",
                                                    "SECTION",
                                                    "قسم تعليم الكبار والتربية الخاصة",
                                                    children=(_n("DIV_SPECIAL_ADULT", "DIVISION", "شعبة التربية الخاصة وتعليم الكبار"),),
                                                ),
                                            ),
                                        ),
                                        _n(
                                            "DEP_SCIENCE",
                                            "DEPARTMENT",
                                            "دائرة العلوم والبحث العلمي",
                                            aliases=("دائرة العلوم البيئية",),
                                            children=(
                                                _n("SEC_ENV", "SECTION", "قسم البيئة والموارد الطبيعية"),
                                                _n("SEC_NATURAL_SCI", "SECTION", "قسم العلوم الطبيعية"),
                                                _n("SEC_RESEARCH", "SECTION", "قسم البحث العلمي"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_HUMAN_SOCIAL",
                                            "DEPARTMENT",
                                            "دائرة العلوم الإنسانية والاجتماعية",
                                            aliases=("دائرة العلوم الانسانية والاجتماعية", "دائرة العلوم الإنسانية"),
                                            children=(
                                                _n("SEC_HUMANITIES", "SECTION", "قسم العلوم الإنسانية"),
                                                _n("SEC_SOCIAL_SCI", "SECTION", "قسم العلوم الاجتماعية"),
                                                _n("SEC_WOMEN_CHILD", "SECTION", "قسم شؤون المرأة والطفولة"),
                                            ),
                                        ),
                                    ),
                                ),
                                _n(
                                    "DIR_PROGRAMS",
                                    "DIRECTORATE",
                                    "الإدارة العامة للبرامج والمشاريع",
                                    children=(
                                        _n(
                                            "DEP_PROGRAMS",
                                            "DEPARTMENT",
                                            "دائرة البرامج",
                                            children=(
                                                _n("SEC_PROGRAM_ACT", "SECTION", "قسم البرامج والأنشطة"),
                                                _n("SEC_FUNDRAISING", "SECTION", "قسم تجنيد الأموال"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_PROJECTS",
                                            "DEPARTMENT",
                                            "دائرة المشاريع",
                                            children=(
                                                _n("SEC_PROJECTS", "SECTION", "قسم المشاريع"),
                                                _n("SEC_MONITOR_EVAL", "SECTION", "قسم المتابعة والتقييم"),
                                            ),
                                        ),
                                    ),
                                ),
                                _n(
                                    "DIR_ORGS",
                                    "DIRECTORATE",
                                    "الإدارة العامة للمنظمات العربية والإسلامية والعلاقات الدولية",
                                    aliases=("الادارة العامة للمنظمات العربية والاسلامية والعلاقات الدولية", "الإدارة العامة للمنظمات"),
                                    children=(
                                        _n(
                                            "DEP_ALECSO",
                                            "DEPARTMENT",
                                            "دائرة الألكسو",
                                            aliases=("دائرة الالكسو",),
                                            children=(
                                                _n("SEC_ALECSO_FOLLOWUP", "SECTION", "قسم المتابعة والتنسيق"),
                                                _n("SEC_ARAB_REL", "SECTION", "قسم العلاقات العربية"),
                                                _n("SEC_ARAB_ORG_ACT", "SECTION", "قسم الأنشطة والمنظمات العربية"),
                                                _n("SEC_ICESCO_REL", "SECTION", "قسم العلاقات الدولية"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_ICESCO",
                                            "DEPARTMENT",
                                            "دائرة الإيسيسكو",
                                            aliases=(
                                                "دائرة الايسيسكو",
                                                "اللجنة الوطنية للتربية وللثقافة والعلوم>الامين العام>مساعد الامين العام للمنظمات والبرامج والمشاريع (الادارات التخصصية)>الادارة العامة للمنظمات العربية والاسلامية والعلاقات الدولية >دائرة الايسيسكو",
                                            ),
                                            children=(
                                                _n("SEC_ICESCO_FOLLOWUP", "SECTION", "قسم المتابعة والتنسيق"),
                                            ),
                                        ),
                                        _n(
                                            "DEP_UNESCO",
                                            "DEPARTMENT",
                                            "دائرة اليونسكو",
                                            children=(
                                                _n("SEC_UNESCO_REL", "SECTION", "قسم العلاقات الدولية"),
                                                _n("SEC_UNESCO_CLUBS", "SECTION", "قسم المدارس والأندية المنتسبة لليونسكو"),
                                                _n("SEC_UNESCO_FOLLOWUP", "SECTION", "قسم المتابعة والتنسيق"),
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    ),
)


def _normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", (value or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ؤ": "و", "ئ": "ي", "ة": "ه"}))
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def _marker_id(key: str) -> int:
    return zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF


def flatten_approved_structure():
    rows = []

    def walk(spec, parent_key=None, sort_order=0):
        rows.append({**spec, "parent_key": parent_key, "sort_order": sort_order})
        for index, child in enumerate(spec["children"], start=1):
            walk(child, spec["key"], index * 10)

    walk(APPROVED_ORG_STRUCTURE)
    marker_ids = [_marker_id(row["key"]) for row in rows]
    if len(marker_ids) != len(set(marker_ids)):
        raise RuntimeError("Approved organization marker collision")
    return rows


def find_approved_org_node_by_name(
    name: str | None,
    type_code: str | None = None,
) -> OrgNode | None:
    """Resolve a legacy label or alias to its canonical approved OrgNode.

    Legacy workflow templates still reference the old master-data tables.  An
    approved node can have a newer canonical name (for example, the legacy
    ``دائرة التربية`` became ``دائرة التربية والتعليم العالي``).  Matching
    against the approved aliases keeps those saved templates connected to the
    manager/deputy configured on the canonical organization chart.

    Ambiguous labels return ``None`` unless exactly one matching node is the
    operational node with a configured manager or deputy.
    """
    normalized_name = _normalize(name)
    if not normalized_name:
        return None

    wanted_type = (type_code or "").strip().upper()
    matched_keys: set[str] = set()
    for spec in flatten_approved_structure():
        if wanted_type and (spec.get("type") or "").strip().upper() != wanted_type:
            continue
        labels = (spec.get("name_ar"), *(spec.get("aliases") or ()))
        if normalized_name in {_normalize(label) for label in labels if label}:
            matched_keys.add(spec["key"])

    if len(matched_keys) != 1:
        return None

    key = next(iter(matched_keys))
    matched_spec = next(spec for spec in flatten_approved_structure() if spec["key"] == key)
    node = OrgNode.query.filter_by(code=key, is_active=True).first()
    if node is not None:
        return node

    node = OrgNode.query.filter_by(
        legacy_type=APPROVED_LEGACY_TYPE,
        legacy_id=_marker_id(key),
        is_active=True,
    ).first()
    if node is not None:
        return node

    # Some databases contain the approved hierarchy from an earlier importer
    # that did not stamp ``code``/``legacy_type``.  Match its canonical label
    # in Python so SQLite collation and Arabic normalization cannot hide it.
    canonical_labels = {
        _normalize(label)
        for label in (
            matched_spec.get("name_ar"),
            *(matched_spec.get("aliases") or ()),
        )
        if label
    }
    candidates = (
        OrgNode.query
        .join(OrgNodeType, OrgNode.type_id == OrgNodeType.id)
        .filter(
            OrgNode.is_active.is_(True),
            OrgNodeType.code == matched_spec["type"],
        )
        .all()
    )
    candidates = [
        candidate
        for candidate in candidates
        if _normalize(candidate.name_ar) in canonical_labels
    ]
    if len(candidates) == 1:
        return candidates[0]

    # Legacy synchronization can leave multiple active aliases in the dynamic
    # tree.  When exactly one of them is the node on which management was
    # actually configured, that node is the unambiguous operational target.
    if candidates:
        candidate_ids = [candidate.id for candidate in candidates]
        managed_node_ids = {
            row.node_id
            for row in OrgNodeManager.query.filter(
                OrgNodeManager.node_id.in_(candidate_ids),
                (
                    OrgNodeManager.manager_user_id.isnot(None)
                    | OrgNodeManager.deputy_user_id.isnot(None)
                ),
            ).all()
        }
        managed_candidates = [
            candidate for candidate in candidates if candidate.id in managed_node_ids
        ]
        if len(managed_candidates) == 1:
            return managed_candidates[0]

    return None


def _setting_set(key: str, value: str):
    row = SystemSetting.query.filter_by(key=key).first()
    if row is None:
        row = SystemSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value


def _ensure_types():
    type_by_code = {(row.code or "").strip().upper(): row for row in OrgNodeType.query.all()}
    for code, name_ar, name_en, sort_order, _, show_in_chart in APPROVED_TYPE_SPECS:
        row = type_by_code.get(code)
        if row is None:
            row = OrgNodeType(code=code, name_ar=name_ar, created_at=datetime.utcnow())
            db.session.add(row)
            type_by_code[code] = row
        row.name_ar = name_ar
        row.name_en = name_en
        row.sort_order = sort_order
        row.allow_in_approvals = True
        row.show_in_chart = show_in_chart
        row.show_in_routes = True
        row.is_active = True
    db.session.flush()
    for code, _, _, _, parent_codes, _ in APPROVED_TYPE_SPECS:
        type_by_code[code].set_allowed_parent_type_ids([type_by_code[p].id for p in parent_codes])
    return type_by_code


def _merge_node_references(source: OrgNode, target: OrgNode):
    """Move all references from a duplicate node to its canonical node.

    These updates intentionally use SQL instead of deleting loaded relationship
    objects.  Several of the relationships are joined eagerly; mutating their
    loaded graph while rebuilding the entire tree can otherwise make SQLAlchemy
    cascade stale in-memory state back onto canonical nodes during autoflush.
    """
    source_id = int(source.id)
    target_id = int(target.id)
    db.session.flush()

    assignments = db.session.execute(
        sa_text(
            "SELECT id, user_id, title, is_primary "
            "FROM org_node_assignments WHERE node_id = :source"
        ),
        {"source": source_id},
    ).mappings().all()
    for assignment in assignments:
        existing = db.session.execute(
            sa_text(
                "SELECT id, title, is_primary FROM org_node_assignments "
                "WHERE user_id = :user_id AND node_id = :target LIMIT 1"
            ),
            {"user_id": assignment["user_id"], "target": target_id},
        ).mappings().first()
        if existing:
            db.session.execute(
                sa_text(
                    "UPDATE org_node_assignments "
                    "SET title = :title, is_primary = :is_primary WHERE id = :id"
                ),
                {
                    "id": existing["id"],
                    "title": existing["title"] or assignment["title"],
                    "is_primary": bool(existing["is_primary"] or assignment["is_primary"]),
                },
            )
            db.session.execute(
                sa_text("DELETE FROM org_node_assignments WHERE id = :id"),
                {"id": assignment["id"]},
            )
        else:
            db.session.execute(
                sa_text("UPDATE org_node_assignments SET node_id = :target WHERE id = :id"),
                {"target": target_id, "id": assignment["id"]},
            )

    source_manager = db.session.execute(
        sa_text(
            "SELECT id, manager_user_id, deputy_user_id, updated_by_id "
            "FROM org_node_managers WHERE node_id = :source LIMIT 1"
        ),
        {"source": source_id},
    ).mappings().first()
    if source_manager:
        target_manager = db.session.execute(
            sa_text(
                "SELECT id, manager_user_id, deputy_user_id, updated_by_id "
                "FROM org_node_managers WHERE node_id = :target LIMIT 1"
            ),
            {"target": target_id},
        ).mappings().first()
        if target_manager:
            manager_candidates = []
            for user_id in (
                target_manager["manager_user_id"],
                target_manager["deputy_user_id"],
                source_manager["manager_user_id"],
                source_manager["deputy_user_id"],
            ):
                if user_id and user_id not in manager_candidates:
                    manager_candidates.append(user_id)
            if len(manager_candidates) > 2:
                raise RuntimeError(
                    f"Cannot merge organization nodes {source_id} and {target_id}: "
                    "more than two distinct manager/deputy assignments"
                )
            db.session.execute(
                sa_text(
                    "UPDATE org_node_managers SET manager_user_id = :manager_user_id, "
                    "deputy_user_id = :deputy_user_id, updated_by_id = :updated_by_id "
                    "WHERE id = :id"
                ),
                {
                    "id": target_manager["id"],
                    "manager_user_id": manager_candidates[0] if manager_candidates else None,
                    "deputy_user_id": manager_candidates[1] if len(manager_candidates) > 1 else None,
                    "updated_by_id": target_manager["updated_by_id"] or source_manager["updated_by_id"],
                },
            )
            db.session.execute(
                sa_text("DELETE FROM org_node_managers WHERE id = :id"),
                {"id": source_manager["id"]},
            )
        else:
            db.session.execute(
                sa_text("UPDATE org_node_managers SET node_id = :target WHERE id = :id"),
                {"target": target_id, "id": source_manager["id"]},
            )

    inspector = inspect(db.session.connection())
    table_names = set(inspector.get_table_names())
    references = (
        ("users", "org_node_id"),
        ("workflow_instance_steps", "approver_org_node_id"),
        ("workflow_routing_rules", "org_node_id"),
        ("workflow_template_parallel_assignees", "approver_org_node_id"),
        ("workflow_template_steps", "approver_org_node_id"),
    )
    for table_name, column_name in references:
        if table_name not in table_names:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name not in columns:
            continue
        db.session.execute(
            sa_text(f"UPDATE {table_name} SET {column_name} = :target WHERE {column_name} = :source"),
            {"target": target_id, "source": source_id},
        )

    db.session.execute(
        sa_text("UPDATE org_nodes SET parent_id = :target WHERE parent_id = :source"),
        {"target": target_id, "source": source_id},
    )
    db.session.execute(
        sa_text("UPDATE org_nodes SET is_active = 0, parent_id = NULL WHERE id = :source"),
        {"source": source_id},
    )
    db.session.expire_all()


def _node_has_references(node_id: int) -> bool:
    if OrgNodeAssignment.query.filter_by(node_id=node_id).first() is not None:
        return True
    if OrgNodeManager.query.filter_by(node_id=node_id).first() is not None:
        return True
    if OrgNode.query.filter_by(parent_id=node_id, is_active=True).first() is not None:
        return True
    inspector = inspect(db.session.connection())
    table_names = set(inspector.get_table_names())
    for table_name, column_name in (
        ("users", "org_node_id"),
        ("workflow_instance_steps", "approver_org_node_id"),
        ("workflow_routing_rules", "org_node_id"),
        ("workflow_template_parallel_assignees", "approver_org_node_id"),
        ("workflow_template_steps", "approver_org_node_id"),
    ):
        if table_name not in table_names:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name not in columns:
            continue
        found = db.session.execute(
            sa_text(f"SELECT 1 FROM {table_name} WHERE {column_name} = :node_id LIMIT 1"),
            {"node_id": node_id},
        ).first()
        if found:
            return True
    return False


def apply_approved_org_structure(*, deactivate_unlisted: bool = True, lock_legacy: bool = True):
    """Idempotently apply the approved 08-05-2023 organization chart.

    Existing matching nodes are reused so employee assignments, managers, and
    workflow references keep their node IDs. Duplicate import artifacts are
    merged into their canonical targets before being deactivated.
    """
    specs = flatten_approved_structure()
    type_by_code = _ensure_types()
    existing_nodes = OrgNode.query.order_by(OrgNode.id.asc()).all()
    claimed_ids = set()
    nodes_by_key = {}
    created = 0
    reused = 0

    by_marker = {
        int(node.legacy_id): node
        for node in existing_nodes
        if node.legacy_type == APPROVED_LEGACY_TYPE and node.legacy_id is not None
    }
    by_code = {(node.code or "").strip().upper(): node for node in existing_nodes if (node.code or "").strip()}

    for spec in specs:
        parent = nodes_by_key.get(spec["parent_key"])
        marker_id = _marker_id(spec["key"])
        node = by_marker.get(marker_id) or by_code.get(spec["key"])
        if node is not None and node.id in claimed_ids:
            node = None

        if node is None:
            wanted_names = {_normalize(spec["name_ar"]), *(_normalize(alias) for alias in spec["aliases"])}
            candidates = [
                candidate
                for candidate in existing_nodes
                if candidate.id not in claimed_ids and _normalize(candidate.name_ar) in wanted_names
            ]
            if candidates:
                candidates.sort(key=lambda candidate: (
                    0 if (candidate.parent_id or None) == (parent.id if parent else None) else 1,
                    0 if getattr(candidate.type, "code", "") == spec["type"] else 1,
                    candidate.id,
                ))
                node = candidates[0]

        if node is None:
            node = OrgNode(created_at=datetime.utcnow(), updated_at=datetime.utcnow())
            db.session.add(node)
            existing_nodes.append(node)
            created += 1
        else:
            reused += 1

        node.type_id = type_by_code[spec["type"]].id
        node.parent_id = parent.id if parent else None
        node.name_ar = spec["name_ar"]
        node.name_en = None
        node.code = spec["key"]
        node.sort_order = spec["sort_order"]
        node.is_active = True
        node.legacy_type = APPROVED_LEGACY_TYPE
        node.legacy_id = marker_id
        node.updated_at = datetime.utcnow()
        db.session.flush()
        claimed_ids.add(node.id)
        nodes_by_key[spec["key"]] = node

    alias_targets = {}
    for spec in specs:
        for value in (spec["name_ar"], *spec["aliases"]):
            alias_targets.setdefault(_normalize(value), set()).add(spec["key"])

    approved_before_cleanup = OrgNode.query.filter_by(legacy_type=APPROVED_LEGACY_TYPE, is_active=True).count()
    if approved_before_cleanup != len(specs):
        raise RuntimeError(
            f"Approved organization staging failed: expected {len(specs)}, found {approved_before_cleanup}"
        )

    merged = 0
    deactivated = 0
    for node in list(existing_nodes):
        if node.id in claimed_ids or not node.is_active:
            continue
        target_keys = alias_targets.get(_normalize(node.name_ar), set())
        if len(target_keys) == 1:
            target = nodes_by_key[next(iter(target_keys))]
            _merge_node_references(node, target)
            merged += 1
            approved_after_merge = OrgNode.query.filter_by(
                legacy_type=APPROVED_LEGACY_TYPE,
                is_active=True,
            ).count()
            if approved_after_merge != len(specs):
                raise RuntimeError(
                    f"Approved organization merge corrupted canonical nodes while handling "
                    f"{node.id} {node.name_ar}: found {approved_after_merge}"
                )
            continue
        if deactivate_unlisted:
            if _node_has_references(node.id):
                raise RuntimeError(f"Unmapped referenced organization node: {node.id} {node.name_ar}")
            node.is_active = False
            node.parent_id = None
            deactivated += 1

    applied_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    _setting_set("ORG_APPROVED_STRUCTURE_VERSION", APPROVED_STRUCTURE_VERSION)
    _setting_set("ORG_APPROVED_STRUCTURE_APPLIED_AT", applied_at)
    _setting_set("ORG_NODE_LAST_SYNC", applied_at)
    if lock_legacy:
        _setting_set("ORG_LEGACY_LOCKED", "1")

    db.session.flush()
    expected = len(specs)
    approved_active = OrgNode.query.filter_by(legacy_type=APPROVED_LEGACY_TYPE, is_active=True).count()
    if approved_active != expected:
        approved_total = OrgNode.query.filter_by(legacy_type=APPROVED_LEGACY_TYPE).count()
        raise RuntimeError(
            f"Approved organization validation failed: expected {expected}, "
            f"found {approved_active} active ({approved_total} total)"
        )

    info = nodes_by_key["DEP_INFORMATION"]
    info_children = {
        child.code for child in OrgNode.query.filter_by(parent_id=info.id, is_active=True).all()
    }
    if info_children != {"SEC_INFORMATION", "SEC_ANALYTICS", "SEC_E_ARCHIVE"}:
        raise RuntimeError("Information department children do not match the approved chart")

    return {
        "version": APPROVED_STRUCTURE_VERSION,
        "expected": expected,
        "created": created,
        "reused": reused,
        "merged": merged,
        "deactivated": deactivated,
        "nodes_by_key": nodes_by_key,
    }
