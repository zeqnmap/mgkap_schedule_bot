"""The public help message, matching the legacy bot wording."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

HELP_TEXT = (
    "ℹ️ Если вам <b>не пришло</b> новое расписание, нажмите на кнопку <b>/start</b> в этом же меню и выберите <b>вашу роль</b>.\n"
    "После чего в меню на <b>/schedule</b>  'Последнее расписание'.\n\n"
    "Если проблема осталась или вы не понимаете интерфейс — напишите разработчику бота: @zeqnmap\n\n"
    "📬 <b>Постараюсь быстро вам ответить!</b>"
)


def register_help_handler(router: Router) -> None:
    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(HELP_TEXT)
