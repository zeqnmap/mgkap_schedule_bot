"""SQLite repository. Telegram handlers must not contain SQL statements."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from aiogram.types import Message

from config import ROLES, SCHEDULE_DIR as DEFAULT_SCHEDULE_DIR
from .migrations import require_current


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.schedule_dir = DEFAULT_SCHEDULE_DIR
        require_current(path)

    def _active_schedule_dir(self) -> Path:
        # Keep legacy tests and local integrations that override bot.SCHEDULE_DIR working.
        runtime = sys.modules.get("bot")
        return getattr(runtime, "SCHEDULE_DIR", self.schedule_dir) if runtime else self.schedule_dir

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def migrate_legacy(self, path: str, default_role: str = "") -> int:
        if not path or not Path(path).is_file() or Path(path).resolve() == self.path.resolve():
            return 0
        if default_role not in ROLES:
            default_role = ""
        legacy = sqlite3.connect(path)
        try:
            columns = {row[1] for row in legacy.execute("PRAGMA table_info(users)")}
            if "user_id" not in columns:
                return 0
            username = "username" if "username" in columns else "''"
            role = "role" if "role" in columns else "NULL"
            rows = legacy.execute(f"SELECT user_id,{username},{role} FROM users").fetchall()
            with self.connect() as db:
                for uid, name, selected_role in rows:
                    target_role = selected_role if selected_role in ROLES else default_role
                    if target_role:
                        db.execute("INSERT OR IGNORE INTO subscribers (telegram_id,role,username,full_name,student_group,subscribed_at,updated_at) VALUES(?,?,?,?,?,?,?)", (uid, target_role, name, name or "", None, now(), now()))
            return sum(selected_role in ROLES or bool(default_role) for _, _, selected_role in rows)
        finally:
            legacy.close()

    def cleanup_completed_broadcasts(self, retention_days: int) -> int:
        """Delete only finished delivery history older than the retention period."""
        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
        with self.connect() as db:
            completed = db.execute(
                "SELECT id, completed_at FROM broadcast_jobs WHERE completed_at IS NOT NULL"
            ).fetchall()
            expired_ids = [
                int(row["id"])
                for row in completed
                if datetime.fromisoformat(row["completed_at"]).timestamp() < cutoff
            ]
            if not expired_ids:
                return 0
            placeholders = ",".join("?" for _ in expired_ids)
            db.execute(f"DELETE FROM broadcast_deliveries WHERE job_id IN ({placeholders})", expired_ids)
            db.execute(f"DELETE FROM broadcast_jobs WHERE id IN ({placeholders})", expired_ids)
            # SQLite reuses deleted pages internally; VACUUM returns them to the file system.
            db.commit()
            db.execute("VACUUM")
            return len(expired_ids)

    def set_role(self, user: Any, role: str) -> None:
        with self.connect() as db:
            timestamp = now()
            db.execute("""INSERT INTO subscribers (telegram_id,role,username,full_name,student_group,subscribed_at,updated_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role,username=excluded.username,
                full_name=excluded.full_name,updated_at=excluded.updated_at""", (user.id, role, user.username, user.full_name or "", None, timestamp, timestamp))

    def remove(self, uid: int) -> bool:
        with self.connect() as db:
            return db.execute("DELETE FROM subscribers WHERE telegram_id=?", (uid,)).rowcount > 0

    def subscriber(self, uid: int) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM subscribers WHERE telegram_id=?", (uid,)).fetchone()

    def recipients(self, audience: str) -> list[int]:
        with self.connect() as db:
            query = "SELECT telegram_id FROM subscribers" if audience == "all" else "SELECT telegram_id FROM subscribers WHERE role=?"
            rows = db.execute(query, () if audience == "all" else (audience,)).fetchall()
            return [int(row[0]) for row in rows]

    def stats(self) -> dict[str, int]:
        result = {key: 0 for key in ROLES}
        with self.connect() as db:
            for row in db.execute("SELECT role,COUNT(*) count FROM subscribers GROUP BY role"):
                result[row["role"]] = row["count"]
        result["total"] = sum(result.values())
        return result

    def users(self, limit: int = 50) -> tuple[list[sqlite3.Row], int]:
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
            return db.execute("SELECT * FROM subscribers ORDER BY telegram_id LIMIT ?", (limit,)).fetchall(), total

    def setting(self, name: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
            return row[0] if row else ""

    def set_setting(self, name: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (name, value, now()),
            )

    def set_student_group(self, uid: int, group_number: int | None) -> None:
        with self.connect() as db:
            db.execute("UPDATE subscribers SET student_group=?,updated_at=? WHERE telegram_id=?", (group_number, now(), uid))

    def set_student_schedule_mode(self, uid: int, mode: str) -> None:
        if mode not in {"group", "full"}:
            raise ValueError("Unknown student schedule mode")
        with self.connect() as db:
            db.execute("UPDATE subscribers SET student_schedule_mode=?,updated_at=? WHERE telegram_id=?", (mode, now(), uid))

    def groups(self, active_only: bool = False) -> list[sqlite3.Row]:
        with self.connect() as db:
            query = "SELECT * FROM schedule_groups"
            if active_only:
                query += " WHERE is_active=1"
            return db.execute(query + " ORDER BY number").fetchall()

    def group_exists(self, number: int) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM schedule_groups WHERE number=? AND is_active=1", (number,)).fetchone())

    def upsert_group(self, number: int, is_active: bool = True) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO schedule_groups(number,is_active,updated_at) VALUES(?,?,?) ON CONFLICT(number) DO UPDATE SET updated_at=excluded.updated_at", (number, int(is_active), now()))

    def set_group_active(self, number: int, is_active: bool) -> bool:
        with self.connect() as db:
            return db.execute("UPDATE schedule_groups SET is_active=?,updated_at=? WHERE number=?", (int(is_active), now(), number)).rowcount > 0

    def remove_group(self, number: int) -> bool:
        with self.connect() as db:
            return db.execute("UPDATE schedule_groups SET is_active=0,updated_at=? WHERE number=?", (now(), number)).rowcount > 0

    def save_group_schedule(self, group_number: int, filename: str, caption: str, content: bytes) -> None:
        schedule_dir = self._active_schedule_dir()
        schedule_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.() -]", "_", Path(filename).name, flags=re.UNICODE)[:120] or f"Группа_{group_number}.pdf"
        target = schedule_dir / f"group_{group_number}_{safe}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, target)
        with self.connect() as db:
            db.execute("""INSERT INTO group_schedules VALUES(?,?,?,?,?,?)
                ON CONFLICT(group_number) DO UPDATE SET filename=excluded.filename,caption=excluded.caption,
                path=excluded.path,content_hash=excluded.content_hash,updated_at=excluded.updated_at""", (group_number, safe, caption, str(target), hashlib.sha256(content).hexdigest(), now()))

    def group_schedule(self, group_number: int) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM group_schedules WHERE group_number=?", (group_number,)).fetchone()

    def publish_student_package(self, filename: str, caption: str, full_pdf: bytes,
                                group_pdfs: dict[int, tuple[str, bytes]], group_numbers: list[int]) -> tuple[str, int]:
        """Install a complete student publication without touching the active one mid-upload."""
        expected = {int(number) for number in group_numbers}
        if not expected or any(number < 1 for number in expected) or set(group_pdfs) != expected:
            raise ValueError("Publication must include every active group exactly once")
        if not full_pdf.startswith(b"%PDF") or any(not content.startswith(b"%PDF") for _, content in group_pdfs.values()):
            raise ValueError("Publication contains an invalid PDF")
        publication_id = uuid.uuid4().hex
        schedule_dir = self._active_schedule_dir()
        version_dir = schedule_dir / "publications" / publication_id
        version_dir.mkdir(parents=True, exist_ok=False)
        safe_full = re.sub(r"[^\w.() -]", "_", Path(filename).name, flags=re.UNICODE)[:120] or "Расписание.pdf"
        full_path = version_dir / safe_full
        full_path.write_bytes(full_pdf)
        file_rows = [("student", None, safe_full, str(full_path), hashlib.sha256(full_pdf).hexdigest())]
        for number in sorted(expected):
            source_name, content = group_pdfs[number]
            safe = re.sub(r"[^\w.() -]", "_", Path(source_name).name, flags=re.UNICODE)[:120] or f"Группа_{number}.pdf"
            path = version_dir / f"group_{number}_{safe}"
            path.write_bytes(content)
            file_rows.append(("student_group", number, safe, str(path), hashlib.sha256(content).hexdigest()))
        try:
            with self.connect() as db:
                timestamp = now()
                db.execute("INSERT INTO publication_versions(id,audience,caption,created_at) VALUES(?,?,?,?)", (publication_id, "student", caption, timestamp))
                db.executemany("INSERT INTO publication_files(publication_id,audience,group_number,filename,caption,path,content_hash) VALUES(?,?,?,?,?,?,?)", [(publication_id, audience, group_number, file_name, caption, path, digest) for audience, group_number, file_name, path, digest in file_rows])
                db.execute("""INSERT INTO latest_schedules VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(audience) DO UPDATE SET filename=excluded.filename,caption=excluded.caption,path=excluded.path,content_hash=excluded.content_hash,telegram_file_id=NULL,updated_at=excluded.updated_at""", ("student", safe_full, caption, str(full_path), hashlib.sha256(full_pdf).hexdigest(), None, timestamp))
                db.executemany("""INSERT INTO group_schedules VALUES(?,?,?,?,?,?)
                    ON CONFLICT(group_number) DO UPDATE SET filename=excluded.filename,caption=excluded.caption,path=excluded.path,content_hash=excluded.content_hash,updated_at=excluded.updated_at""", [(number, file_name, caption, path, digest, timestamp) for _, number, file_name, path, digest in file_rows if number is not None])
                cursor = db.execute(
                    "INSERT INTO broadcast_jobs(audience,kind,caption,publication_id,created_at) VALUES(?,?,?,?,?)",
                    ("student", "student_group_pdf", caption, publication_id, timestamp),
                )
                job_id = int(cursor.lastrowid)
                recipients = db.execute("""
                    SELECT s.telegram_id,s.student_group,s.student_schedule_mode
                    FROM subscribers s
                    LEFT JOIN schedule_groups g ON g.number=s.student_group AND g.is_active=1
                    WHERE s.role='student' AND (
                        s.student_schedule_mode='full'
                        OR (s.student_schedule_mode='group' AND s.student_group IS NOT NULL AND g.number IS NOT NULL
                            AND EXISTS (
                                SELECT 1 FROM publication_files pf
                                WHERE pf.publication_id=? AND pf.audience='student_group'
                                  AND pf.group_number=s.student_group
                            ))
                    )
                """, (publication_id,)).fetchall()
                db.executemany(
                    "INSERT INTO broadcast_deliveries(job_id,telegram_id,student_group,student_schedule_mode) VALUES(?,?,?,?)",
                    [(job_id, int(row["telegram_id"]), row["student_group"], row["student_schedule_mode"]) for row in recipients],
                )
                if not recipients:
                    db.execute("UPDATE broadcast_jobs SET completed_at=? WHERE id=?", (timestamp, job_id))
        except Exception:
            for path in version_dir.iterdir():
                path.unlink(missing_ok=True)
            version_dir.rmdir()
            raise
        return publication_id, job_id

    def publication_schedule(self, publication_id: str, audience: str, group_number: int | None = None) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT filename,caption,path,content_hash FROM publication_files WHERE publication_id=? AND audience=? AND group_number IS ?", (publication_id, audience, group_number)).fetchone()

    def publish_single_schedule(self, audience: str, filename: str, caption: str, content: bytes) -> int:
        if audience not in {"student", "teacher"} or not content.startswith(b"%PDF"):
            raise ValueError("Invalid audience or PDF")
        publication_id = uuid.uuid4().hex
        schedule_dir = self._active_schedule_dir()
        version_dir = schedule_dir / "publications" / publication_id
        version_dir.mkdir(parents=True, exist_ok=False)
        safe = re.sub(r"[^\w.() -]", "_", Path(filename).name, flags=re.UNICODE)[:120] or "Расписание.pdf"
        path = version_dir / safe
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        try:
            with self.connect() as db:
                timestamp = now()
                db.execute("INSERT INTO publication_versions(id,audience,caption,created_at) VALUES(?,?,?,?)", (publication_id, audience, caption, timestamp))
                db.execute("INSERT INTO publication_files(publication_id,audience,group_number,filename,caption,path,content_hash) VALUES(?,?,?,?,?,?,?)", (publication_id, audience, None, safe, caption, str(path), digest))
                db.execute("""INSERT INTO latest_schedules VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(audience) DO UPDATE SET filename=excluded.filename,caption=excluded.caption,path=excluded.path,content_hash=excluded.content_hash,telegram_file_id=NULL,updated_at=excluded.updated_at""", (audience, safe, caption, str(path), digest, None, timestamp))
                cursor = db.execute("INSERT INTO broadcast_jobs(audience,kind,path,filename,caption,publication_id,created_at) VALUES(?,?,?,?,?,?,?)", (audience, "pdf", str(path), safe, caption, publication_id, timestamp))
                job_id = int(cursor.lastrowid)
                recipients = db.execute("SELECT telegram_id FROM subscribers WHERE role=?", (audience,)).fetchall()
                db.executemany("INSERT INTO broadcast_deliveries(job_id,telegram_id) VALUES(?,?)", [(job_id, int(row[0])) for row in recipients])
                if not recipients:
                    db.execute("UPDATE broadcast_jobs SET completed_at=? WHERE id=?", (timestamp, job_id))
                return job_id
        except Exception:
            for item in version_dir.iterdir():
                item.unlink(missing_ok=True)
            version_dir.rmdir()
            raise

    def latest(self, audience: str) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM latest_schedules WHERE audience=?", (audience,)).fetchone()
            return row or (db.execute("SELECT * FROM latest_schedules WHERE audience='all'").fetchone() if audience == "student" else None)

    def save_file_id(self, audience: str, file_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE latest_schedules SET telegram_file_id=?,updated_at=? WHERE audience=?", (file_id, now(), audience))

    def save_job_file_id(self, job_id: int, file_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE broadcast_jobs SET telegram_file_id=? WHERE id=?", (file_id, job_id))

    def save_schedule(self, audience: str, filename: str, caption: str, content: bytes) -> None:
        schedule_dir = self._active_schedule_dir()
        schedule_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.() -]", "_", Path(filename).name, flags=re.UNICODE)[:120] or "Расписание.pdf"
        target = schedule_dir / f"{audience}_{safe}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, target)
        with self.connect() as db:
            db.execute("""INSERT INTO latest_schedules VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(audience) DO UPDATE SET filename=excluded.filename,caption=excluded.caption,
                path=excluded.path,content_hash=excluded.content_hash,telegram_file_id=NULL,updated_at=excluded.updated_at""", (audience, safe, caption, str(target), hashlib.sha256(content).hexdigest(), None, now()))

    def enqueue_pdf(self, audience: str, filename: str, caption: str, content: bytes) -> int:
        self.save_schedule(audience, filename, caption, content)
        row = self.latest(audience)
        with self.connect() as db:
            cursor = db.execute("INSERT INTO broadcast_jobs(audience,kind,filename,caption,created_at) VALUES(?,?,?,?,?)", (audience, "pdf", row["filename"], caption, now()))
            job_id = cursor.lastrowid
            job_path = self._active_schedule_dir() / f"job_{job_id}.pdf"
            job_path.write_bytes(content)
            db.execute("UPDATE broadcast_jobs SET path=? WHERE id=?", (str(job_path), job_id))
            db.executemany("INSERT INTO broadcast_deliveries(job_id,telegram_id) VALUES(?,?)", [(job_id, uid) for uid in self.recipients(audience)])
            if not db.execute("SELECT 1 FROM broadcast_deliveries WHERE job_id=? LIMIT 1", (job_id,)).fetchone():
                db.execute("UPDATE broadcast_jobs SET completed_at=? WHERE id=?", (now(), job_id))
            return job_id

    def enqueue_student_group_schedules(self, publication_id: str | None = None) -> int:
        """Queue one group-specific PDF for every student with a valid selected group."""
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO broadcast_jobs(audience,kind,caption,publication_id,created_at) VALUES(?,?,?,?,?)",
                ("student", "student_group_pdf", "", publication_id, now()),
            )
            job_id = int(cursor.lastrowid)
            recipients = db.execute("""
                SELECT s.telegram_id,s.student_group,s.student_schedule_mode
                FROM subscribers s
                LEFT JOIN schedule_groups g ON g.number=s.student_group AND g.is_active=1
                LEFT JOIN group_schedules pdf ON pdf.group_number=s.student_group
                WHERE s.role='student' AND (
                    s.student_schedule_mode='full'
                    OR (s.student_schedule_mode='group' AND s.student_group IS NOT NULL AND g.number IS NOT NULL AND pdf.group_number IS NOT NULL)
                )
            """).fetchall()
            db.executemany(
                "INSERT INTO broadcast_deliveries(job_id,telegram_id,student_group,student_schedule_mode) VALUES(?,?,?,?)",
                [(job_id, int(row["telegram_id"]), row["student_group"], row["student_schedule_mode"]) for row in recipients],
            )
            if not recipients:
                db.execute("UPDATE broadcast_jobs SET completed_at=? WHERE id=?", (now(), job_id))
            return job_id

    def enqueue_copy(self, audience: str, source: Message) -> int:
        with self.connect() as db:
            cursor = db.execute("INSERT INTO broadcast_jobs(audience,kind,source_chat_id,source_message_id,created_at) VALUES(?,?,?,?,?)", (audience, "copy", source.chat.id, source.message_id, now()))
            job_id = cursor.lastrowid
            db.executemany("INSERT INTO broadcast_deliveries(job_id,telegram_id) VALUES(?,?)", [(job_id, uid) for uid in self.recipients(audience)])
            if not db.execute("SELECT 1 FROM broadcast_deliveries WHERE job_id=? LIMIT 1", (job_id,)).fetchone():
                db.execute("UPDATE broadcast_jobs SET completed_at=? WHERE id=?", (now(), job_id))
            return job_id

    def next_delivery(self) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("""SELECT d.job_id,d.telegram_id,d.attempts,d.student_group AS delivery_student_group,
                d.student_schedule_mode AS delivery_student_schedule_mode,j.audience,j.kind,j.path,j.filename,j.caption,
                j.source_chat_id,j.source_message_id,j.telegram_file_id,j.publication_id,s.student_group,s.student_schedule_mode FROM broadcast_deliveries d
                JOIN broadcast_jobs j ON j.id=d.job_id
                LEFT JOIN subscribers s ON s.telegram_id=d.telegram_id
                WHERE d.status='pending'
                ORDER BY d.job_id,d.telegram_id LIMIT 1""").fetchone()

    def mark_delivery(self, job_id: int, uid: int, status: str) -> str | None:
        with self.connect() as db:
            db.execute("UPDATE broadcast_deliveries SET status=?,attempts=attempts+1 WHERE job_id=? AND telegram_id=?", (status, job_id, uid))
            remaining = db.execute("SELECT COUNT(*) FROM broadcast_deliveries WHERE job_id=? AND status='pending'", (job_id,)).fetchone()[0]
            if not remaining:
                db.execute("UPDATE broadcast_jobs SET completed_at=? WHERE id=?", (now(), job_id))
                row = db.execute("SELECT path FROM broadcast_jobs WHERE id=?", (job_id,)).fetchone()
                return row[0] if row else None
        return None

    def bump_delivery_attempt(self, job_id: int, uid: int) -> int:
        with self.connect() as db:
            db.execute("UPDATE broadcast_deliveries SET attempts=attempts+1 WHERE job_id=? AND telegram_id=?", (job_id, uid))
            row = db.execute("SELECT attempts FROM broadcast_deliveries WHERE job_id=? AND telegram_id=?", (job_id, uid)).fetchone()
            return int(row[0]) if row else 0

    def job_stats(self, job_id: int) -> dict[str, int]:
        result = {"queued": 0, "delivered": 0, "failed": 0, "removed": 0, "pending": 0}
        with self.connect() as db:
            for status, count in db.execute("SELECT status,COUNT(*) FROM broadcast_deliveries WHERE job_id=? GROUP BY status", (job_id,)):
                result[status] = count
        return result
