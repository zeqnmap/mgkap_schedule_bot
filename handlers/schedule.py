from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import BELLS_PATH


def schedule_mode_keyboard(mode: str) -> InlineKeyboardMarkup:
    text = "📄 Получать полное расписание" if mode == "group" else "🎓 Получать расписание моей группы"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="schedule:toggle-mode")],
        [InlineKeyboardButton(text="✏️ Изменить свою группу", callback_data="subscription:change-group")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="subscription:done")],
    ])


async def send_latest_to_user(runtime, message: Message, audience: str, group_number: int | None = None) -> None:
    if audience == "student" and group_number is not None:
        row = runtime.repository.group_schedule(group_number)
        if not row or not Path(row["path"]).is_file():
            await message.answer("📭 Персональное расписание для вашей группы пока не загружено.")
            return
    else:
        row = runtime.repository.latest(audience)
    if not row or not Path(row["path"]).is_file():
        await message.answer("📭 Последнее расписание пока не загружено.")
        return
    document = row["telegram_file_id"] or BufferedInputFile(Path(row["path"]).read_bytes(), filename=row["filename"])
    await runtime.bot.send_document(message.chat.id, document=document, caption=row["caption"] or "📌 Последнее расписание")


def register_schedule_handlers(router: Router, runtime) -> None:

    @router.message(Command("schedule"))
    async def schedule(message: Message) -> None:
        await send_latest_to_user(runtime, message, "student")

    @router.message(Command("schedule_teacher"))
    async def teacher_schedule(message: Message) -> None:
        await send_latest_to_user(runtime, message, "teacher")

    @router.callback_query(F.data == "schedule:toggle-mode")
    async def toggle_schedule_mode(callback: CallbackQuery) -> None:
        if not callback.message:
            await callback.answer()
            return
        subscriber = runtime.repository.subscriber(callback.from_user.id)
        if not subscriber or subscriber["role"] != "student" or not subscriber["student_group"]:
            await callback.answer("Сначала выберите роль учащегося и группу.", show_alert=True)
            return
        next_mode = "full" if subscriber["student_schedule_mode"] == "group" else "group"
        runtime.repository.set_student_schedule_mode(callback.from_user.id, next_mode)
        text = "✅ Включено: при следующих рассылках будет приходить полное расписание." if next_mode == "full" else "✅ Включено: при следующих рассылках будет приходить расписание только вашей группы."
        await callback.message.edit_text(text)
        await callback.answer("Настройка сохранена")

    @router.message(Command("bells"))
    async def bells(message: Message) -> None:
        if not BELLS_PATH.is_file():
            await message.answer("📭 Расписание звонков пока не загружено.")
            return
        await message.answer_photo(BufferedInputFile(BELLS_PATH.read_bytes(), filename=BELLS_PATH.name), caption="🔔 Расписание звонков")
