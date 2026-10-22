from __future__ import annotations

from dataclasses import dataclass

from bot.db import Database
from bot.logic import SpamTracker
from bot.ops import AdminCache


@dataclass
class App:
    db: Database
    owner_id: int
    bot_username: str
    bot_id: int
    tracker: SpamTracker
    admins: AdminCache
