"""Entrypoint for the MGKAP schedule bot."""

import os

import uvicorn

from app import app, runtime
from config import SCHEDULE_DIR, SEND_RATE
from services.broadcast import worker as broadcast_worker_service
from storage.repository import Repository

# Compatibility exports for integrations that imported the old entrypoint.
bot = runtime.bot
repository = runtime.repository


async def broadcast_worker() -> None:
    runtime.bot = bot
    runtime.repository = repository
    await broadcast_worker_service(runtime)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("BOT_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("BOT_BIND_PORT", "8081")),
    )
