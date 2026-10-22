from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from bot.app import App
from bot.logic import contains_link, human_seconds, is_caps
from bot.ops import ban_member, delete_message, ensure_chat, fade, mention, mute_member
from bot.panel import SPAM_REASON

logger = logging.getLogger(__name__)
router = Router()
GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})


def _has_link(message: Message) -> bool:
    text = message.text or message.caption or ""
    if contains_link(text):
        return True
    entities = list(message.entities or []) + list(message.caption_entities or [])
    return any(entity.type in {"url", "text_link"} for entity in entities)


def _is_forward(message: Message) -> bool:
    return any(
        getattr(message, name, None) is not None
        for name in ("forward_origin", "forward_from", "forward_from_chat")
    )


async def _newcomer(app: App, chat: dict, user_id: int) -> bool:
    seconds = int(chat["newcomer_seconds"])
    if seconds <= 0:
        return False
    row = await app.db.get_verified(chat["chat_id"], user_id)
    if row is None:
        return False
    return time.time() - int(row["verified_at"]) < seconds


async def _punish(bot: Bot, app: App, chat: dict, message: Message, reason_key: str) -> None:
    await delete_message(bot, message.chat.id, message.message_id)
    user = message.from_user
    action = chat["spam_action"]
    if action in {"mute", "ban"}:
        if not app.tracker.claim("punish", chat["chat_id"], user.id, 60):
            return
        try:
            if action == "mute":
                await mute_member(bot, chat["chat_id"], user.id, int(chat["spam_mute_seconds"]))
            else:
                await ban_member(bot, chat["chat_id"], user.id, 0)
        except TelegramBadRequest as exc:
            logger.info("Наказание за спам не применилось: %s", exc)
            return
        until = None
        if action == "mute":
            until = int(time.time()) + int(chat["spam_mute_seconds"])
        await app.db.add_log(
            chat["chat_id"],
            user.id,
            "spam_ban" if action == "ban" else "spam_mute",
            reason=SPAM_REASON.get(reason_key, "спам"),
            target_name=user.full_name,
            until_ts=until,
        )
    if not app.tracker.claim("notice", chat["chat_id"], user.id, 25):
        return
    reason = SPAM_REASON.get(reason_key, "спам")
    text = f"{mention(user.id, user.full_name)}: {reason}. Сообщение удалено."
    if action == "mute":
        text += f" Мут на {human_seconds(int(chat['spam_mute_seconds']))}."
    elif action == "ban":
        text += " Бан."
    await fade(bot, chat["chat_id"], text, seconds=20)


async def inspect(message: Message, bot: Bot, app: App, *, edited: bool) -> None:
    user = message.from_user
    if user is None or user.is_bot or message.sender_chat is not None:
        return
    if getattr(message, "is_automatic_forward", False):
        return
    if (message.text or "").startswith("/"):
        return
    chat = await ensure_chat(bot, app.db, app.bot_id, message.chat)
    if chat is None or not int(chat["moderation_enabled"]):
        return
    admins = await app.admins.ids(bot, message.chat.id)
    if admins is None or user.id in admins:
        return
    if await app.db.get_pending(message.chat.id, user.id):
        await delete_message(bot, message.chat.id, message.message_id)
        return

    text = message.text or message.caption or ""
    linked = _has_link(message)
    forwarded = _is_forward(message)
    newbie = await _newcomer(app, chat, user.id)

    if not edited and app.tracker.flood(
        message.chat.id,
        user.id,
        int(chat["spam_flood_count"]),
        int(chat["spam_flood_window"]),
    ):
        await _punish(bot, app, chat, message, "flood")
        return
    if linked and (chat["spam_links"] or newbie):
        await _punish(bot, app, chat, message, "links" if chat["spam_links"] else "newcomer_links")
        return
    if not edited and forwarded and (chat["spam_forwards"] or newbie):
        await _punish(
            bot,
            app,
            chat,
            message,
            "forwards" if chat["spam_forwards"] else "newcomer_forwards",
        )
        return
    if chat["spam_caps"] and text and is_caps(text):
        await _punish(bot, app, chat, message, "caps")
        return
    if not edited and chat["spam_repeats"] and text and app.tracker.repeat(message.chat.id, user.id, text):
        await _punish(bot, app, chat, message, "repeat")
        return
    if (
        not edited
        and chat["spam_stickers"]
        and message.sticker is not None
        and app.tracker.stickers(message.chat.id, user.id)
    ):
        await _punish(bot, app, chat, message, "stickers")


@router.message(GROUP)
async def on_message(message: Message, bot: Bot, app: App) -> None:
    await inspect(message, bot, app, edited=False)


@router.edited_message(GROUP)
async def on_edit(message: Message, bot: Bot, app: App) -> None:
    await inspect(message, bot, app, edited=True)
