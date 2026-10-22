from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from bot.app import App
from bot.ops import missing_rights
from bot.panel import chat_keyboard, chat_text, home_keyboard, home_text


async def render_home(app: App, user_id: int):
    chats = await app.db.chats_for_user(user_id)
    text = home_text(len(chats))
    markup = home_keyboard(chats, user_id == app.owner_id, app.bot_username)
    return text, markup


async def render_chat(app: App, bot: Bot, chat_id: int):
    chat = await app.db.get_chat(chat_id)
    if chat is None or not chat["active"]:
        return None
    ads = await app.db.get_ads()
    try:
        missing = await missing_rights(bot, chat_id, app.bot_id)
    except TelegramBadRequest:
        missing = ["бот не видит чат"]
    return chat_text(chat, ads, missing), chat_keyboard(chat, app.bot_username, missing)
