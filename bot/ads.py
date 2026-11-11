"""Реклама каждому участнику: до 3 показов в день, по его действию в чате.

Сообщение эфемерное: в группе его видит только сам человек. Показываем в момент,
когда человек что-то сделал в чате (написал, исправил сообщение, поставил реакцию):
он точно в сети, поэтому сообщение почти всегда доходит. Полной гарантии доставки
эфемерных сообщений Telegram не даёт.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

from bot.app import App
from bot.logic import ADS_PER_DAY, AD_MIN_GAP, ad_message_text, msk_day
from bot.ops import delete_message, send_ephemeral
from bot.panel import ad_keyboard

logger = logging.getLogger(__name__)

# Кто админ или с тегом — помним 10 минут, чтобы не спрашивать Telegram на каждое действие.
EXEMPT_CACHE_SECONDS = 600
_exempt: dict[tuple[int, int], tuple[float, bool]] = {}


def ads_for_members(ads: dict) -> bool:
    return bool(ads.get("enabled") and ads.get("personal") and (ads.get("text") or "").strip())


async def ad_exempt(bot: Bot, app: App, chat_id: int, user_id: int) -> bool:
    """True — рекламу не показывать: админ, владелец или участник с тегом."""
    key = (chat_id, user_id)
    now = time.monotonic()
    cached = _exempt.get(key)
    if cached and now - cached[0] < EXEMPT_CACHE_SECONDS:
        return cached[1]
    result = await _check_exempt(bot, app, chat_id, user_id)
    _exempt[key] = (now, result)
    return result


async def _check_exempt(bot: Bot, app: App, chat_id: int, user_id: int) -> bool:
    admins = await app.admins.ids(bot, chat_id)
    if admins is None or user_id in admins:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return True
    if member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        return True
    # Bot API 9.5: тег обычного участника. В aiogram поля пока нет, приходит как extra.
    return bool(getattr(member, "tag", None))


async def offer_ad(
    bot: Bot,
    app: App,
    chat_id: int,
    user_id: int,
    *,
    sender_tag: str | None = None,
    now: int | None = None,
) -> bool:
    """Человек что-то сделал в чате. Показывает рекламу, если пора. True — если отправили."""
    if sender_tag:  # у автора сообщения есть тег или звание
        return False
    ads = await app.db.get_ads()
    if not ads_for_members(ads):
        return False
    chat = await app.db.get_chat(chat_id)
    if chat is None or not chat["active"] or not int(chat["moderation_enabled"]):
        return False
    moment = int(time.time()) if now is None else now
    day = msk_day(moment)
    # Дешёвая проверка лимита до запроса в Telegram.
    if not await app.db.ad_allowed(user_id, day=day, now=moment, limit=ADS_PER_DAY, gap=AD_MIN_GAP):
        return False
    if await app.db.get_pending(chat_id, user_id):
        return False
    if await ad_exempt(bot, app, chat_id, user_id):
        return False
    if not await app.db.claim_ad(user_id, day=day, now=moment, limit=ADS_PER_DAY, gap=AD_MIN_GAP):
        return False
    try:
        await send_ephemeral(
            bot,
            chat_id,
            user_id,
            ad_message_text(ads["text"]),
            ad_keyboard(ads),
            preview=True,
        )
    except TelegramAPIError as exc:
        logger.info("Реклама не ушла %s/%s: %s", chat_id, user_id, exc)
        return False
    return True


async def cleanup_public_ads(bot: Bot, app: App) -> int:
    """Удаляет публичные рекламные посты, которые бот публиковал раньше."""
    removed = 0
    try:
        for chat_id, message_id in await app.db.pop_posted_ads():
            if await delete_message(bot, chat_id, message_id):
                removed += 1
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Сбой при удалении старых рекламных постов")
    if removed:
        logger.info("Удалил старые рекламные посты: %s", removed)
    return removed
