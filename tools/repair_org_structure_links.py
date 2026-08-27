from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import unicodedata


CANONICAL_SECTION_CODE = "SEC_ICESCO_REL"
CANONICAL_PARENT_CODE = "DEP_ALECSO"
APPROVED_STRUCTURE_VERSION = "2023-05-08:v2"


@dataclass(frozen=True)
class ParentRepair:
    node_id: int
    node_code: str
    node_name: str
    old_parent_id: int | None
    new_parent_id: int
    source: str


def _normalize_arabic(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", (value or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.translate(str.maketrans({
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ؤ": "و",
        "ئ": "ي",
        "ة": "ه",
    }))
    return "".join(ch for ch in text if ch.isalnum())


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def validate_database(conn: sqlite3.Connection) -> None:
    required = {
        "org_nodes",
        "org_node_types",
        "sections",
        "departments",
        "directorates",
        "units",
        "system_setting",
    }
    missing = sorted(required - _table_names(conn))
    if missing:
        raise RuntimeError("Missing required tables: " + ", ".join(missing))
    quick_check = conn.execute("PRAGMA quick_check").fetchone()
    if not quick_check or str(quick_check[0]).lower() != "ok":
        raise RuntimeError(
            f"SQLite quick_check failed: {quick_check[0] if quick_check else 'no result'}"
        )


def _active_node_by_code(conn: sqlite3.Connection, code: str):
    return conn.execute(
        "SELECT id, parent_id, code, name_ar FROM org_nodes "
        "WHERE code = ? AND is_active = 1 ORDER BY id ASC LIMIT 1",
        (code,),
    ).fetchone()


def _legacy_parent_node_id(
    conn: sqlite3.Connection,
    parent_type: str,
    parent_legacy_id: int,
) -> int | None:
    row = conn.execute(
        "SELECT id FROM org_nodes "
        "WHERE legacy_type = ? AND legacy_id = ? AND is_active = 1 "
        "ORDER BY id ASC LIMIT 1",
        (parent_type, parent_legacy_id),
    ).fetchone()
    if row:
        return int(row[0])

    table_by_type = {
        "DEPARTMENT": "departments",
        "DIRECTORATE": "directorates",
        "UNIT": "units",
    }
    table_name = table_by_type.get(parent_type)
    if not table_name:
        return None
    source = conn.execute(
        f'SELECT name_ar FROM "{table_name}" WHERE id = ?',
        (parent_legacy_id,),
    ).fetchone()
    wanted_name = _normalize_arabic(source[0] if source else None)
    if not wanted_name:
        return None

    candidates = conn.execute(
        "SELECT n.id, n.name_ar FROM org_nodes n "
        "JOIN org_node_types t ON t.id = n.type_id "
        "WHERE t.code = ? AND n.is_active = 1 ORDER BY n.id ASC",
        (parent_type,),
    ).fetchall()
    matched_ids = [
        int(candidate[0])
        for candidate in candidates
        if _normalize_arabic(candidate[1]) == wanted_name
    ]
    return matched_ids[0] if len(matched_ids) == 1 else None


