ACHIEVEMENT_BONUS_CAP = 5.0

ACHIEVEMENT_TYPES = {
    "AWARD": {"label": "جائزة أو تكريم", "icon": "bi-trophy"},
    "RESEARCH": {"label": "بحث أو نشر علمي", "icon": "bi-journal-richtext"},
    "INNOVATION": {"label": "ابتكار أو مبادرة", "icon": "bi-lightbulb"},
    "DEVELOPMENT": {"label": "تطوير أو تحسين", "icon": "bi-rocket-takeoff"},
    "COMMUNITY": {"label": "مشاركة أو أثر مجتمعي", "icon": "bi-people"},
    "OTHER": {"label": "إنجاز مميز آخر", "icon": "bi-star"},
}

ACHIEVEMENT_LEVELS = {
    "NOTABLE": {
        "label": "إنجاز موثّق",
        "points": 1.0,
        "class": "info",
        "description": "إنجاز يتجاوز العمل الروتيني وله دليل واضح.",
    },
    "SIGNIFICANT": {
        "label": "إنجاز مميز",
        "points": 2.0,
        "class": "warning",
        "description": "إنجاز ذو أثر ملموس على العمل أو سمعة المؤسسة.",
    },
    "EXCEPTIONAL": {
        "label": "إنجاز استثنائي",
        "points": 3.0,
        "class": "danger",
        "description": "إنجاز نادر واسع الأثر مثل جائزة مهمة أو ابتكار نوعي.",
    },
}

ACHIEVEMENT_STATUSES = {
    "PENDING": {"label": "قيد المراجعة", "class": "warning"},
    "APPROVED": {"label": "معتمد ومحتسب", "class": "success"},
    "REJECTED": {"label": "غير معتمد", "class": "secondary"},
}


def achievement_points(level: str | None) -> float:
    return float((ACHIEVEMENT_LEVELS.get((level or "").upper()) or {}).get("points") or 0.0)
