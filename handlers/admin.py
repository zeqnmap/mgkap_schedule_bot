from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS, AUDIENCES, ROLES


def is_admin(uid: int | None) -> bool:
    return bool(uid and uid in ADMIN_IDS)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"), InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton(text="🎓 Актуальные группы", callback_data="admin:groups")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🗑 Удалить пользователя", callback_data="admin:delete"), InlineKeyboardButton(text="📄 Последние PDF", callback_data="admin:latest")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:close")],
    ])


def register_admin_handlers(router: Router, runtime) -> None:
    def groups_text() -> str:
        rows = runtime.repository.groups()
        if not rows:
            return "🎓 Список групп пуст."
        return "🎓 <b>Актуальные группы</b>\n" + "\n".join(
            f"{row['number']} — {'активна' if row['is_active'] else 'отключена'}"
            for row in rows
        )

    @router.message(Command("admin"))
    async def admin(message: Message) -> None:
        if is_admin(message.from_user.id):
            await message.answer("🔐 Админ-панель", reply_markup=admin_keyboard())

    @router.message(Command("broadcast_status"))
    async def broadcast_status(message: Message, command: CommandObject) -> None:
        if not is_admin(message.from_user.id):
            return
        try:
            job_id = int((command.args or "").strip())
        except ValueError:
            await message.answer("Использование: /broadcast_status 123")
            return
        stats = runtime.repository.job_stats(job_id)
        await message.answer(f"Рассылка №{job_id}\nВ очереди: {stats['pending']}\nОтправлено: {stats['delivered']}\nУдалено заблокировавших: {stats['removed']}\nОшибок: {stats['failed']}")

    @router.callback_query(F.data.startswith("admin:"))
    async def admin_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id) or not callback.message:
            await callback.answer()
            return
        action = callback.data.split(":", 1)[1]
        await callback.answer()
        if action == "close":
            await callback.message.edit_text("Админ-панель закрыта.")
        elif action == "stats":
            stats = runtime.repository.stats()
            text = "📊 <b>Статистика</b>\n\n" + "\n".join([f"Всего: <b>{stats['total']}</b>", f"Учащиеся: <b>{stats['student']}</b>", f"Преподаватели: <b>{stats['teacher']}</b>", f"Абитуриенты: <b>{stats['applicant']}</b>"])
            await callback.message.edit_text(text, reply_markup=admin_keyboard())
        elif action == "users":
            rows, total = runtime.repository.users()
            lines = [f"👥 <b>Пользователи ({total})</b>"] + [f"<code>{r['telegram_id']}</code> — {ROLES[r['role']]}" + (f" — группа <b>{r['student_group']}</b>" if r['student_group'] else "") + f" — @{html.escape(r['username'] or 'без username')}" for r in rows]
            if total > len(rows):
                lines.append(f"\nПоказаны первые {len(rows)}.")
            await callback.message.edit_text("\n".join(lines), reply_markup=admin_keyboard())
        elif action == "broadcast":
            runtime.admin_states[callback.from_user.id] = "broadcast_material"
            runtime.admin_broadcast_source.pop(callback.from_user.id, None)
            await callback.message.answer("Отправьте текст, фото или файл для рассылки. Затем отдельным сообщением укажите student, teacher, applicant или all. /cancel — отмена.")
        elif action == "delete":
            runtime.admin_states[callback.from_user.id] = "delete"
            await callback.message.answer("Отправьте Telegram ID пользователя. /cancel — отмена.")
        elif action == "latest":
            lines = [f"{ROLES[a]}: {(runtime.repository.latest(a) or {'filename': 'нет'})['filename']}" for a in ("student", "teacher")]
            await callback.message.edit_text("📄 " + "\n".join(lines), reply_markup=admin_keyboard())
        elif action == "groups":
            runtime.admin_states[callback.from_user.id] = "groups"
            await callback.message.edit_text(groups_text() + "\n\nКоманды:\n<code>add 451</code> — добавить\n<code>on 451</code> — сделать активной\n<code>off 451</code> — отключить\n<code>remove 451</code> — удалить\n<code>list</code> — обновить список\n/cancel — выйти", reply_markup=admin_keyboard())

    @router.message()
    async def admin_state(message: Message) -> None:
        uid = message.from_user.id
        if uid in runtime.roll_waiting:
            if message.text and message.text.strip().lower() == "/cancel":
                runtime.roll_waiting.discard(uid)
                await message.answer("Рулетка отменена.")
            elif message.text:
                await runtime.process_roll(message, message.text.strip())
            else:
                await message.answer("Нужен текстовый диапазон или список вариантов.")
            return
        if not is_admin(uid):
            return
        state = runtime.admin_states.get(uid)
        if not state:
            return
        if message.text and message.text.strip().lower() == "/cancel":
            runtime.admin_states.pop(uid, None)
            runtime.admin_broadcast_source.pop(uid, None)
            await message.answer("Отменено.", reply_markup=admin_keyboard())
            return
        if state == "delete":
            try:
                target_uid = int((message.text or "").strip())
            except ValueError:
                await message.answer("ID должен быть целым числом.")
                return
            removed = runtime.repository.remove(target_uid)
            runtime.admin_states.pop(uid, None)
            await message.answer("✅ Пользователь удалён." if removed else "Пользователь не найден.", reply_markup=admin_keyboard())
        elif state == "groups":
            parts = (message.text or "").strip().lower().split()
            if parts == ["list"]:
                await message.answer(groups_text(), reply_markup=admin_keyboard())
                return
            if len(parts) != 2 or parts[0] not in {"add", "on", "off", "remove"} or not parts[1].isdigit():
                await message.answer("Формат: add 451, on 451, off 451, remove 451 или list.")
                return
            number = int(parts[1])
            action_name = parts[0]
            if action_name == "add":
                runtime.repository.upsert_group(number, True)
                response = f"✅ Группа {number} добавлена и активна."
            elif action_name == "on":
                response = f"✅ Группа {number} активирована." if runtime.repository.set_group_active(number, True) else f"Группа {number} не найдена. Сначала используйте add {number}."
            elif action_name == "off":
                response = f"✅ Группа {number} отключена." if runtime.repository.set_group_active(number, False) else f"Группа {number} не найдена."
            else:
                response = f"✅ Группа {number} отключена." if runtime.repository.remove_group(number) else f"Группа {number} не найдена."
            await message.answer(response + "\n\n" + groups_text(), reply_markup=admin_keyboard())
        elif state == "broadcast_material":
            if message.text and message.text.lower() in AUDIENCES:
                source = runtime.admin_broadcast_source.pop(uid, None)
                if source is None:
                    await message.answer("Сначала отправьте материал для рассылки.")
                    return
                runtime.admin_states.pop(uid, None)
                job_id = runtime.repository.enqueue_copy(message.text.lower(), source)
                queued = runtime.repository.job_stats(job_id)["pending"]
                await message.answer(f"📣 Рассылка №{job_id} поставлена в очередь для {queued} подписчиков.\nПроверить: /broadcast_status {job_id}", reply_markup=admin_keyboard())
                return
            if message.content_type not in {"text", "photo", "document", "video", "audio", "voice", "animation"}:
                await message.answer("Поддерживаются текст, фото, документ, видео, аудио или голосовое сообщение.")
                return
            runtime.admin_broadcast_source[uid] = message
            await message.answer("Материал сохранён. Теперь отдельным сообщением отправьте: student, teacher, applicant или all.")
