from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BufferedInputFile

from config import SCHEDULE_DIR as DEFAULT_SCHEDULE_DIR, SEND_RATE as DEFAULT_SEND_RATE, logger


def _send_rate() -> int:
    runtime = sys.modules.get("bot")
    return int(getattr(runtime, "SEND_RATE", DEFAULT_SEND_RATE)) if runtime else DEFAULT_SEND_RATE


def _schedule_dir():
    runtime = sys.modules.get("bot")
    return getattr(runtime, "SCHEDULE_DIR", DEFAULT_SCHEDULE_DIR) if runtime else DEFAULT_SCHEDULE_DIR


async def pace(state: dict[str, Any]) -> None:
    lock = state.setdefault("lock", asyncio.Lock())
    async with lock:
        delay = max(0.0, 1 / _send_rate() - (time.monotonic() - state.get("last", 0.0)))
        if delay:
            await asyncio.sleep(delay)
        state["last"] = time.monotonic()


async def retry_send(state: dict[str, Any], action: Any) -> Any:
    for attempt in range(3):
        try:
            await pace(state)
            return await action()
        except TelegramRetryAfter as error:
            await asyncio.sleep(float(error.retry_after) + .25)
        except (TelegramNetworkError, TimeoutError):
            if attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError("send failed")


def is_unavailable_chat(error: TelegramBadRequest) -> bool:
    message = str(error).lower()
    return "chat not found" in message or "user is deactivated" in message


def remove_completed_job_file(job_id: int, path: str | None) -> None:
    if not path:
        return
    target = Path(path)
    if target.name == f"job_{job_id}.pdf" and target.parent.resolve() == _schedule_dir().resolve():
        target.unlink(missing_ok=True)


async def worker(runtime) -> None:
    state: dict[str, Any] = {}
    while True:
        row = runtime.repository.next_delivery()
        if row is None:
            await asyncio.sleep(.5)
            continue
        job_id, uid = row["job_id"], row["telegram_id"]
        try:
            if row["kind"] == "copy":
                await retry_send(state, lambda: runtime.bot.copy_message(chat_id=uid, from_chat_id=row["source_chat_id"], message_id=row["source_message_id"]))
            elif row["kind"] == "student_group_pdf":
                delivery_mode = row["delivery_student_schedule_mode"] or row["student_schedule_mode"]
                delivery_group = row["delivery_student_group"] if row["delivery_student_group"] is not None else row["student_group"]
                if delivery_mode == "full":
                    schedule = runtime.repository.publication_schedule(row["publication_id"], "student") if row["publication_id"] else runtime.repository.latest("student")
                else:
                    group_number = delivery_group
                    schedule = runtime.repository.publication_schedule(row["publication_id"], "student_group", group_number) if row["publication_id"] and group_number else (runtime.repository.group_schedule(group_number) if group_number else None)
                if not schedule or not Path(schedule["path"]).is_file():
                    raise FileNotFoundError(f"Student schedule is unavailable for user {uid}")
                document = BufferedInputFile(Path(schedule["path"]).read_bytes(), filename=schedule["filename"])
                await retry_send(state, lambda: runtime.bot.send_document(uid, document=document, caption=schedule["caption"] or None))
            else:
                file_id = row["telegram_file_id"]
                document = file_id or BufferedInputFile(Path(row["path"]).read_bytes(), filename=row["filename"])
                sent = await retry_send(state, lambda: runtime.bot.send_document(uid, document=document, caption=row["caption"] or None))
                if not file_id and getattr(sent.document, "file_id", None):
                    runtime.repository.save_job_file_id(job_id, sent.document.file_id)
                    latest = runtime.repository.latest(row["audience"])
                    if latest and latest["content_hash"] == hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest():
                        runtime.repository.save_file_id(row["audience"], sent.document.file_id)
            remove_completed_job_file(job_id, runtime.repository.mark_delivery(job_id, uid, "delivered"))
        except TelegramForbiddenError:
            runtime.repository.remove(uid)
            remove_completed_job_file(job_id, runtime.repository.mark_delivery(job_id, uid, "removed"))
        except TelegramBadRequest as error:
            if is_unavailable_chat(error):
                runtime.repository.remove(uid)
                remove_completed_job_file(job_id, runtime.repository.mark_delivery(job_id, uid, "removed"))
                continue
            logger.warning("Delivery rejected for job %s and user %s: %s", job_id, uid, error)
            attempts = runtime.repository.bump_delivery_attempt(job_id, uid)
            if attempts >= 3:
                remove_completed_job_file(job_id, runtime.repository.mark_delivery(job_id, uid, "failed"))
            else:
                await asyncio.sleep(2 * attempts)
        except Exception as error:
            logger.warning("Delivery failed for job %s and user %s: %s", job_id, uid, error)
            attempts = runtime.repository.bump_delivery_attempt(job_id, uid)
            if attempts >= 3:
                remove_completed_job_file(job_id, runtime.repository.mark_delivery(job_id, uid, "failed"))
            else:
                await asyncio.sleep(2 * attempts)
