from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ROLES


def roles_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Я учащийся 🎓", callback_data="role:student"), InlineKeyboardButton(text="Я преподаватель 💼", callback_data="role:teacher")],
        [InlineKeyboardButton(text="Я абитуриент", callback_data="role:applicant")],
        [InlineKeyboardButton(text="Отказаться от подписки", callback_data="subscription:unsubscribe")],
    ])


def unsubscribe_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, отказаться", callback_data="subscription:confirm-unsubscribe")],
        [InlineKeyboardButton(text="Оставить подписку", callback_data="subscription:cancel-unsubscribe")],
    ])


def register_subscription_handlers(router: Router, runtime) -> None:
    def message_for(uid: int) -> str:
        subscriber = runtime.repository.subscriber(uid)
        if subscriber:
            return f"Текущая роль: <b>{ROLES[subscriber['role']]}</b>. Выберите другую роль или откажитесь от подписки."
        return "Выберите роль — она определяет, какое расписание вы будете получать."

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(message_for(message.from_user.id), reply_markup=roles_keyboard())

    @router.message(Command("role"))
    async def role(message: Message) -> None:
        await message.answer(message_for(message.from_user.id), reply_markup=roles_keyboard())

    @router.callback_query(F.data.startswith("role:"))
    async def choose_role(callback: CallbackQuery) -> None:
        selected = callback.data.split(":", 1)[1]
        if selected not in ROLES or not callback.message:
            await callback.answer("Неизвестная роль", show_alert=True)
            return
        runtime.repository.set_role(callback.from_user, selected)
        if selected == "applicant":
            text = "✅ Роль сохранена: <b>Абитуриент</b>.\nИнформация для абитуриентов появится позже.\nИзменить роль: /role"
        else:
            text = f"✅ Роль сохранена: <b>{ROLES[selected]}</b>.\nРасписание будет приходить автоматически.\nИзменить роль: /role"
        await callback.message.edit_text(text, reply_markup=None)
        await callback.answer("Роль сохранена")

    @router.callback_query(F.data == "subscription:unsubscribe")
    async def unsubscribe_request(callback: CallbackQuery) -> None:
        if not callback.message:
            await callback.answer()
            return
        if not runtime.repository.subscriber(callback.from_user.id):
            await callback.answer("Подписка уже отключена", show_alert=True)
            return
        await callback.message.edit_text("Вы больше не будете получать автоматические рассылки. Подтвердить отказ от подписки?", reply_markup=unsubscribe_confirmation_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "subscription:confirm-unsubscribe")
    async def unsubscribe_confirm(callback: CallbackQuery) -> None:
        if not callback.message:
            await callback.answer()
            return
        runtime.repository.remove(callback.from_user.id)
        await callback.message.edit_text("✅ Подписка отключена. Вы можете вернуться в любой момент: /start", reply_markup=None)
        await callback.answer("Подписка отключена")

    @router.callback_query(F.data == "subscription:cancel-unsubscribe")
    async def unsubscribe_cancel(callback: CallbackQuery) -> None:
        if not callback.message:
            await callback.answer()
            return
        await callback.message.edit_text(message_for(callback.from_user.id), reply_markup=roles_keyboard())
        await callback.answer("Подписка сохранена")
