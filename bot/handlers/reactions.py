from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import MessageReactionUpdated

from bot.ads import offer_ad
from bot.app import App

router = Router()


@router.message_reaction(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_reaction(event: MessageReactionUpdated, bot: Bot, app: App) -> None:
    """Реакция — тоже действие в чате: повод показать рекламу."""
    user = event.user
    if user is None or user.is_bot:
        return
    if not event.new_reaction:
        return
    await offer_ad(bot, app, event.chat.id, user.id)
