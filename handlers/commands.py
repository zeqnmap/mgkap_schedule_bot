"""Command menu shown by Telegram clients."""

from aiogram.types import BotCommand


def public_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="🚀 Запустить бота"),
        BotCommand(command="role", description="Изменить роль или подписку"),
        BotCommand(command="roll", description="🎲 Рулетка"),
        BotCommand(command="help", description="🆘 Помощь"),
        BotCommand(command="schedule", description="Последнее расписание для студента"),
        BotCommand(command="schedule_teacher", description="Последнее расписание для преподавателя"),
    ]
