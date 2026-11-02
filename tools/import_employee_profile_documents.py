"""Import employee profile documents from named employee folders.

The importer is deliberately independent from Flask so it can target a
production SQLite database and its storage directory without booting a web
server.  It is idempotent for the same employee, filename and attachment type.

Example:

    python tools/import_employee_profile_documents.py \
        --source-dir C:\\path\\to\\emps_profile \
        --database C:\\path\\to\\instance\\workflow.db \
        --storage-root C:\\path\\to\\application \
        --uploaded-by-id 5
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import mimetypes
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import unicodedata
import uuid


PERSONAL_CLASSIFICATION = "PERSONAL_DATA"
WORK_CLASSIFICATION = "WORK_DATA"

TYPE_SPECS = {
    "GEN": ("عام", "General", 0),
    "ID_DOC": ("هوية", "ID Document", 10),
    "CERT": ("شهادة", "Certificate", 20),
    "CONTRACT": ("عقد", "Contract", 30),
    "PLACEMENT": ("تعيين", "Appointment", 35),
    "PROM": ("ترقية", "Promotion", 36),
    "BIRTH_CER": ("شهادة ميلاد ابن/ابنة", "Birth Certificate", 37),
    "PASS": ("جواز سفر", "Passport", 38),
    "PIX": ("صورة شخصية", "Personal Photo", 39),
    "Taw": ("شهادة توجيهي", "High School Certificate", 40),
    "BACH": ("شهادة بكالوريوس", "Bachelor Certificate", 41),
    "MAST": ("شهادة ماجستير", "Master Certificate", 42),
    "DOC": ("شهادة دكتوراه", "Doctorate Certificate", 43),
    "CV": ("السيرة الذاتية", "Curriculum Vitae", 44),
    "ASSIGNMENT": ("قرار/تكليف عمل", "Work Decision/Assignment", 45),
    "ADJ": ("تعديل مؤهل", "Qualification Update", 46),
    "TRAIN": ("دورات تدريبية", "Training", 47),
    "EXP": ("شهادة خبرة", "Experience Certificate", 48),
    "MAR": ("عقد زواج", "Marriage Contract", 49),
    "CON": ("قرار تثبيت", "Confirmation Decision", 50),
    "EVAL": ("نموذج تقييم", "Evaluation", 51),
    "EMPLOYEE_CARD_FRONT": (
        "بطاقة الموظف - الوجه الأمامي",
        "Employee Card - Front",
        5,
    ),
    "EMPLOYEE_CARD_BACK": (
        "بطاقة الموظف - الوجه الخلفي",
        "Employee Card - Back",
        6,
    ),
}

PERSONAL_TYPES = {"ID_DOC", "BIRTH_CER", "PASS", "PIX", "MAR"}


def _normalise(value: str) -> str:
    """Normalise Arabic names enough for safe folder-to-user matching."""
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[\u064B-\u065F\u0670]", "", value)
    value = value.translate(
        str.maketrans(
            {
                "أ": "ا",
                "إ": "ا",
                "آ": "ا",
                "ى": "ي",
                "ة": "ه",
                "ؤ": "و",
                "ئ": "ي",
                "ـ": "",
            }
        )
    )
    value = value.replace("'", " ").replace('"', " ")
    value = re.sub(r"[^\w\u0600-\u06ff]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _tokens(value: str) -> set[str]:
    result: set[str] = set()
    for token in _normalise(value).split():
        if token == "ملف":
            continue
        result.add(token)
        if token.startswith("ال") and len(token) > 3:
            result.add(token[2:])
        if token.startswith("ابو") and len(token) > 3:
            result.update(("ابو", token[3:]))
        if token.startswith("ابن") and len(token) > 3:
            result.update(("ابن", "بن", token[3:]))
        if token.startswith("بن") and len(token) > 2:
            result.update(("بن", "ابن", token[2:]))
    return {token for token in result if token}


def _token_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    left = left[2:] if left.startswith("ال") and len(left) > 3 else left
    right = right[2:] if right.startswith("ال") and len(right) > 3 else right
    if left == right:
        return True
    if {left, right} <= {"بن", "ابن"}:
        return True
    # Some names store أبو/ابن as one token (e.g. ابوهجرس).  Only accept
    # substring matches for meaningful tokens to avoid matching short words.
    return len(left) >= 3 and len(right) >= 3 and (left in right or right in left)


def _resolve_employee(folder_name: str, users: list[sqlite3.Row]) -> sqlite3.Row:
    folder_tokens = _tokens(folder_name)
    candidates = []
    for user in users:
        user_tokens = _tokens(
            " ".join(
                value
                for value in (user["name"], user["full_name_quad"])
                if value
            )
        )
        if folder_tokens and all(
            any(_token_compatible(folder_token, user_token) for user_token in user_tokens)
            for folder_token in folder_tokens
        ):
            candidates.append((len(user_tokens), user))

    if not candidates:
        raise ValueError(f"لم تتم مطابقة مجلد الموظف: {folder_name}")
    candidates.sort(key=lambda item: (item[0], int(item[1]["id"])))
    best_size = candidates[0][0]
    best = [row for size, row in candidates if size == best_size]
    if len(best) != 1:
        names = ", ".join(
            f"{row['id']}:{row['name'] or row['full_name_quad'] or row['email']}"
            for row in best
        )
        raise ValueError(f"مطابقة مجلد الموظف غير حاسمة: {folder_name} ({names})")
    return best[0]


def classify_document(filename: str) -> tuple[str, str]:
    """Return ``(attachment type code, classification code)`` from a filename."""
    name = _normalise(Path(filename).stem)

    if "وجه امام" in name or "الوجه امام" in name or "front" in name:
        return "EMPLOYEE_CARD_FRONT", PERSONAL_CLASSIFICATION
    if "وجه خلف" in name or "الوجه خلف" in name or "back" in name:
        return "EMPLOYEE_CARD_BACK", PERSONAL_CLASSIFICATION

    if "ترقي" in name or "تقيه" in name:
        return "PROM", WORK_CLASSIFICATION
    if "تقييم" in name:
        return "EVAL", WORK_CLASSIFICATION
    if "هوية" in name or "هويه" in name:
        return "ID_DOC", PERSONAL_CLASSIFICATION
    if "ميلاد" in name:
        return "BIRTH_CER", PERSONAL_CLASSIFICATION
    if "عقد زواج" in name or "عقد الزواج" in name or "الزواج" in name:
        return "MAR", PERSONAL_CLASSIFICATION
    if "قرار تعيين" in name or "قرار تعين" in name or "التعيين" in name or "استلام عمل" in name:
        return "PLACEMENT", WORK_CLASSIFICATION
    if "تعديل المؤهل" in name:
        return "ADJ", WORK_CLASSIFICATION
    if "خبره" in name:
        return "EXP", WORK_CLASSIFICATION
    if (
        "قرار" in name
        or "تكليف" in name
        or "مهمه عمل" in name
        or "تسكين" in name
        or "نقل" in name
    ):
        return "ASSIGNMENT", WORK_CLASSIFICATION
    if (
        "دوره" in name
        or "تدريب" in name
        or "التحاق" in name
        or "برنامج" in name
        or "مشاركه" in name
    ):
        return "TRAIN", WORK_CLASSIFICATION

    education_keywords = (
        ("دكتور", "DOC"),
        ("ماجستير", "MAST"),
        ("ماستر", "MAST"),
        ("بكالوريوس", "BACH"),
        ("كلية", "BACH"),
        ("توجيهي", "Taw"),
        ("ثانويه", "Taw"),
        ("مدرسيه", "Taw"),
    )
    education_matches = [code for keyword, code in education_keywords if keyword in name]
    if "شهادات" in name and len(set(education_matches)) > 1:
        return "CERT", WORK_CLASSIFICATION
    if education_matches:
        return education_matches[0], WORK_CLASSIFICATION
    if "عقد" in name:
        return "CONTRACT", WORK_CLASSIFICATION
    if "شهاد" in name:
        return "CERT", WORK_CLASSIFICATION
    return "GEN", WORK_CLASSIFICATION


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _ensure_schema(connection: sqlite3.Connection) -> None:
    attachment_columns = _table_columns(connection, "employee_attachment")
    if "attachment_classification_lookup_id" not in attachment_columns:
        connection.execute(
            "ALTER TABLE employee_attachment ADD COLUMN attachment_classification_lookup_id INTEGER"
        )
    lookup_columns = _table_columns(connection, "hr_lookup_item")
    if "attachment_classification_lookup_id" not in lookup_columns:
        connection.execute(
            "ALTER TABLE hr_lookup_item ADD COLUMN attachment_classification_lookup_id INTEGER"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_employee_attachment_classification_lookup_id "
        "ON employee_attachment (attachment_classification_lookup_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_hr_lookup_item_attachment_classification_lookup_id "
        "ON hr_lookup_item (attachment_classification_lookup_id)"
    )


def _ensure_lookup(
    connection: sqlite3.Connection,
    *,
    category: str,
    code: str,
    name_ar: str,
    name_en: str,
    sort_order: int,
    classification_id: int | None = None,
) -> int:
    row = connection.execute(
        "SELECT id, attachment_classification_lookup_id FROM hr_lookup_item "
        "WHERE category = ? AND code = ?",
        (category, code),
    ).fetchone()
    now = datetime.utcnow().isoformat(sep=" ")
    if row:
        if (
            category == "ATTACH_TYPE"
            and classification_id
            and row[1] is None
        ):
            connection.execute(
                "UPDATE hr_lookup_item SET attachment_classification_lookup_id = ?, updated_at = ? "
                "WHERE id = ?",
                (classification_id, now, row[0]),
            )
        return int(row[0])

    connection.execute(
        "INSERT INTO hr_lookup_item "
        "(category, code, name_ar, name_en, sort_order, is_active, created_at, updated_at, "
        "attachment_classification_lookup_id) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (
            category,
            code,
            name_ar,
            name_en,
            sort_order,
            now,
            now,
            classification_id,
        ),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _ensure_lookups(connection: sqlite3.Connection) -> dict[str, int]:
    personal_id = _ensure_lookup(
        connection,
        category="ATTACH_CLASSIFICATION",
        code=PERSONAL_CLASSIFICATION,
        name_ar="بيانات شخصية",
        name_en="Personal Data",
        sort_order=10,
    )
    work_id = _ensure_lookup(
        connection,
        category="ATTACH_CLASSIFICATION",
        code=WORK_CLASSIFICATION,
        name_ar="بيانات العمل",
        name_en="Work Data",
        sort_order=20,
    )
    ids = {
        PERSONAL_CLASSIFICATION: personal_id,
        WORK_CLASSIFICATION: work_id,
    }
    for code, (name_ar, name_en, sort_order) in TYPE_SPECS.items():
        default_classification = personal_id if code in PERSONAL_TYPES or code.startswith("EMPLOYEE_CARD_") else work_id
        ids[code] = _ensure_lookup(
            connection,
            category="ATTACH_TYPE",
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            sort_order=sort_order,
            classification_id=default_classification,
        )
    return ids


def _copy_archive(
    connection: sqlite3.Connection,
    *,
    source_path: Path,
    archive_dir: Path | None,
    user_id: int,
    original_name: str,
    now: str,
) -> int | None:
    if archive_dir is None or "archived_file" not in {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }:
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"employee_{user_id}_{uuid.uuid4().hex}{source_path.suffix.lower()}"
    archive_path = archive_dir / stored_name
    shutil.copy2(source_path, archive_path)
    connection.execute(
        "INSERT INTO archived_file "
        "(original_name, stored_name, description, upload_date, file_path, mime_type, file_size, "
        "visibility, is_final_deleted, owner_id, is_deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'owner', 0, ?, 0)",
        (
            original_name,
            stored_name,
            "Employee file attachment",
            now,
            str(archive_path),
            mimetypes.guess_type(original_name)[0],
            archive_path.stat().st_size,
            user_id,
        ),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _source_employee_dir(source_dir: Path) -> Path:
    nested = source_dir / "ملفات الموظفين المسكنة"
    return nested if nested.is_dir() else source_dir


def import_documents(
    *,
    source_dir: Path,
    database: Path,
    storage_root: Path,
    uploaded_by_id: int | None,
    dry_run: bool,
    report_path: Path | None,
) -> dict:
    source_dir = _source_employee_dir(source_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"مجلد المصدر غير موجود: {source_dir}")
    if not database.is_file():
        raise FileNotFoundError(f"قاعدة البيانات غير موجودة: {database}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    copied_files: list[Path] = []
    report = {
        "source_dir": str(source_dir),
        "database": str(database),
        "storage_root": str(storage_root),
        "dry_run": dry_run,
        "employees": [],
        "imported": 0,
        "skipped": 0,
        "errors": [],
        "types": Counter(),
        "classifications": Counter(),
    }

    try:
        users = connection.execute(
            "SELECT u.id, u.name, u.email, ef.full_name_quad "
            "FROM users u LEFT JOIN employee_file ef ON ef.user_id = u.id"
        ).fetchall()
        if not dry_run:
            _ensure_schema(connection)
            lookup_ids = _ensure_lookups(connection)
            connection.commit()
        else:
            lookup_ids = {}

        if uploaded_by_id is not None:
            exists = connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (uploaded_by_id,)
            ).fetchone()
            if not exists:
                raise ValueError(f"uploaded-by-id غير موجود: {uploaded_by_id}")

        employee_dirs = sorted(path for path in source_dir.iterdir() if path.is_dir())
        if not dry_run:
            connection.execute("BEGIN")

        for employee_dir in employee_dirs:
            user = _resolve_employee(employee_dir.name, users)
            user_id = int(user["id"])
            employee_report = {
                "folder": employee_dir.name,
                "user_id": user_id,
                "user_name": user["name"] or user["full_name_quad"] or user["email"],
                "files": [],
            }
            for source_path in sorted(path for path in employee_dir.iterdir() if path.is_file()):
                type_code, classification_code = classify_document(source_path.name)
                file_report = {
                    "source_name": source_path.name,
                    "type": type_code,
                    "classification": classification_code,
                    "status": "planned" if dry_run else "pending",
                }
                employee_report["files"].append(file_report)
                report["types"][type_code] += 1
                report["classifications"][classification_code] += 1
                if dry_run:
                    continue

                existing = connection.execute(
                    "SELECT id, stored_name FROM employee_attachment "
                    "WHERE user_id = ? AND original_name = ? AND attachment_type_lookup_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (user_id, source_path.name, lookup_ids[type_code]),
                ).fetchone()
                if existing:
                    report["skipped"] += 1
                    file_report["status"] = "skipped_existing"
                    continue

                destination_dir = storage_root / "instance" / "uploads" / "employees" / str(user_id)
                destination_dir.mkdir(parents=True, exist_ok=True)
                stored_name = f"{uuid.uuid4().hex}{source_path.suffix.lower()}"
                destination = destination_dir / stored_name
                shutil.copy2(source_path, destination)
                copied_files.append(destination)
                now = datetime.utcnow().isoformat(sep=" ")
                archive_id = _copy_archive(
                    connection,
                    source_path=destination,
                    archive_dir=storage_root / "storage" / "archive",
                    user_id=user_id,
                    original_name=source_path.name,
                    now=now,
                )
                connection.execute(
                    "INSERT INTO employee_attachment "
                    "(user_id, attachment_type, attachment_type_lookup_id, "
                    "attachment_classification_lookup_id, original_name, stored_name, note, "
                    "payslip_year, payslip_month, is_published, published_at, published_by_id, "
                    "uploaded_at, uploaded_by_id, archived_file_id) "
                    "VALUES (?, 'OTHER', ?, ?, ?, ?, ?, NULL, NULL, 1, NULL, NULL, ?, ?, ?)",
                    (
                        user_id,
                        lookup_ids[type_code],
                        lookup_ids[classification_code],
                        source_path.name,
                        stored_name,
                        TYPE_SPECS[type_code][0],
                        now,
                        uploaded_by_id,
                        archive_id,
                    ),
                )
                report["imported"] += 1
                file_report["status"] = "imported"
                file_report["stored_name"] = stored_name

            report["employees"].append(employee_report)

        if not dry_run:
            connection.commit()
    except Exception:
        if not dry_run:
            connection.rollback()
        for path in reversed(copied_files):
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass
        raise
    finally:
        connection.close()

    report["types"] = dict(report["types"])
    report["classifications"] = dict(report["classifications"])
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--uploaded-by-id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    report = import_documents(
        source_dir=args.source_dir,
        database=args.database,
        storage_root=args.storage_root,
        uploaded_by_id=args.uploaded_by_id,
        dry_run=args.dry_run,
        report_path=args.report,
    )
    print(json.dumps({
        "employees": len(report["employees"]),
        "imported": report["imported"],
        "skipped": report["skipped"],
        "types": report["types"],
        "classifications": report["classifications"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
