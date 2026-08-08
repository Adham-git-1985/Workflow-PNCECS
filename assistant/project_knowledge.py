"""Searchable, read-only knowledge about the application itself.

This module gives Aref a local retrieval layer for source code, templates,
documentation, project files, Flask routes, and the SQLAlchemy schema.  Source
content never leaves this module unless it was selected as relevant evidence
for an authenticated administrator's question.

Runtime data remains in :mod:`assistant.knowledge`, where the product's normal
permission and confidentiality checks are applied.  This module deliberately
does not provide arbitrary SQL or arbitrary file reads.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Iterable
import os
import re
import unicodedata

from flask import current_app
from sqlalchemy import func, select

from extensions import db


PROJECT_ROOT = Path(__file__).resolve().parents[1]

_AR_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_SPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[0-9A-Za-z_\u0600-\u06FF.-]+")
_DECLARATION = re.compile(
    r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)|^\s*#{1,6}\s+(.+)$"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret(?:[_-]?key)?|password|passwd|token|client[_-]?secret)[A-Za-z0-9_]*)"
    r"(\s*[:=]\s*)(['\"])(.*?)(\3)"
)
_QUOTED_SECRET_PAIR = re.compile(
    r"(?i)((?:['\"])[^'\"]*(?:api[_-]?key|secret(?:[_-]?key)?|password|passwd|token|client[_-]?secret)"
    r"[^'\"]*(?:['\"])\s*\]?\s*(?::|=)\s*)(['\"])(.*?)(\2)"
)
_URL_CREDENTIALS = re.compile(r"(?i)(https?://[^\s:/]+:)[^@\s]+(@)")
_CREDENTIAL_LINE = re.compile(
    r"(?i)(login\s+credentials|default\s+password|بيانات\s+الدخول|كلمات?\s+المرور|"
    r"(?:admin|user|super[_ -]?admin)\s*-?>\s*[^\s]+@[^\s]+\s*/\s*\S+)"
)

_TEXT_EXTENSIONS = {
    ".py", ".html", ".htm", ".jinja", ".jinja2", ".j2",
    ".js", ".css", ".scss", ".md", ".rst", ".txt", ".sql",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg",
    ".ps1", ".bat", ".mako", ".xml", ".svg", ".gitignore",
}
_SPECIAL_TEXT_FILES = {"dockerfile", "procfile", "requirements.txt", "updates.txt"}
_IGNORED_DIRS = {
    ".git", ".idea", ".venv", "venv", "__pycache__", ".pytest_cache",
    "node_modules", "instance", "logs", "tmp", "storage",
}
_SENSITIVE_FILE_NAMES = {
    ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
}
_SENSITIVE_NAME_PARTS = ("credential", "private_key", "private-key", "secrets.")
_STOP_WORDS = {
    "ما", "ماذا", "من", "في", "على", "عن", "الى", "إلى", "هو", "هي", "هل",
    "كيف", "اشرح", "اعطني", "أعطني", "اريد", "أريد", "كل", "هذا", "هذه", "ذلك",
    "the", "a", "an", "of", "to", "in", "and", "or", "is", "are", "show", "explain",
}
_PROJECT_INTENT_PHRASES = (
    "المشروع", "الكود", "كود المشروع", "ملفات المشروع", "هيكليه المشروع", "هيكلية المشروع",
    "بنيه المشروع", "بنية المشروع", "ملف بايثون", "داله", "دالة", "كلاس", "موديل",
    "كيف يعمل", "اليه العمل", "آلية العمل", "منطق النظام", "تنفيذ", "سورس", "source code",
    "function", "class", "module", "template", "endpoint", "route", "api",
)
_DATABASE_INTENT_PHRASES = (
    "قاعده البيانات", "قاعدة البيانات", "الجداول", "جدول", "الاعمده", "الأعمدة", "المخطط",
    "العلاقات", "sql", "sqlite", "sqlalchemy", "database", "schema", "table", "column",
)
_BROAD_PHRASES = (
    "كل شيء", "كل شي", "كل المشروع", "كل معلومات المشروع", "نظره شامله", "نظرة شاملة",
    "ملخص المشروع", "هيكليه المشروع", "هيكلية المشروع", "مكونات المشروع",
)

_SYNONYMS = {
    "قاعده البيانات": ("database", "sqlite", "sqlalchemy", "models", "db"),
    "قاعدة البيانات": ("database", "sqlite", "sqlalchemy", "models", "db"),
    "الجداول": ("table", "tablename", "models"),
    "جدول": ("table", "tablename", "model"),
    "المسارات": ("workflow", "routes", "routing"),
    "المعاملات": ("workflow_request", "workflow", "request"),
    "الصلاحيات": ("permission", "permissions", "has_perm", "role"),
    "الموظفين": ("employee", "employees", "users", "hr"),
    "الموظف": ("employee", "user", "hr"),
    "الاجازات": ("leave", "hr_leave"),
    "الإجازات": ("leave", "hr_leave"),
    "الحضور": ("attendance", "timeclock"),
    "المراسلات": ("correspondence", "corr", "inbound", "outbound"),
    "الوارد": ("inbound", "corr_inbound"),
    "الصادر": ("outbound", "corr_outbound"),
    "المستودع": ("inventory", "store", "warehouse", "inv"),
    "النقل": ("transport", "vehicle", "trip"),
    "الارشيف": ("archive", "archived_file"),
    "الأرشيف": ("archive", "archived_file"),
    "الواجهات": ("templates", "html", "javascript", "css"),
    "الصفحات": ("templates", "routes", "html"),
}

_DOMAIN_PATH_HINTS = {
    "permission": ("permissions", "models.py", "decorators", "perm_defs"),
    "database": ("models.py", "migrations", "init_db", "extensions.py"),
    "workflow": ("workflow", "models.py", "services/workflow"),
    "leave": ("portal", "models.py", "templates/portal/hr"),
    "attendance": ("portal", "models.py", "timeclock"),
    "correspondence": ("portal", "models.py", "corr"),
    "archive": ("archive", "models.py"),
    "inventory": ("store", "portal", "models.py"),
    "transport": ("portal/transport", "models.py"),
}

_SAFE_SAMPLE_TABLES = {
    "roles", "organizations", "directorates", "units", "departments", "sections", "divisions",
    "teams", "org_node_types", "org_nodes", "request_types", "workflow_templates",
    "hr_lookup_item", "hr_leave_type", "hr_status_def", "corr_category", "corr_party",
    "store_category", "inv_warehouse", "inv_item_category", "inv_unit", "transport_zone",
    "transport_destination",
}
_SAMPLE_COLUMNS = ("code", "name_ar", "name_en", "name", "title", "label", "category", "status")


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _AR_DIACRITICS.sub("", text).replace("ـ", "")
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي").replace("ة", "ه")
    text = text.replace("_", " ").replace("-", " ").replace(".", " ")
    return _SPACE.sub(" ", text).casefold().strip()


def _tokens(value: Any) -> list[str]:
    normalized = _norm(value)
    output: list[str] = []
    for token in _TOKEN.findall(normalized):
        if len(token) < 2 or token in _STOP_WORDS:
            continue
        if token not in output:
            output.append(token)
    for phrase, synonyms in _SYNONYMS.items():
        if _norm(phrase) in normalized:
            for synonym in synonyms:
                normalized_synonym = _norm(synonym)
                if normalized_synonym and normalized_synonym not in output:
                    output.append(normalized_synonym)
    return output[:24]


def _compact(value: Any, limit: int = 1000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _config_int(name: str, default: int) -> int:
    try:
        return int(current_app.config.get(name, default))
    except (RuntimeError, TypeError, ValueError):
        return default


def _config_bool(name: str, default: bool = True) -> bool:
    try:
        value = current_app.config.get(name, "1" if default else "0")
    except RuntimeError:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def internal_knowledge_allowed(user) -> bool:
    """Full project/schema knowledge is limited to administrator accounts."""
    if not user:
        return False
    try:
        if user.has_role("SUPER_ADMIN") or user.has_role("SUPERADMIN") or user.has_role("ADMIN"):
            return True
    except Exception:
        pass
    role = _norm(getattr(user, "role", "")).replace(" ", "_")
    return role in {"admin", "super_admin", "superadmin"} or role.startswith("super_")


def _sanitize_line(line: str) -> str:
    if _CREDENTIAL_LINE.search(line):
        return "[REDACTED CREDENTIAL LINE]"
    line = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(5)}", line)
    line = _QUOTED_SECRET_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(4)}", line)
    return _URL_CREDENTIALS.sub(r"\1[REDACTED]\2", line)


def _is_sensitive_file(path: Path) -> bool:
    name = path.name.casefold()
    if name in _SENSITIVE_FILE_NAMES or name.startswith(".env."):
        return True
    return any(part in name for part in _SENSITIVE_NAME_PARTS)


def _file_kind(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".py":
        return "code"
    if suffix in {".html", ".htm", ".jinja", ".jinja2", ".j2", ".mako"}:
        return "template"
    if suffix in {".js", ".css", ".scss"}:
        return "frontend"
    if suffix in {".md", ".rst", ".txt"}:
        return "documentation"
    if suffix in {".ps1", ".bat", ".sql"}:
        return "script"
    return "configuration"


def _chunk_title(lines: list[str], fallback: str) -> str:
    for line in lines[:18]:
        match = _DECLARATION.search(line)
        if match:
            return _compact(match.group(1) or match.group(2) or fallback, 160)
        stripped = line.strip()
        if stripped.startswith("@") and ".route(" in stripped:
            return _compact(stripped, 160)
    return fallback


@dataclass(frozen=True)
class KnowledgeChunk:
    kind: str
    path: str
    start_line: int
    end_line: int
    title: str
    text: str
    search_text: str = field(repr=False)

    @property
    def citation(self) -> str:
        if self.start_line > 0:
            return f"{self.path}:{self.start_line}"
        return self.path


@dataclass
class ProjectIndex:
    root: Path
    chunks: list[KnowledgeChunk]
    indexed_files: int
    discovered_files: int
    indexed_lines: int
    extension_counts: dict[str, int]
    directory_counts: dict[str, int]

    def search(self, query: str, limit: int = 7) -> list[tuple[float, KnowledgeChunk]]:
        normalized = _norm(query)
        tokens = _tokens(query)
        broad = any(_norm(phrase) in normalized for phrase in _BROAD_PHRASES)
        scored: list[tuple[float, KnowledgeChunk]] = []
        for chunk in self.chunks:
            score = 0.0
            path_text = _norm(chunk.path)
            title_text = _norm(chunk.title)
            haystack = chunk.search_text
            if normalized and len(normalized) >= 4 and normalized in haystack:
                score += 20.0
            hits = 0
            for token in tokens:
                if token in path_text:
                    score += 8.0
                    hits += 1
                elif token in title_text:
                    score += 6.0
                    hits += 1
                elif token in haystack:
                    score += 2.0
                    hits += 1
            if tokens and hits == len(tokens):
                score += 7.0
            elif hits:
                score += hits / max(len(tokens), 1)
            for domain_token, path_hints in _DOMAIN_PATH_HINTS.items():
                if domain_token in tokens and any(hint in chunk.path.casefold() for hint in path_hints):
                    score += 14.0
                    break
            if broad and chunk.kind == "overview":
                score += 40.0
            if broad and chunk.kind == "file_manifest":
                score += 5.0
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].path, item[1].start_line))
        return scored[: max(1, limit)]


def _make_chunk(kind: str, path: str, start: int, end: int, title: str, text: str) -> KnowledgeChunk:
    search_text = _norm(" ".join((path, title, text)))
    return KnowledgeChunk(kind, path, start, end, title, text, search_text)


def _iter_project_files(root: Path) -> Iterable[Path]:
    for current_root, directory_names, file_names in os.walk(root):
        directory_names[:] = [
            name for name in directory_names
            if name.casefold() not in _IGNORED_DIRS
        ]
        base = Path(current_root)
        for file_name in file_names:
            path = base / file_name
            if path.is_symlink() or _is_sensitive_file(path):
                continue
            yield path


def build_project_index(
    root: Path | str | None = None,
    *,
    max_file_bytes: int | None = None,
    chunk_lines: int | None = None,
) -> ProjectIndex:
    """Build an in-memory index without reading runtime/user-uploaded data."""
    root_path = Path(root or PROJECT_ROOT).resolve()
    max_bytes = max_file_bytes or _config_int("ASSISTANT_INDEX_MAX_FILE_BYTES", 1_500_000)
    line_window = max(16, chunk_lines or _config_int("ASSISTANT_INDEX_CHUNK_LINES", 48))
    overlap = min(10, max(4, line_window // 6))

    chunks: list[KnowledgeChunk] = []
    manifest: list[str] = []
    extension_counts: Counter[str] = Counter()
    directory_counts: Counter[str] = Counter()
    indexed_files = 0
    indexed_lines = 0

    for path in _iter_project_files(root_path):
        try:
            relative = path.relative_to(root_path).as_posix()
            size = path.stat().st_size
        except (OSError, ValueError):
            continue
        top_level = relative.split("/", 1)[0] if "/" in relative else "[root]"
        suffix = path.suffix.casefold() or "[no extension]"
        extension_counts[suffix] += 1
        directory_counts[top_level] += 1
        manifest.append(f"{relative} ({size} bytes)")

        is_text = suffix in _TEXT_EXTENSIONS or path.name.casefold() in _SPECIAL_TEXT_FILES
        if not is_text or size > max_bytes:
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "\x00" in raw:
            continue
        lines = [_sanitize_line(line) for line in raw.splitlines()]
        if not any(line.strip() for line in lines):
            continue

        indexed_files += 1
        indexed_lines += len(lines)
        kind = _file_kind(path)
        step = max(1, line_window - overlap)
        for offset in range(0, len(lines), step):
            window = lines[offset: offset + line_window]
            if not any(line.strip() for line in window):
                continue
            text = "\n".join(window).strip()
            start = offset + 1
            end = min(len(lines), offset + len(window))
            title = _chunk_title(window, path.name)
            chunks.append(_make_chunk(kind, relative, start, end, title, text))
            if end >= len(lines):
                break

    discovered_files = len(manifest)
    top_dirs = ", ".join(f"{name}: {count}" for name, count in directory_counts.most_common(18))
    top_extensions = ", ".join(f"{name}: {count}" for name, count in extension_counts.most_common(16))
    overview = (
        f"جذر المشروع: {root_path.name}\n"
        f"الملفات المكتشفة: {discovered_files}. الملفات النصية المفهرسة: {indexed_files}. "
        f"الأسطر المفهرسة: {indexed_lines}.\n"
        f"الوحدات والمجلدات الرئيسية: {top_dirs}.\n"
        f"أنواع الملفات: {top_extensions}.\n"
        "التطبيق Flask/SQLAlchemy ويضم مسار سير عمل، بوابة إدارية وموارد بشرية، "
        "مراسلات، أرشيف، مستودع، نقل، صلاحيات، رسائل، تدقيق، وخدمات مساندة."
    )
    chunks.insert(0, _make_chunk("overview", "PROJECT_OVERVIEW", 0, 0, "نظرة عامة على المشروع", overview))

    for offset in range(0, len(manifest), 80):
        group = manifest[offset: offset + 80]
        chunks.append(
            _make_chunk(
                "file_manifest",
                "PROJECT_FILES",
                offset + 1,
                offset + len(group),
                "قائمة ملفات المشروع",
                "\n".join(group),
            )
        )

    return ProjectIndex(
        root=root_path,
        chunks=chunks,
        indexed_files=indexed_files,
        discovered_files=discovered_files,
        indexed_lines=indexed_lines,
        extension_counts=dict(extension_counts),
        directory_counts=dict(directory_counts),
    )


_INDEX_LOCK = RLock()
_PROJECT_INDEX: ProjectIndex | None = None
_PROJECT_INDEX_BUILT_AT = 0.0


def get_project_index(*, force: bool = False) -> ProjectIndex:
    global _PROJECT_INDEX, _PROJECT_INDEX_BUILT_AT
    refresh_seconds = max(30, _config_int("ASSISTANT_INDEX_REFRESH_SECONDS", 300))
    now = monotonic()
    with _INDEX_LOCK:
        if force or _PROJECT_INDEX is None or now - _PROJECT_INDEX_BUILT_AT >= refresh_seconds:
            _PROJECT_INDEX = build_project_index()
            _PROJECT_INDEX_BUILT_AT = now
        return _PROJECT_INDEX


def rebuild_project_index() -> dict[str, Any]:
    index = get_project_index(force=True)
    return index_stats(index)


def index_stats(index: ProjectIndex | None = None) -> dict[str, Any]:
    index = index or get_project_index()
    return {
        "discovered_files": index.discovered_files,
        "indexed_files": index.indexed_files,
        "indexed_lines": index.indexed_lines,
        "chunks": len(index.chunks),
        "directories": len(index.directory_counts),
    }


def _source_from_chunk(chunk: KnowledgeChunk) -> dict[str, Any]:
    return {
        "type": chunk.kind,
        "label": chunk.citation,
        "path": chunk.path,
        "line": chunk.start_line or None,
        "end_line": chunk.end_line or None,
    }


def _excerpt(chunk: KnowledgeChunk, query: str, limit: int = 760) -> str:
    lines = chunk.text.splitlines()
    tokens = _tokens(query)
    if not lines:
        return ""
    best_index = 0
    best_score = -1
    for index, line in enumerate(lines):
        normalized = _norm(line)
        score = sum(1 for token in tokens if token in normalized)
        if score > best_score:
            best_index = index
            best_score = score
    start = max(0, best_index - 3)
    end = min(len(lines), start + 10)
    return _compact("\n".join(lines[start:end]).strip(), limit)


def _database_requested(message: str) -> bool:
    normalized = _norm(message)
    return any(_norm(phrase) in normalized for phrase in _DATABASE_INTENT_PHRASES)


def _project_requested(message: str) -> bool:
    normalized = _norm(message)
    return (
        any(_norm(phrase) in normalized for phrase in _PROJECT_INTENT_PHRASES)
        or any(_norm(phrase) in normalized for phrase in _DATABASE_INTENT_PHRASES)
        or bool(re.search(r"\b[\w.-]+\.(?:py|html|js|css|md)\b", str(message), re.IGNORECASE))
    )


def _broad_requested(message: str) -> bool:
    normalized = _norm(message)
    return any(_norm(phrase) in normalized for phrase in _BROAD_PHRASES)


def _table_group(name: str) -> str:
    if name.startswith(("hr_", "employee", "attendance", "work_")):
        return "الموارد البشرية"
    if name.startswith(("workflow", "request_")) or name in {"approval", "notification"}:
        return "مسار وسير العمل"
    if name.startswith(("corr_",)):
        return "المراسلات"
    if name.startswith(("inv_", "store_")):
        return "المستودع"
    if name.startswith("transport_"):
        return "النقل والحركة"
    if name.startswith(("org_",)) or name in {"organizations", "directorates", "departments", "sections", "units", "divisions", "teams"}:
        return "الهيكلية التنظيمية"
    if name in {"users", "roles", "role_permission", "user_permission", "delegations"}:
        return "المستخدمون والصلاحيات"
    if name.startswith(("archived_", "file_permission")):
        return "الأرشيف"
    if name.startswith(("message", "audit")):
        return "الرسائل والتدقيق"
    return "جداول مساندة"


def _database_table_score(table, message: str) -> float:
    normalized = _norm(message)
    terms = _tokens(message)
    table_name = _norm(table.name)
    columns = " ".join(_norm(column.name) for column in table.columns)
    blob = f"{table_name} {columns}"
    score = 0.0
    if table_name and table_name in normalized:
        score += 30.0
    for term in terms:
        if term in table_name:
            score += 8.0
        elif term in columns:
            score += 2.5
    return score


def _safe_table_samples(table) -> list[str]:
    if table.name not in _SAFE_SAMPLE_TABLES:
        return []
    columns = [table.c[name] for name in _SAMPLE_COLUMNS if name in table.c][:3]
    if not columns:
        return []
    try:
        rows = db.session.execute(select(*columns).limit(6)).all()
    except Exception:
        return []
    output: list[str] = []
    for row in rows:
        values = [str(value).strip() for value in row if value is not None and str(value).strip()]
        rendered = " | ".join(values)
        if rendered and rendered not in output:
            output.append(_compact(rendered, 180))
    return output


def _describe_table(table) -> tuple[str, list[str]]:
    try:
        row_count = int(db.session.execute(select(func.count()).select_from(table)).scalar_one())
    except Exception:
        row_count = -1
    columns = ", ".join(f"{column.name} ({column.type})" for column in table.columns)
    foreign_keys = ", ".join(
        f"{foreign_key.parent.name} → {foreign_key.target_fullname}"
        for foreign_key in table.foreign_keys
    ) or "لا توجد مفاتيح خارجية مصرّح بها"
    count_text = str(row_count) if row_count >= 0 else "تعذر حسابه"
    description = (
        f"الجدول {table.name} ضمن «{_table_group(table.name)}». عدد السجلات: {count_text}.\n"
        f"الأعمدة: {columns}.\nالعلاقات: {foreign_keys}."
    )
    samples = _safe_table_samples(table)
    if samples:
        description += "\nقيم مرجعية آمنة: " + "؛ ".join(samples) + "."
    return description, samples


def _database_evidence(message: str, *, broad: bool, limit: int = 6) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    tables = list(db.metadata.sorted_tables)
    if not tables:
        return [], [], []
    facts: list[str] = []
    evidence: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    if broad:
        groups = Counter(_table_group(table.name) for table in tables)
        summary = "، ".join(f"{name}: {count}" for name, count in groups.most_common())
        facts.append(f"مخطط قاعدة البيانات يحتوي {len(tables)} جدولًا: {summary}.")
        evidence.append({
            "label": "SQLAlchemy metadata",
            "type": "database_overview",
            "content": facts[-1],
        })
        sources.append({
            "type": "database_overview",
            "label": "قاعدة البيانات / مخطط SQLAlchemy",
            "path": "database://workflow.db",
            "line": None,
            "end_line": None,
        })

    scored = [(_database_table_score(table, message), table) for table in tables]
    scored = [(score, table) for score, table in scored if score > 0]
    scored.sort(key=lambda item: (-item[0], item[1].name))
    if broad and not scored:
        scored = [(1.0, table) for table in tables if table.name in {
            "users", "workflow_request", "workflow_templates", "organizations",
            "employee_file", "hr_leave_request", "corr_inbound", "corr_outbound",
        }]
    for _, table in scored[:limit]:
        description, _ = _describe_table(table)
        facts.append(_compact(description.replace("\n", " "), 420))
        evidence.append({
            "label": f"database://workflow.db/{table.name}",
            "type": "database_schema",
            "content": _compact(description, 2200),
        })
        sources.append({
            "type": "database_schema",
            "label": f"جدول {table.name}",
            "path": f"database://workflow.db/{table.name}",
            "line": None,
            "end_line": None,
        })
    return facts, evidence, sources


def _route_evidence(message: str, *, broad: bool, limit: int = 10) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        rules = [rule for rule in current_app.url_map.iter_rules() if rule.endpoint != "static"]
    except RuntimeError:
        return [], [], []
    query_tokens = _tokens(message)
    scored: list[tuple[float, Any]] = []
    for rule in rules:
        blob = _norm(f"{rule.endpoint} {rule.rule} {' '.join(sorted(rule.methods or []))}")
        score = sum(5.0 for token in query_tokens if token in blob)
        if score or broad:
            scored.append((score, rule))
    scored.sort(key=lambda item: (-item[0], item[1].rule, item[1].endpoint))
    selected = scored[:limit]
    if not selected:
        return [], [], []
    lines = [
        f"{','.join(sorted((rule.methods or set()) - {'HEAD', 'OPTIONS'})) or 'GET'} {rule.rule} → {rule.endpoint}"
        for _, rule in selected
    ]
    facts = [f"خريطة Flask المسجلة تحتوي {len(rules)} مسارًا (دون الملفات الثابتة)."]
    evidence = [{"label": "Flask URL map", "type": "routes", "content": "\n".join(lines)}]
    sources = [{
        "type": "routes",
        "label": "خريطة مسارات Flask الفعلية",
        "path": "flask://url-map",
        "line": None,
        "end_line": None,
    }]
    return facts, evidence, sources


def collect_internal_knowledge(
    user,
    message: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return admin-only evidence about code, files, routes, and DB schema."""
    empty = {"reply": "", "facts": [], "links": [], "sources": [], "evidence": [], "intents": []}
    if not _config_bool("ASSISTANT_PROJECT_KNOWLEDGE_ENABLED", True):
        return empty
    if not internal_knowledge_allowed(user) or not _project_requested(message):
        return empty

    context = context or {}
    broad = _broad_requested(message)
    index = get_project_index()
    max_results = max(2, min(12, _config_int("ASSISTANT_INDEX_MAX_RESULTS", 7)))
    matches = index.search(message, limit=max_results)
    if broad and not any(chunk.kind == "overview" for _, chunk in matches):
        matches.insert(0, (100.0, index.chunks[0]))

    facts: list[str] = [
        f"فهرس المشروع: {index.indexed_files} ملفًا نصيًا، {index.indexed_lines} سطرًا، "
        f"{len(index.chunks)} مقطع معرفة، من أصل {index.discovered_files} ملفًا مكتشفًا."
    ]
    sources: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    reply_lines = ["معرفة المشروع والكود (نطاق إداري)", facts[0]]

    for _, chunk in matches[:max_results]:
        excerpt = _excerpt(chunk, message)
        if not excerpt:
            continue
        source = _source_from_chunk(chunk)
        sources.append(source)
        evidence.append({
            "label": chunk.citation,
            "type": chunk.kind,
            "content": _compact(chunk.text, 2400),
        })
        if len(reply_lines) < 8:
            reply_lines.append(f"[{chunk.citation}] {chunk.title}\n{excerpt}")

    intents = ["project"]
    if _database_requested(message) or broad:
        db_facts, db_evidence, db_sources = _database_evidence(message, broad=broad)
        facts.extend(db_facts)
        evidence.extend(db_evidence)
        sources.extend(db_sources)
        intents.append("database")
        if db_facts:
            reply_lines.append("قاعدة البيانات\n" + "\n".join(db_facts[:4]))

    route_requested = broad or any(term in _norm(message) for term in ("route", "endpoint", "api", "مسارات flask", "الروابط"))
    if route_requested:
        route_facts, route_evidence, route_sources = _route_evidence(message, broad=broad)
        facts.extend(route_facts)
        evidence.extend(route_evidence)
        sources.extend(route_sources)
        if route_facts:
            intents.append("routes")
            reply_lines.extend(route_facts)

    seen_sources: set[tuple[str, Any]] = set()
    deduped_sources: list[dict[str, Any]] = []
    for source in sources:
        key = (str(source.get("path")), source.get("line"))
        if key in seen_sources:
            continue
        seen_sources.add(key)
        deduped_sources.append(source)

    return {
        "reply": _compact("\n\n".join(reply_lines), 7200),
        "facts": facts[:50],
        "links": [],
        "sources": deduped_sources[:14],
        "evidence": evidence[:14],
        "intents": intents,
        "index_stats": index_stats(index),
        "page": _compact(context.get("title"), 120),
    }
