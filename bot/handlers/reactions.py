from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import MessageReactionUpdated

from bot.app import App
from bot.captcha import offer_personal_ad

router = Router()


@router.message_reaction(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_reaction(event: MessageReactionUpdated, bot: Bot, app: App) -> None:
    """Человек поставил реакцию — показываем ему рекламу, как после сообщения."""
    user = event.user
    if user is None or user.is_bot:
        return
    if not event.new_reaction:
        return
    chat = await app.db.get_chat(event.chat.id)
    if chat is None or not chat["active"] or not int(chat["moderation_enabled"]):
        return
    await offer_personal_ad(bot, app, event.chat.id, user.id, await app.db.get_ads())
