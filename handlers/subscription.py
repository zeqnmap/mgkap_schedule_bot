from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ROLES
from handlers.schedule import schedule_mode_keyboard


def roles_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Я учащийся 🎓", callback_data="role:student"), InlineKeyboardButton(text="Я преподаватель 💼", callback_data="role:teacher")],
        [InlineKeyboardButton(text="Я абитуриент", callback_data="role:applicant")],
        [InlineKeyboardButton(text="Отказаться от подписки", callback_data="subscription:unsubscribe")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def unsubscribe_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, отказаться", callback_data="subscription:confirm-unsubscribe")],
        [InlineKeyboardButton(text="Оставить подписку", callback_data="subscription:cancel-unsubscribe")],
    ])


def register_subscription_handlers(router: Router, runtime) -> None:
    def message_for(uid: int) -> str:
        subscriber = runtime.repository.subscriber(uid)
        if subscriber:
            group_text = f"\nГруппа: <b>{subscriber['student_group']}</b>." if subscriber["role"] == "student" and subscriber["student_group"] else ""
            return f"Текущая роль: <b>{ROLES[subscriber['role']]}</b>.{group_text}\nВыберите другую роль или откажитесь от подписки."
        return "Выберите роль — она определяет, какое расписание вы будете получать."

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        runtime.group_waiting.discard(message.from_user.id)
        subscriber = runtime.repository.subscriber(message.from_user.id)
        await message.answer(message_for(message.from_user.id), reply_markup=roles_keyboard())

    @router.callback_query(F.data.startswith("role:"))
    async def choose_role(callback: CallbackQuery) -> None:
        selected = callback.data.split(":", 1)[1]
        if selected not in ROLES or not callback.message:
            await callback.answer("Неизвестная роль", show_alert=True)
            return
        subscriber = runtime.repository.subscriber(callback.from_user.id)
        saved_group = subscriber["student_group"] if subscriber else None
        runtime.repository.set_role(callback.from_user, selected)
        runtime.group_waiting.discard(callback.from_user.id)
        if selected == "applicant":
            text = "✅ Роль сохранена: <b>Абитуриент</b>.\nИнформация для абитуриентов появится позже.\nИзменить роль: /role"
        elif selected == "student":
            if saved_group and runtime.repository.group_exists(saved_group):
                await callback.message.edit_text(
                    f"✅ Роль сохранена: <b>Учащийся</b>.\nВаша группа: <b>{saved_group}</b>.",
                    reply_markup=schedule_mode_keyboard(subscriber["student_schedule_mode"]),
                )
                await callback.answer("Роль сохранена")
                return
            runtime.group_waiting.add(callback.from_user.id)
            await callback.message.edit_text("✅ Роль сохранена: <b>Учащийся</b>.\nТеперь напишите номер вашей группы, чтобы получать индивидуальный файл с расписанием.", reply_markup=None)
            await callback.answer("Введите группу")
            return
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

    @router.callback_query(F.data == "subscription:change-group")
    async def change_group(callback: CallbackQuery) -> None:
        subscriber = runtime.repository.subscriber(callback.from_user.id)
        if not callback.message or not subscriber or subscriber["role"] != "student":
            await callback.answer("Сначала выберите роль учащегося.", show_alert=True)
            return
        runtime.group_waiting.add(callback.from_user.id)
        await callback.message.edit_text("Напишите номер новой группы.")
        await callback.answer("Введите новую группу")

    @router.message(lambda message: message.from_user.id in runtime.group_waiting)
    async def student_group(message: Message) -> None:
        uid = message.from_user.id
        if uid not in runtime.group_waiting:
            return
        raw = (message.text or "").strip()
        if not raw.isdigit() or not runtime.repository.group_exists(int(raw)):
            await message.answer("Такой группы не существует. Напишите номер существующей группы.")
            return
        group_number = int(raw)
        runtime.repository.set_student_group(uid, group_number)
        runtime.group_waiting.discard(uid)
        subscriber = runtime.repository.subscriber(uid)
        delivery_text = "полное расписание" if subscriber["student_schedule_mode"] == "full" else "расписание только вашей группы"
        await message.answer(f"✅ Группа <b>{group_number}</b> сохранена. При автоматической рассылке вы получите {delivery_text}.")
