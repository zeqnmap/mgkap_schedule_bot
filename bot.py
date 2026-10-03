"""MGKAP schedule bot with role subscriptions, admin tools and safe broadcasts."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import os
import random
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from fastapi import FastAPI, Header, HTTPException, Request
import uvicorn
from handlers.commands import public_commands
from handlers.help import register_help_handler
from config import (
    ADMIN_IDS, AUDIENCES, BELLS_PATH, DB_PATH, LEGACY_DB_PATH, ROLES,
    SCHEDULE_DIR, SEND_RATE, SHARED_SECRET, TOKEN, logger,
)
from storage.repository import Repository


"""Telegram handlers and application wiring."""


repository = Repository(DB_PATH)
router = Router()
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dispatcher = Dispatcher()
dispatcher.include_router(router)
register_help_handler(router)
admin_states: dict[int, str] = {}
admin_broadcast_source: dict[int, Message] = {}
roll_waiting: set[int] = set()
reroll_options: dict[str, tuple[float, list[Any]]] = {}
broadcast_send_lock = asyncio.Lock()
last_broadcast_send = 0.0
ROULETTE_FORBIDDEN = {18, 22, 25, 26, 27, 28, "18", "22", "25", "26", "27", "28", "Манько", "манько"}


async def pace_broadcast() -> None:
    global last_broadcast_send
    async with broadcast_send_lock:
        delay = max(0.0, 1 / SEND_RATE - (time.monotonic() - last_broadcast_send))
        if delay:
            await asyncio.sleep(delay)
        last_broadcast_send = time.monotonic()


def admin_id(uid: int | None) -> bool:
    return bool(uid and uid in ADMIN_IDS)


def roles_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Я учащийся 🎓", callback_data="role:student"), InlineKeyboardButton(text="Я преподаватель 💼", callback_data="role:teacher")], [InlineKeyboardButton(text="Я абитуриент", callback_data="role:applicant")]])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"), InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🗑 Удалить пользователя", callback_data="admin:delete"), InlineKeyboardButton(text="📄 Последние PDF", callback_data="admin:latest")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:close")],
    ])


async def retry_send(chat_id: int, action: Any) -> Any:
    for attempt in range(3):
        try:
            await pace_broadcast()
            return await action()
        except TelegramRetryAfter as error:
            await asyncio.sleep(float(error.retry_after) + .25)
        except (TelegramNetworkError, TimeoutError):
            if attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"send failed: {chat_id}")


def is_unavailable_chat(error: TelegramBadRequest) -> bool:
    message = str(error).lower()
    return "chat not found" in message or "user is deactivated" in message


def remove_completed_job_file(job_id: int, path: str | None) -> None:
    if not path:
        return
    target = Path(path)
    if target.name == f"job_{job_id}.pdf" and target.parent.resolve() == SCHEDULE_DIR.resolve():
        target.unlink(missing_ok=True)


async def broadcast_worker() -> None:
    while True:
        row = repository.next_delivery()
        if row is None:
            await asyncio.sleep(.5)
            continue
        job_id, uid = row["job_id"], row["telegram_id"]
        file_id = row["telegram_file_id"]
        try:
            if row["kind"] == "copy":
                await retry_send(uid, lambda: bot.copy_message(chat_id=uid, from_chat_id=row["source_chat_id"], message_id=row["source_message_id"]))
            else:
                document = file_id or BufferedInputFile(Path(row["path"]).read_bytes(), filename=row["filename"])
                sent = await retry_send(uid, lambda: bot.send_document(uid, document=document, caption=row["caption"] or None))
                if not file_id and getattr(sent.document, "file_id", None):
                    repository.save_job_file_id(job_id, sent.document.file_id)
                    latest = repository.latest(row["audience"])
                    if latest and latest["content_hash"] == hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest():
                        repository.save_file_id(row["audience"], sent.document.file_id)
            remove_completed_job_file(job_id, repository.mark_delivery(job_id, uid, "delivered"))
        except TelegramForbiddenError:
            repository.remove(uid)
            remove_completed_job_file(job_id, repository.mark_delivery(job_id, uid, "removed"))
        except TelegramBadRequest as error:
            if is_unavailable_chat(error):
                repository.remove(uid)
                remove_completed_job_file(job_id, repository.mark_delivery(job_id, uid, "removed"))
                continue
            logger.warning("Delivery rejected for job %s and user %s: %s", job_id, uid, error)
            attempts = repository.bump_delivery_attempt(job_id, uid)
            if attempts >= 3:
                remove_completed_job_file(job_id, repository.mark_delivery(job_id, uid, "failed"))
            else:
                await asyncio.sleep(2 * attempts)
        except Exception as error:
            logger.warning("Delivery failed for job %s and user %s: %s", job_id, uid, error)
            attempts = repository.bump_delivery_attempt(job_id, uid)
            if attempts >= 3:
                remove_completed_job_file(job_id, repository.mark_delivery(job_id, uid, "failed"))
            else:
                await asyncio.sleep(2 * attempts)


@router.message(Command("start"))
async def start(message: Message) -> None:
    await message.answer("Выберите роль — она определяет, какое расписание вы будете получать.", reply_markup=roles_keyboard())


@router.message(Command("role"))
async def role(message: Message) -> None:
    await message.answer("Роль можно изменить в любой момент.", reply_markup=roles_keyboard())


@router.callback_query(F.data.startswith("role:"))
async def choose_role(callback: CallbackQuery) -> None:
    selected = callback.data.split(":", 1)[1]
    if selected not in ROLES or not callback.message:
        await callback.answer("Неизвестная роль", show_alert=True)
        return
    repository.set_role(callback.from_user, selected)
    if selected == "applicant":
        await callback.message.edit_text("✅ Роль сохранена: <b>Абитуриент</b>.\nИнформация для абитуриентов появится позже.\nИзменить роль: /role", reply_markup=None)
    else:
        await callback.message.edit_text(f"✅ Роль сохранена: <b>{ROLES[selected]}</b>.\nРасписание будет приходить автоматически.\nИзменить роль: /role", reply_markup=None)
    await callback.answer("Роль сохранена")


async def latest_to_user(message: Message, audience: str) -> None:
    row = repository.latest(audience)
    if not row or not Path(row["path"]).is_file():
        await message.answer("📭 Последнее расписание пока не загружено.")
        return
    document = row["telegram_file_id"] or BufferedInputFile(Path(row["path"]).read_bytes(), filename=row["filename"])
    sent = await bot.send_document(message.chat.id, document=document, caption=row["caption"] or "📌 Последнее расписание")
    if not row["telegram_file_id"] and getattr(sent.document, "file_id", None):
        repository.save_file_id(audience, sent.document.file_id)


@router.message(Command("schedule"))
async def schedule(message: Message) -> None:
    await latest_to_user(message, "student")


@router.message(Command("schedule_teacher"))
async def teacher_schedule(message: Message) -> None:
    await latest_to_user(message, "teacher")


@router.message(Command("bells"))
async def bells(message: Message) -> None:
    if not BELLS_PATH.is_file():
        await message.answer("📭 Расписание звонков пока не загружено.")
        return
    await message.answer_photo(BufferedInputFile(BELLS_PATH.read_bytes(), filename=BELLS_PATH.name), caption="🔔 Расписание звонков")


@router.message(Command("roll"))
async def roll(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw:
        roll_waiting.add(message.from_user.id)
        await message.answer("🎲 Пришлите диапазон 1-100 или варианты через запятую. /cancel — отмена.")
        return
    await process_roll(message, raw)


async def process_roll(message: Message, raw: str) -> None:
    roll_waiting.discard(message.from_user.id)
    if len(raw) > 300:
        await message.answer("Слишком длинный ввод: максимум 300 символов.")
        return
    match = re.fullmatch(r"(-?\d+)\s*-\s*(-?\d+)", raw)
    if match:
        start_value, end_value = map(int, match.groups())
        if start_value >= end_value or end_value - start_value > 500:
            await message.answer("Диапазон должен быть возрастающим и не больше 500 чисел.")
            return
        options: list[Any] = list(range(start_value, end_value + 1))
    else:
        options = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
        if len(options) < 2:
            await message.answer("Нужно минимум два варианта через запятую.")
            return
    filtered_options = [option for option in options if option not in ROULETTE_FORBIDDEN]
    final_options = filtered_options or options
    token = os.urandom(6).hex()
    reroll_options[token] = (time.monotonic() + 3600, final_options)
    await message.answer(f"🎯 Результат: <b>{html.escape(str(random.choice(final_options)))}</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Повторить", callback_data=f"reroll:{token}")]]))


@router.callback_query(F.data.startswith("reroll:"))
async def reroll(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 1)[1]
    item = reroll_options.get(token)
    if not item or item[0] < time.monotonic():
        await callback.answer("Запустите /roll заново", show_alert=True)
        return
    await callback.message.edit_text(f"🎯 Результат: <b>{html.escape(str(random.choice(item[1])))}</b>", reply_markup=callback.message.reply_markup)
    await callback.answer()


@router.message(Command("admin"))
async def admin(message: Message) -> None:
    if admin_id(message.from_user.id):
        await message.answer("🔐 Админ-панель", reply_markup=admin_keyboard())


@router.message(Command("broadcast_status"))
async def admin_broadcast_status(message: Message, command: CommandObject) -> None:
    if not admin_id(message.from_user.id):
        return
    try:
        job_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Использование: /broadcast_status 123")
        return
    stats = repository.job_stats(job_id)
    await message.answer(
        f"Рассылка №{job_id}\nВ очереди: {stats['pending']}\nОтправлено: {stats['delivered']}\n"
        f"Удалено заблокировавших: {stats['removed']}\nОшибок: {stats['failed']}"
    )


@router.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery) -> None:
    if not admin_id(callback.from_user.id) or not callback.message:
        await callback.answer()
        return
    action = callback.data.split(":", 1)[1]
    await callback.answer()
    if action == "close":
        await callback.message.edit_text("Админ-панель закрыта.")
    elif action == "stats":
        stats = repository.stats()
        await callback.message.edit_text("📊 <b>Статистика</b>\n\n" + "\n".join([f"Всего: <b>{stats['total']}</b>", f"Учащиеся: <b>{stats['student']}</b>", f"Преподаватели: <b>{stats['teacher']}</b>", f"Абитуриенты: <b>{stats['applicant']}</b>"]), reply_markup=admin_keyboard())
    elif action == "users":
        rows, total = repository.users()
        lines = [f"👥 <b>Пользователи ({total})</b>"] + [f"<code>{r['telegram_id']}</code> — {ROLES[r['role']]} — @{html.escape(r['username'] or 'без username')}" for r in rows]
        if total > len(rows): lines.append(f"\nПоказаны первые {len(rows)}.")
        await callback.message.edit_text("\n".join(lines), reply_markup=admin_keyboard())
    elif action == "broadcast":
        admin_states[callback.from_user.id] = "broadcast_material"
        admin_broadcast_source.pop(callback.from_user.id, None)
        await callback.message.answer("Отправьте текст, фото или файл для рассылки. Затем отдельным сообщением укажите student, teacher, applicant или all. /cancel — отмена.")
    elif action == "delete":
        admin_states[callback.from_user.id] = "delete"
        await callback.message.answer("Отправьте Telegram ID пользователя. /cancel — отмена.")
    elif action == "latest":
        lines = [f"{ROLES[a]}: {(repository.latest(a) or {'filename': 'нет'})['filename']}" for a in ("student", "teacher")]
        await callback.message.edit_text("📄 " + "\n".join(lines), reply_markup=admin_keyboard())


@router.message()
async def admin_state(message: Message) -> None:
    if message.from_user.id in roll_waiting:
        if message.text and message.text.strip().lower() == "/cancel":
            roll_waiting.discard(message.from_user.id)
            await message.answer("Рулетка отменена.")
        elif message.text:
            await process_roll(message, message.text.strip())
        else:
            await message.answer("Нужен текстовый диапазон или список вариантов.")
        return
    if not admin_id(message.from_user.id):
        return
    state = admin_states.get(message.from_user.id)
    if not state:
        return
    if message.text and message.text.strip().lower() == "/cancel":
        admin_states.pop(message.from_user.id, None)
        admin_broadcast_source.pop(message.from_user.id, None)
        await message.answer("Отменено.", reply_markup=admin_keyboard())
        return
    if state == "delete":
        try:
            uid = int((message.text or "").strip())
        except ValueError:
            await message.answer("ID должен быть целым числом.")
            return
        removed = repository.remove(uid)
        admin_states.pop(message.from_user.id, None)
        await message.answer("✅ Пользователь удалён." if removed else "Пользователь не найден.", reply_markup=admin_keyboard())
    elif state == "broadcast_material":
        if message.text and message.text.lower() in AUDIENCES:
            source = admin_broadcast_source.pop(message.from_user.id, None)
            if source is None:
                await message.answer("Сначала отправьте материал для рассылки.")
                return
            admin_states.pop(message.from_user.id, None)
            job_id = repository.enqueue_copy(message.text.lower(), source)
            queued = repository.job_stats(job_id)["pending"]
            await message.answer(f"📣 Рассылка №{job_id} поставлена в очередь для {queued} подписчиков.\nПроверить: /broadcast_status {job_id}", reply_markup=admin_keyboard())
            return
        if message.content_type not in {"text", "photo", "document", "video", "audio", "voice", "animation"}:
            await message.answer("Поддерживаются текст, фото, документ, видео, аудио или голосовое сообщение.")
            return
        admin_broadcast_source[message.from_user.id] = message
        await message.answer("Материал сохранён. Теперь отдельным сообщением отправьте: student, teacher, applicant или all.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    repository.migrate_legacy(LEGACY_DB_PATH)
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_my_commands(public_commands())
    polling = asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False))
    worker = asyncio.create_task(broadcast_worker())
    try:
        yield
    finally:
        polling.cancel()
        worker.cancel()
        await asyncio.gather(polling, worker, return_exceptions=True)
        await bot.session.close()


app = FastAPI(title="MGKAP schedule bot", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/broadcast-pdf")
async def internal_broadcast_pdf(request: Request, audience: str, filename: str = "Расписание.pdf", caption: str = "", x_bot_secret: str | None = Header(default=None)) -> dict[str, int]:
    if not x_bot_secret or not hmac.compare_digest(x_bot_secret, SHARED_SECRET):
        raise HTTPException(status_code=401, detail="Invalid internal token")
    if audience not in {"student", "teacher", "all"}:
        raise HTTPException(status_code=400, detail="Audience must be student, teacher or all")
    content = await request.body()
    if not content or not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Body must be a non-empty PDF")
    if len(content) > 49 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF is too large")
    job_id = repository.enqueue_pdf(audience, filename, caption, content)
    return {"job_id": job_id, "queued": len(repository.recipients(audience)), "delivered": 0, "failed": 0}


@app.get("/internal/broadcast/{job_id}")
async def broadcast_status(job_id: int, x_bot_secret: str | None = Header(default=None)) -> dict[str, int]:
    if not x_bot_secret or not hmac.compare_digest(x_bot_secret, SHARED_SECRET):
        raise HTTPException(status_code=401, detail="Invalid internal token")
    return repository.job_stats(job_id)


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("BOT_BIND_HOST", "0.0.0.0"), port=int(os.environ.get("BOT_BIND_PORT", "8081")))
