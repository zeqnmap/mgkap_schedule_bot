import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("BOT_SHARED_SECRET", "test-secret")
os.environ.setdefault("BOT_DB_PATH", "/tmp/mgkap-import-test.db")

import bot


class RepositoryTests(unittest.TestCase):
    def test_old_database_schema_is_upgraded_without_losing_users(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE subscribers(telegram_id INTEGER PRIMARY KEY, role TEXT NOT NULL, username TEXT, full_name TEXT, subscribed_at TEXT NOT NULL)")
                db.execute("INSERT INTO subscribers VALUES(1,'student','old','Old','2026-09-01')")
            repository = bot.Repository(path)
            repository.set_role(SimpleNamespace(id=2, username="new", full_name="New"), "teacher")
            self.assertEqual(repository.stats(), {"student": 1, "teacher": 1, "applicant": 0, "total": 2})

    def test_legacy_import_keeps_only_selected_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = Path(directory) / "legacy.db"
            with sqlite3.connect(legacy_path) as db:
                db.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, role TEXT)")
                db.executemany("INSERT INTO users VALUES(?,?,?)", [(1, "one", "student"), (2, "two", "teacher"), (3, "three", None)])
            repository = bot.Repository(Path(directory) / "bot.db")
            repository.migrate_legacy(str(legacy_path))
            repository.migrate_legacy(str(legacy_path))
            self.assertEqual(repository.stats()["total"], 2)

    def test_legacy_import_can_keep_users_without_role_as_applicants(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = Path(directory) / "legacy.db"
            with sqlite3.connect(legacy_path) as db:
                db.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, role TEXT)")
                db.executemany("INSERT INTO users VALUES(?,?,?)", [(1, "one", "student"), (2, "two", None)])
            repository = bot.Repository(Path(directory) / "bot.db")
            repository.migrate_legacy(str(legacy_path), default_role="applicant")
            self.assertEqual(repository.stats(), {"student": 1, "teacher": 0, "applicant": 1, "total": 2})

    def test_completed_broadcast_history_is_pruned_after_retention_period(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = bot.Repository(Path(directory) / "bot.db")
            with repository.connect() as db:
                db.execute(
                    "INSERT INTO broadcast_jobs(audience,kind,created_at,completed_at) VALUES(?,?,?,?)",
                    ("student", "pdf", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
                )
                db.execute("INSERT INTO broadcast_deliveries(job_id,telegram_id,status) VALUES(1,1,'delivered')")
            self.assertEqual(repository.cleanup_completed_broadcasts(7), 1)
            with repository.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM broadcast_jobs").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM broadcast_deliveries").fetchone()[0], 0)

    def test_subscriber_can_be_found_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = bot.Repository(Path(directory) / "bot.db")
            repository.set_role(SimpleNamespace(id=42, username="user", full_name="User"), "student")
            self.assertEqual(repository.subscriber(42)["role"], "student")
            self.assertTrue(repository.remove(42))
            self.assertIsNone(repository.subscriber(42))


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_targets_only_selected_role_and_reuses_file_id(self):
        with tempfile.TemporaryDirectory() as directory:
            old_repository = bot.repository
            old_schedule_dir = bot.SCHEDULE_DIR
            old_rate = bot.SEND_RATE
            bot.repository = bot.Repository(Path(directory) / "bot.db")
            bot.SCHEDULE_DIR = Path(directory) / "schedules"
            bot.SEND_RATE = 1000
            try:
                for uid, role in ((1, "student"), (2, "student"), (3, "teacher")):
                    bot.repository.set_role(SimpleNamespace(id=uid, username=None, full_name="Test"), role)
                job_id = bot.repository.enqueue_pdf("student", "Расписание.pdf", "на 1 октября", b"%PDF-1.4\n")
                sent = []

                async def fake_send(uid, document, caption):
                    sent.append((uid, document, caption))
                    return SimpleNamespace(document=SimpleNamespace(file_id="telegram-file-id"))

                original_send = bot.bot.send_document
                bot.bot.send_document = AsyncMock(side_effect=fake_send)
                try:
                    worker = asyncio.create_task(bot.broadcast_worker())
                    for _ in range(100):
                        if bot.repository.job_stats(job_id)["delivered"] == 2:
                            break
                        await asyncio.sleep(.02)
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                finally:
                    bot.bot.send_document = original_send
                self.assertEqual([item[0] for item in sent], [1, 2])
                self.assertEqual(sent[1][1], "telegram-file-id")
                self.assertEqual(bot.repository.job_stats(job_id)["delivered"], 2)
                self.assertEqual(bot.repository.job_stats(job_id)["pending"], 0)
            finally:
                bot.repository = old_repository
                bot.SCHEDULE_DIR = old_schedule_dir
                bot.SEND_RATE = old_rate

    async def test_admin_copy_queue_keeps_source_and_role(self):
        with tempfile.TemporaryDirectory() as directory:
            old_repository = bot.repository
            old_rate = bot.SEND_RATE
            bot.repository = bot.Repository(Path(directory) / "bot.db")
            bot.SEND_RATE = 1000
            try:
                for uid, role in ((1, "student"), (2, "teacher")):
                    bot.repository.set_role(SimpleNamespace(id=uid, username=None, full_name="Test"), role)
                source = SimpleNamespace(chat=SimpleNamespace(id=99), message_id=42)
                job_id = bot.repository.enqueue_copy("teacher", source)
                copied = []

                async def fake_copy(**kwargs):
                    copied.append(kwargs)
                    return SimpleNamespace()

                original_copy = bot.bot.copy_message
                bot.bot.copy_message = AsyncMock(side_effect=fake_copy)
                try:
                    worker = asyncio.create_task(bot.broadcast_worker())
                    for _ in range(100):
                        if bot.repository.job_stats(job_id)["delivered"]:
                            break
                        await asyncio.sleep(.02)
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                finally:
                    bot.bot.copy_message = original_copy
                self.assertEqual(copied, [{"chat_id": 2, "from_chat_id": 99, "message_id": 42}])
            finally:
                bot.repository = old_repository
                bot.SEND_RATE = old_rate

    def test_failed_delivery_attempts_are_counted_and_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            old_schedule_dir = bot.SCHEDULE_DIR
            bot.SCHEDULE_DIR = Path(directory) / "schedules"
            repository = bot.Repository(Path(directory) / "bot.db")
            try:
                repository.set_role(SimpleNamespace(id=1, username=None, full_name="Test"), "student")
                job_id = repository.enqueue_pdf("student", "Расписание.pdf", "", b"%PDF-1.4\n")
                self.assertEqual([repository.bump_delivery_attempt(job_id, 1) for _ in range(3)], [1, 2, 3])
                repository.mark_delivery(job_id, 1, "failed")
                self.assertEqual(repository.job_stats(job_id)["failed"], 1)
                self.assertEqual(repository.job_stats(job_id)["pending"], 0)
            finally:
                bot.SCHEDULE_DIR = old_schedule_dir


if __name__ == "__main__":
    unittest.main()
