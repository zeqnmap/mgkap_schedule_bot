"""Environment-backed configuration for the bot."""

import logging
import os
from pathlib import Path

ROLES = {"student": "Учащиеся", "teacher": "Преподаватели", "applicant": "Абитуриенты"}
AUDIENCES = {"student", "teacher", "applicant", "all"}
ADMIN_IDS = {int(value) for value in os.environ.get("BOT_ADMIN_IDS", "").split(",") if value.strip().isdigit()}
TOKEN = os.environ["BOT_TOKEN"]
SHARED_SECRET = os.environ["BOT_SHARED_SECRET"]
DB_PATH = Path(os.environ.get("BOT_DB_PATH", "/app/bot.db"))
SCHEDULE_DIR = Path(os.environ.get("BOT_SCHEDULE_DIR", "/app/schedules"))
BELLS_PATH = Path(os.environ.get("BOT_BELLS_PATH", "/app/assets/bells.png"))
LEGACY_DB_PATH = os.environ.get("BOT_LEGACY_DB_PATH", "")
LEGACY_DEFAULT_ROLE = os.environ.get("BOT_LEGACY_DEFAULT_ROLE", "")
SEND_RATE = max(1, int(os.environ.get("BOT_BROADCAST_RATE", "15")))
BROADCAST_RETENTION_DAYS = max(1, int(os.environ.get("BOT_BROADCAST_RETENTION_DAYS", "7")))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mgkap_schedule_bot")