def plan_repairs(conn: sqlite3.Connection) -> list[ParentRepair]:
    validate_database(conn)
    repairs: list[ParentRepair] = []

    canonical = _active_node_by_code(conn, CANONICAL_SECTION_CODE)
    canonical_parent = _active_node_by_code(conn, CANONICAL_PARENT_CODE)
    if canonical and canonical_parent and canonical[1] != canonical_parent[0]:
        repairs.append(ParentRepair(
            node_id=int(canonical[0]),
            node_code=str(canonical[2] or ""),
            node_name=str(canonical[3] or ""),
            old_parent_id=int(canonical[1]) if canonical[1] is not None else None,
            new_parent_id=int(canonical_parent[0]),
            source="approved",
        ))

    section_nodes = conn.execute(
        "SELECT n.id, n.parent_id, n.code, n.name_ar, "
        "s.department_id, s.directorate_id, s.unit_id "
        "FROM org_nodes n "
        "JOIN sections s ON s.id = n.legacy_id "
        "WHERE n.legacy_type = 'SECTION' AND n.is_active = 1 "
        "ORDER BY n.id ASC"
    ).fetchall()
    for row in section_nodes:
        if row[4] is not None:
            parent_type, parent_legacy_id = "DEPARTMENT", int(row[4])
        elif row[6] is not None:
            parent_type, parent_legacy_id = "UNIT", int(row[6])
        elif row[5] is not None:
            parent_type, parent_legacy_id = "DIRECTORATE", int(row[5])
        else:
            continue

        parent_node_id = _legacy_parent_node_id(
            conn,
            parent_type,
            parent_legacy_id,
        )
        if parent_node_id is None or row[1] == parent_node_id:
            continue
        repairs.append(ParentRepair(
            node_id=int(row[0]),
            node_code=str(row[2] or ""),
            node_name=str(row[3] or ""),
            old_parent_id=int(row[1]) if row[1] is not None else None,
            new_parent_id=parent_node_id,
            source="legacy-section",
        ))

    return repairs


def _backup_database(conn: sqlite3.Connection, db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(
        f"{db_path.stem}_before_org_link_repair_{stamp}{db_path.suffix}"
    )
    backup_conn = sqlite3.connect(backup_path)
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()
    return backup_path


def apply_repairs(db_path: Path) -> tuple[list[ParentRepair], Path | None]:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        repairs = plan_repairs(conn)
        if not repairs:
            return [], None

        backup_path = _backup_database(conn, db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for repair in repairs:
                cursor = conn.execute(
                    "UPDATE org_nodes SET parent_id = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND is_active = 1",
                    (repair.new_parent_id, repair.node_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Expected to update OrgNode {repair.node_id}, updated {cursor.rowcount}"
                    )

            setting = conn.execute(
                "UPDATE system_setting SET value = ? "
                "WHERE key = 'ORG_APPROVED_STRUCTURE_VERSION'",
                (APPROVED_STRUCTURE_VERSION,),
            )
            if setting.rowcount == 0:
                conn.execute(
                    "INSERT INTO system_setting (key, value) VALUES (?, ?)",
                    ("ORG_APPROVED_STRUCTURE_VERSION", APPROVED_STRUCTURE_VERSION),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        remaining = plan_repairs(conn)
        if remaining:
            raise RuntimeError(
                "Repair committed but validation still found pending links: "
                + ", ".join(str(item.node_id) for item in remaining)
            )
        return repairs, backup_path
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Repair approved and legacy OrgNode parent links safely."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=project_root / "instance" / "workflow.db",
        help="Path to workflow.db",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create a backup and apply the planned parent-link repairs.",
    )
    return parser


def _print_repairs(repairs: list[ParentRepair]) -> None:
    if not repairs:
        print("No organizational parent-link repairs are needed.")
        return
    for repair in repairs:
        print(
            f"{repair.source}: node={repair.node_id} "
            f"code={repair.node_code or '-'} name={repair.node_name!r} "
            f"parent {repair.old_parent_id} -> {repair.new_parent_id}"
        )


def main() -> int:
    args = build_parser().parse_args()
    db_path = args.db.resolve()
    if not db_path.is_file():
        raise SystemExit(f"Database file does not exist: {db_path}")

    if not args.apply:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            repairs = plan_repairs(conn)
        finally:
            conn.close()
        _print_repairs(repairs)
        if repairs:
            print("Dry run only. Re-run with --apply to create a backup and apply them.")
        return 0

    repairs, backup_path = apply_repairs(db_path)
    _print_repairs(repairs)
    if backup_path:
        print(f"Backup created: {backup_path}")
        print(f"Applied {len(repairs)} organizational parent-link repair(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
