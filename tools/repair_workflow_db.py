from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SQLITE_HEADER = b"SQLite format 3\x00"
ZIP_DB_CANDIDATES = ("db/workflow.db", "instance/workflow.db", "workflow.db")

REQUIRED_SCHEMA_COLUMNS = {
    "workflow_request": (
        ("confidentiality", "TEXT NOT NULL DEFAULT 'NORMAL'"),
        ("source_corr_kind", "TEXT"),
        ("source_corr_id", "INTEGER"),
    ),
}


def validate_sqlite(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "file does not exist"
    try:
        if path.stat().st_size < len(SQLITE_HEADER):
            return False, "file is too small to be a SQLite database"
        with path.open("rb") as fh:
            if fh.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                return False, "missing SQLite header"
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        if not result or str(result[0]).lower() != "ok":
            return False, f"quick_check failed: {result[0] if result else 'no result'}"
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def get_missing_required_columns(path: Path) -> list[str]:
    valid, reason = validate_sqlite(path)
    if not valid:
        raise RuntimeError(f"Cannot inspect invalid SQLite database: {reason}")

    conn = sqlite3.connect(path)
    try:
        missing: list[str] = []
        for table, columns in REQUIRED_SCHEMA_COLUMNS.items():
            existing = _table_columns(conn, table)
            if not existing:
                missing.append(f"{table}.* (table missing)")
                continue
            for column, _ctype in columns:
                if column not in existing:
                    missing.append(f"{table}.{column}")
        return missing
    finally:
        conn.close()


def upgrade_required_schema(path: Path) -> list[str]:
    valid, reason = validate_sqlite(path)
    if not valid:
        raise RuntimeError(f"Cannot upgrade invalid SQLite database: {reason}")

    conn = sqlite3.connect(path, timeout=30)
    applied: list[str] = []
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")

        for table, columns in REQUIRED_SCHEMA_COLUMNS.items():
            existing = _table_columns(conn, table)
            if not existing:
                raise RuntimeError(f"Required table is missing: {table}")

            for column, ctype in columns:
                if column in existing:
                    continue
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ctype}')
                applied.append(f"{table}.{column}")
                existing.add(column)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    missing = get_missing_required_columns(path)
    if missing:
        raise RuntimeError(
            "Schema upgrade completed but required columns are still missing: "
            + ", ".join(missing)
        )
    return applied


def _backup_sort_key(path: Path) -> tuple[float, str]:
    try:
        return path.stat().st_mtime, path.name
    except OSError:
        return 0.0, path.name


def iter_backup_archives(backups_dir: Path):
    if not backups_dir.is_dir():
        return
    items = [p for p in backups_dir.iterdir() if p.is_file() and p.suffix.lower() == ".zip"]
    for path in sorted(items, key=_backup_sort_key, reverse=True):
        yield path


def extract_valid_db_from_zip(zip_path: Path, work_dir: Path) -> tuple[Path | None, str]:
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            names = {name.replace("\\", "/"): name for name in archive.namelist()}
            member = next((names[name] for name in ZIP_DB_CANDIDATES if name in names), None)
            if not member:
                return None, "no workflow.db found in archive"
            out = work_dir / f"candidate_{abs(hash(str(zip_path)))}.db"
            with archive.open(member, "r") as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    except Exception as exc:
        return None, f"cannot read archive: {type(exc).__name__}: {exc}"

    valid, reason = validate_sqlite(out)
    if not valid:
        try:
            out.unlink()
        except OSError:
            pass
        return None, reason
    return out, "ok"


def find_latest_valid_backup(backups_dir: Path, work_dir: Path) -> tuple[Path | None, Path | None, list[str]]:
    notes: list[str] = []
    for archive in iter_backup_archives(backups_dir) or ():
        candidate, reason = extract_valid_db_from_zip(archive, work_dir)
        if candidate:
            return archive, candidate, notes
        notes.append(f"{archive.name}: {reason}")
    return None, None, notes


def _preserve_runtime_file(path: Path, suffix: str) -> Path | None:
    if not path.exists():
        return None
    preserved = path.with_name(path.name + suffix)
    shutil.move(str(path), str(preserved))
    return preserved


