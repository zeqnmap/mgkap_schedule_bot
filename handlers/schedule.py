from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from config import BELLS_PATH


def register_schedule_handlers(router: Router, runtime) -> None:
    async def latest_to_user(message: Message, audience: str) -> None:
        row = runtime.repository.latest(audience)
        if not row or not Path(row["path"]).is_file():
            await message.answer("📭 Последнее расписание пока не загружено.")
            return
        document = row["telegram_file_id"] or BufferedInputFile(Path(row["path"]).read_bytes(), filename=row["filename"])
        sent = await runtime.bot.send_document(message.chat.id, document=document, caption=row["caption"] or "📌 Последнее расписание")
        if not row["telegram_file_id"] and getattr(sent.document, "file_id", None):
            runtime.repository.save_file_id(audience, sent.document.file_id)

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
