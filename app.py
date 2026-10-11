"""Application assembly and lifecycle."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI

from api.internal import register_internal_api
from config import BROADCAST_RETENTION_DAYS, DB_PATH, LEGACY_DB_PATH, LEGACY_DEFAULT_ROLE, TOKEN, logger
from core.context import Runtime
from handlers.admin import register_admin_handlers
from handlers.commands import public_commands
from handlers.help import register_help_handler
from handlers.roulette import register_roulette_handlers
from handlers.schedule import register_schedule_handlers
from handlers.subscription import register_subscription_handlers
from services.broadcast import worker
from storage.repository import Repository


def build_runtime() -> Runtime:
    router = Router()
    runtime = Runtime(
        bot=Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)),
        repository=Repository(DB_PATH),
        router=router,
    )
    register_subscription_handlers(router, runtime)
    register_schedule_handlers(router, runtime)
    register_roulette_handlers(router, runtime)
    register_help_handler(router)
    register_admin_handlers(router, runtime)
    return runtime


def create_app(runtime: Runtime | None = None) -> FastAPI:
    runtime = runtime or build_runtime()
    dispatcher = Dispatcher()
    dispatcher.include_router(runtime.router)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        migrated = runtime.repository.migrate_legacy(LEGACY_DB_PATH, LEGACY_DEFAULT_ROLE)
        cleaned = runtime.repository.cleanup_completed_broadcasts(BROADCAST_RETENTION_DAYS)
        if migrated:
            logger.info("Imported %s subscribers from legacy database", migrated)
        if cleaned:
            logger.info("Removed %s expired broadcast jobs", cleaned)
        await runtime.bot.delete_webhook(drop_pending_updates=False)
        await runtime.bot.set_my_commands(public_commands())
        polling = asyncio.create_task(dispatcher.start_polling(runtime.bot, handle_signals=False))
        delivery_worker = asyncio.create_task(worker(runtime))
        try:
            yield
        finally:
            polling.cancel()
            delivery_worker.cancel()
            await asyncio.gather(polling, delivery_worker, return_exceptions=True)
            await runtime.bot.session.close()

    app = FastAPI(title="MGKAP schedule bot", lifespan=lifespan)
    app.include_router(register_internal_api(runtime))
    return app


runtime = build_runtime()
app = create_app(runtime)