def restore_database(db_path: Path, candidate: Path, source_label: str) -> list[Path]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    valid, reason = validate_sqlite(candidate)
    if not valid:
        raise RuntimeError(f"Refusing to restore invalid candidate: {reason}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = f".invalid_{stamp}"
    preserved: list[Path] = []

    for runtime_path in (
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
    ):
        saved = _preserve_runtime_file(runtime_path, suffix)
        if saved:
            preserved.append(saved)

    staged = db_path.with_name(db_path.name + f".restore_{stamp}.tmp")
    shutil.copy2(candidate, staged)
    staged_valid, staged_reason = validate_sqlite(staged)
    if not staged_valid:
        staged.unlink(missing_ok=True)
        raise RuntimeError(f"Staged database failed validation: {staged_reason}")

    os.replace(staged, db_path)
    print(f"Restored {db_path} from {source_label}")
    return preserved


def build_parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Validate workflow.db, upgrade required runtime schema, and safely restore "
            "the latest valid SQLite backup when needed."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=project_root / "instance" / "workflow.db",
        help="Path to workflow.db",
    )
    parser.add_argument(
        "--backups",
        type=Path,
        default=project_root / "instance" / "backups",
        help="Directory containing workflow backup ZIP files",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="Use one specific backup ZIP instead of scanning the backups directory",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply the required schema upgrade, and replace an invalid DB from backup "
            "when recovery is needed. Without this flag the command is read-only."
        ),
    )
    return parser


def _print_schema_status(db_path: Path) -> tuple[bool, list[str]]:
    try:
        missing = get_missing_required_columns(db_path)
    except Exception as exc:
        print(f"SCHEMA CHECK FAILED: {type(exc).__name__}: {exc}")
        return False, []

    if not missing:
        print("SCHEMA OK: all required workflow_request columns are present.")
        return True, []

    print("SCHEMA OUTDATED: required columns are missing:")
    for item in missing:
        print(f"  - {item}")
    return False, missing


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = args.db.resolve()
    backups_dir = args.backups.resolve()

    current_valid, current_reason = validate_sqlite(db_path)
    if current_valid:
        print(f"OK: {db_path} is a valid SQLite database.")
        schema_ok, missing = _print_schema_status(db_path)
        if schema_ok:
            return 0

        if not args.apply:
            print("Dry run only. Stop the Workflow server, then re-run with --apply.")
            return 3

        try:
            applied = upgrade_required_schema(db_path)
        except Exception as exc:
            print(f"SCHEMA UPGRADE FAILED: {type(exc).__name__}: {exc}")
            print("Make sure Flask/Waitress/DB Browser/PyCharm is not holding workflow.db.")
            return 5

        for item in applied:
            print(f"Added column: {item}")
        print("SUCCESS: database schema upgraded without deleting existing data.")
        return 0

    print(f"INVALID: {db_path}: {current_reason}")

    with tempfile.TemporaryDirectory(prefix="workflow_db_repair_") as tmp:
        work_dir = Path(tmp)
        if args.backup:
            archive = args.backup.resolve()
            candidate, reason = extract_valid_db_from_zip(archive, work_dir)
            if not candidate:
                print(f"Backup is not usable: {archive}: {reason}")
                return 2
            selected = archive
        else:
            selected, candidate, notes = find_latest_valid_backup(backups_dir, work_dir)
            if not candidate or not selected:
                print(f"No valid workflow.db backup was found in {backups_dir}.")
                for note in notes[:10]:
                    print(f"  - {note}")
                return 2

        print(f"RECOVERY CANDIDATE: {selected}")
        if not args.apply:
            print("Dry run only. Re-run with --apply after stopping the Workflow server.")
            return 3

        preserved = restore_database(db_path, candidate, str(selected))
        for path in preserved:
            print(f"Preserved previous runtime file: {path}")

    final_valid, final_reason = validate_sqlite(db_path)
    if not final_valid:
        print(f"Restore completed but validation failed: {final_reason}")
        return 4

    try:
        applied = upgrade_required_schema(db_path)
    except Exception as exc:
        print(f"Restore succeeded, but schema upgrade failed: {type(exc).__name__}: {exc}")
        return 5

    for item in applied:
        print(f"Added column: {item}")

    final_schema_ok, _missing = _print_schema_status(db_path)
    if not final_schema_ok:
        return 5

    print("SUCCESS: restored database passed PRAGMA quick_check and schema validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
