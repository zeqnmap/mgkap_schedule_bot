from __future__ import annotations

import html
import os
import random
import re
import time
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

FORBIDDEN = {18, 22, 25, 26, 27, 28, "18", "22", "25", "26", "27", "28", "Манько", "манько"}


def register_roulette_handlers(router: Router, runtime) -> None:
    async def process_roll(message: Message, raw: str) -> None:
        runtime.roll_waiting.discard(message.from_user.id)
        if len(raw) > 300:
            await message.answer("Слишком длинный ввод: максимум 300 символов.")
            return
        match = re.fullmatch(r"(-?\d+)\s*-\s*(-?\d+)", raw)
        if match:
            start, end = map(int, match.groups())
            if start >= end or end - start > 500:
                await message.answer("Диапазон должен быть возрастающим и не больше 500 чисел.")
                return
            options: list[Any] = list(range(start, end + 1))
        else:
            options = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
            if len(options) < 2:
                await message.answer("Нужно минимум два варианта через запятую.")
                return
        final_options = [option for option in options if option not in FORBIDDEN] or options
        token = os.urandom(6).hex()
        runtime.reroll_options[token] = (time.monotonic() + 3600, final_options)
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Повторить", callback_data=f"reroll:{token}")]])
        await message.answer(f"🎯 Результат: <b>{html.escape(str(random.choice(final_options)))}</b>", reply_markup=markup)

    @router.message(Command("roll"))
    async def roll(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        if not raw:
            runtime.roll_waiting.add(message.from_user.id)
            await message.answer("🎲 Пришлите диапазон 1-100 или варианты через запятую. /cancel — отмена.")
            return
        await process_roll(message, raw)

    @router.callback_query(F.data.startswith("reroll:"))
    async def reroll(callback: CallbackQuery) -> None:
        token = callback.data.split(":", 1)[1]
        item = runtime.reroll_options.get(token)
        if not item or item[0] < time.monotonic():
            await callback.answer("Запустите /roll заново", show_alert=True)
            return
        await callback.message.edit_text(f"🎯 Результат: <b>{html.escape(str(random.choice(item[1])))}</b>", reply_markup=callback.message.reply_markup)
        await callback.answer()

    runtime.process_roll = process_roll
