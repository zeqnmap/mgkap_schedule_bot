from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, Router

from storage.repository import Repository


@dataclass
class Runtime:
    bot: Bot
    repository: Repository
    router: Router
    admin_states: dict[int, str] = field(default_factory=dict)
    admin_broadcast_source: dict[int, Any] = field(default_factory=dict)
    roll_waiting: set[int] = field(default_factory=set)
    reroll_options: dict[str, tuple[float, list[Any]]] = field(default_factory=dict)
    group_waiting: set[int] = field(default_factory=set)
