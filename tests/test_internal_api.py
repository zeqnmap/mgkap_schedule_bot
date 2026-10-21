from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("BOT_SHARED_SECRET", "test-secret")
os.environ.setdefault("BOT_DB_PATH", "/tmp/mgkap-internal-api-test.db")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.internal import register_internal_api
from config import SHARED_SECRET
from storage.migrations import upgrade
from storage.repository import Repository
import bot


class InternalApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "bot.db"
        upgrade(self.db_path)
        self.repository = Repository(self.db_path)
        self.old_repository = bot.repository
        self.old_schedule_dir = bot.SCHEDULE_DIR
        bot.repository = self.repository
        bot.SCHEDULE_DIR = self.root / "schedules"
        self.app = FastAPI()
        self.app.include_router(register_internal_api(SimpleNamespace(repository=self.repository)))
        self.client = TestClient(self.app)
        self.headers = {"X-Bot-Secret": SHARED_SECRET}

    def tearDown(self):
        self.client.close()
        bot.repository = self.old_repository
        bot.SCHEDULE_DIR = self.old_schedule_dir
        self.temp.cleanup()

    def test_health_does_not_require_internal_secret(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_broadcast_pdf_requires_valid_secret_and_pdf_content(self):
        denied = self.client.post("/internal/broadcast-pdf?audience=student", content=b"%PDF-1.4\n")
        self.assertEqual(denied.status_code, 401)
        invalid = self.client.post("/internal/broadcast-pdf?audience=student", headers=self.headers, content=b"not a pdf")
        self.assertEqual(invalid.status_code, 400)
        bad_audience = self.client.post("/internal/broadcast-pdf?audience=applicant", headers=self.headers, content=b"%PDF-1.4\n")
        self.assertEqual(bad_audience.status_code, 400)

    def test_broadcast_pdf_creates_real_job_and_targets_matching_role(self):
        from types import SimpleNamespace
        for uid, role in ((10, "student"), (11, "teacher"), (12, "student")):
            self.repository.set_role(SimpleNamespace(id=uid, username=None, full_name=f"user {uid}"), role)
        response = self.client.post(
            "/internal/broadcast-pdf?audience=student&filename=week.pdf&caption=Week",
            headers=self.headers,
            content=b"%PDF-1.4\nreal fixture payload",
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["queued"], 2)
        self.assertEqual(data["delivered"], 0)
        stats = self.client.get(f"/internal/broadcast/{data['job_id']}", headers=self.headers)
        self.assertEqual(stats.json()["pending"], 2)
        self.assertEqual(stats.json()["delivered"], 0)
        self.assertEqual(stats.json()["failed"], 0)
        self.assertEqual(self.repository.latest("student")["filename"], "week.pdf")

    def test_group_schedule_persists_exact_pdf_bytes_and_rejects_invalid_group(self):
        invalid_group = self.client.post(
            "/internal/group-schedule?group_number=0", headers=self.headers, content=b"%PDF-1.4",
        )
        self.assertEqual(invalid_group.status_code, 400)
        response = self.client.post(
            "/internal/group-schedule?group_number=345&filename=schedule.pdf",
            headers=self.headers, content=b"%PDF-1.4\ncontents",
        )
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.repository.group_schedule(345)
        self.assertEqual(Path(saved["path"]).read_bytes(), b"%PDF-1.4\ncontents")

    def test_student_group_broadcast_queues_only_members_with_existing_group_pdf(self):
        from types import SimpleNamespace
        self.repository.upsert_group(345)
        self.repository.upsert_group(346)
        self.repository.save_group_schedule(345, "345.pdf", "", b"%PDF-1.4\n345")
        for uid, group in ((1, 345), (2, 346), (3, None)):
            self.repository.set_role(SimpleNamespace(id=uid, username=None, full_name="Student"), "student")
            self.repository.set_student_group(uid, group)
        response = self.client.post("/internal/broadcast-student-groups", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["queued"], 1)
        row = self.repository.next_delivery()
        self.assertEqual(row["telegram_id"], 1)

    def test_broadcast_status_is_protected(self):
        response = self.client.get("/internal/broadcast/1")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
