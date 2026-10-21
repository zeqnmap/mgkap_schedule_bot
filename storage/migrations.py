"""Explicit SQLite schema migrations for the Telegram bot."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CURRENT_REVISION = "0003_pinned_student_deliveries"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    return db


def current_revision(path: Path) -> str | None:
    if not path.exists():
        return None
    with connect(path) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone():
            return None
        row = db.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None


def _upgrade_schema(path: Path) -> None:
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS subscribers (
                telegram_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL CHECK(role IN ('student','teacher','applicant')),
                username TEXT, full_name TEXT NOT NULL DEFAULT '', student_group INTEGER,
                student_schedule_mode TEXT NOT NULL DEFAULT 'group' CHECK(student_schedule_mode IN ('group','full')),
                subscribed_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_subscribers_role ON subscribers(role);
            CREATE TABLE IF NOT EXISTS schedule_groups (
                number INTEGER PRIMARY KEY, is_active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_schedules (
                group_number INTEGER PRIMARY KEY, filename TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
                content_hash TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS latest_schedules (
                audience TEXT PRIMARY KEY, filename TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
                content_hash TEXT NOT NULL, telegram_file_id TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broadcast_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, audience TEXT NOT NULL,
                kind TEXT NOT NULL, source_chat_id INTEGER, source_message_id INTEGER,
                path TEXT, filename TEXT, caption TEXT NOT NULL DEFAULT '', telegram_file_id TEXT,
                created_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS publication_versions (
                id TEXT PRIMARY KEY, audience TEXT NOT NULL, caption TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publication_files (
                publication_id TEXT NOT NULL, audience TEXT NOT NULL,
                group_number INTEGER, filename TEXT NOT NULL, caption TEXT NOT NULL,
                path TEXT NOT NULL, content_hash TEXT NOT NULL,
                PRIMARY KEY(publication_id, audience, group_number)
            );
            CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                job_id INTEGER NOT NULL, telegram_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                student_group INTEGER, student_schedule_mode TEXT,
                PRIMARY KEY(job_id,telegram_id)
            );
            CREATE INDEX IF NOT EXISTS idx_delivery_pending ON broadcast_deliveries(status,job_id);
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(subscribers)")}
        for name, definition in (
            ("full_name", "TEXT NOT NULL DEFAULT ''"),
            ("student_group", "INTEGER"),
            ("student_schedule_mode", "TEXT NOT NULL DEFAULT 'group'"),
            ("subscribed_at", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE subscribers ADD COLUMN {name} {definition}")
        job_columns = {row[1] for row in db.execute("PRAGMA table_info(broadcast_jobs)")}
        if "publication_id" not in job_columns:
            db.execute("ALTER TABLE broadcast_jobs ADD COLUMN publication_id TEXT")
        delivery_columns = {row[1] for row in db.execute("PRAGMA table_info(broadcast_deliveries)")}
        for name, definition in (("student_group", "INTEGER"), ("student_schedule_mode", "TEXT")):
            if name not in delivery_columns:
                db.execute(f"ALTER TABLE broadcast_deliveries ADD COLUMN {name} {definition}")
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
            (CURRENT_REVISION,),
        )


def import_legacy(path: Path, target: Path, default_role: str = "") -> int:
    """Import old ``users`` rows once, without running it during bot startup."""
    if not path.is_file() or path.resolve() == target.resolve():
        return 0
    allowed = {"student", "teacher", "applicant"}
    default_role = default_role if default_role in allowed else ""
    with sqlite3.connect(path) as legacy, connect(target) as db:
        columns = {row[1] for row in legacy.execute("PRAGMA table_info(users)")}
        if "user_id" not in columns:
            return 0
        username = "username" if "username" in columns else "''"
        role = "role" if "role" in columns else "NULL"
        rows = legacy.execute(f"SELECT user_id,{username},{role} FROM users").fetchall()
        timestamp = datetime.now(timezone.utc).isoformat()
        imported = 0
        for uid, name, selected_role in rows:
            target_role = selected_role if selected_role in allowed else default_role
            if not target_role:
                continue
            cursor = db.execute(
                "INSERT OR IGNORE INTO subscribers "
                "(telegram_id,role,username,full_name,student_group,subscribed_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, target_role, name, name or "", None, timestamp, timestamp),
            )
            imported += cursor.rowcount
        return imported


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = path.with_name(f"{path.name}.pre-migration-{stamp}.bak")
    with sqlite3.connect(path) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    return destination


def upgrade(path: Path) -> bool:
    if current_revision(path) == CURRENT_REVISION:
        return False
    _upgrade_schema(path)
    return True


def require_current(path: Path) -> None:
    revision = current_revision(path)
    if revision != CURRENT_REVISION:
        found = revision or "none"
        raise RuntimeError(
            f"Database schema revision is {found}; expected {CURRENT_REVISION}. "
            "Run `python -m storage.migrate` before starting the bot."
        )
