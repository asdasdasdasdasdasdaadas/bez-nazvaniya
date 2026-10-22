from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: int
    db_path: str


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    owner_raw = os.getenv("OWNER_ID", "").strip()
    if not token:
        raise SystemExit("Создайте файл .env и укажите BOT_TOKEN. Пример есть в .env.example.")
    if not owner_raw.isdigit():
        raise SystemExit("В .env укажите OWNER_ID — числовой Telegram ID создателя бота.")
    db_path = os.getenv("DATABASE_PATH", "bot.db").strip() or "bot.db"
    return Settings(bot_token=token, owner_id=int(owner_raw), db_path=db_path)
