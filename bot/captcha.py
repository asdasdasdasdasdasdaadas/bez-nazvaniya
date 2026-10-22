from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message, User

from bot.app import App
from bot.logic import (
    AD_EVERY_MESSAGES,
    AD_KEEP_MESSAGES,
    DEFAULT_WELCOME,
    ad_message_text,
    build_welcome_text,
)
from bot.ops import (
    ban_member,
    clear_restrictions,
    delete_message,
    ensure_chat,
    fade,
    is_chat_admin,
    kick_member,
    lock_member,
    mention,
    send_html,
    warn_rights_once,
)
from bot.panel import ad_keyboard, check_keyboard

logger = logging.getLogger(__name__)

FAIL_TEXT = {
    "kick": "Время вышло, вы исключены из чата. Можно войти снова и пройти проверку.",
    "ban": "Время вышло, доступ в чат закрыт.",
    "mute": "Время вышло. Написать можно будет, когда администратор снимет ограничение.",
}
FAIL_PUBLIC = {
    "kick": "кик",
    "ban": "бан",
    "mute": "мут",
}


def _ads_inside(ads: dict) -> bool:
    return bool(ads["enabled"] and ads["in_welcome"] and (ads["text"] or "").strip())


def _ads_separate(ads: dict) -> bool:
    return bool(ads["enabled"] and ads["separate"] and (ads["text"] or "").strip())


def check_body(chat: dict, user_name: str, username: str, ads: dict) -> str:
    inside = _ads_inside(ads)
    text = build_welcome_text(
        chat["welcome_text"] or DEFAULT_WELCOME,
        name=user_name,
        chat=chat["title"] or "чат",
        username=username,
        ad_text=ads["text"] if inside else None,
    )
    if len(text) > 3900:
        text = text[:3890] + "…"
    return text


async def begin_captcha(bot: Bot, app: App, chat, user: User) -> None:
    if user is None or user.is_bot:
        return
    row = await ensure_chat(bot, app.db, app.bot_id, chat)
    if row is None or not row["captcha_enabled"] or not int(row["moderation_enabled"]):
        return
    await app.db.forget_verified(chat.id, user.id)
    if await is_chat_admin(bot, chat.id, user.id):
        await app.db.mark_verified(chat.id, user.id)
        return

    deadline = int(time.time()) + int(row["captcha_timeout"])
    if not await app.db.add_pending(chat.id, user.id, deadline):
        return

    try:
        await lock_member(bot, chat.id, user.id)
    except TelegramBadRequest as exc:
        await app.db.drop_pending(chat.id, user.id)
        if "administrator" in str(exc).lower():
            await app.db.mark_verified(chat.id, user.id)
            return
        logger.warning("Не закрыл вход %s для %s: %s", chat.id, user.id, exc)
        await warn_rights_once(bot, chat.id)
        return

    ads = await app.db.get_ads()
    username = f"@{user.username}" if user.username else "без username"
    name = user.full_name or user.first_name or "участник"
    body = check_body(row, name, username, ads)
    try:
        lead = f"{mention(user.id, name)}, подтвердите, что вы человек."
        prompt = await send_html(
            bot,
            chat.id,
            f"{lead}\n\n{body}" if body else lead,
            check_keyboard(chat.id, user.id, ads, _ads_inside(ads)),
        )
    except TelegramAPIError:
        logger.warning("Не удалось показать проверку в %s", chat.id)
        await app.db.drop_pending(chat.id, user.id)
        try:
            await clear_restrictions(bot, chat.id, user.id)
        except TelegramBadRequest:
            pass
        return
    await app.db.set_captcha_messages(chat.id, user.id, prompt_id=prompt.message_id)


async def pass_captcha(bot: Bot, app: App, chat_id: int, user_id: int) -> str:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest:
        return "join"
    if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        return "join"
    if member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        pending = await app.db.drop_pending(chat_id, user_id)
        await app.db.mark_verified(chat_id, user_id)
        if pending and pending.get("prompt_message_id"):
            await delete_message(bot, chat_id, pending["prompt_message_id"])
        return "ok"

    now = int(time.time())
    verified = await app.db.get_verified(chat_id, user_id)
    pending = None
    just_passed = False
    if verified is None:
        pending = await app.db.finish_captcha(chat_id, user_id, success=True, now=now)
        if pending is None:
            return "late"
        just_passed = True
    elif member.status != ChatMemberStatus.RESTRICTED:
        return "already"

    try:
        await clear_restrictions(bot, chat_id, user_id)
    except TelegramBadRequest as exc:
        if "administrator" not in str(exc).lower():
            logger.warning("Не снял ограничение %s/%s: %s", chat_id, user_id, exc)
            return "rights"

    if pending and pending.get("prompt_message_id"):
        await delete_message(bot, chat_id, pending["prompt_message_id"])
    if just_passed:
        await app.db.add_log(
            chat_id,
            user_id,
            "verify",
            target_name=member.user.full_name,
        )
    return "ok"


async def fail_captcha(bot: Bot, app: App, chat_id: int, user_id: int) -> None:
    chat = await app.db.get_chat(chat_id)
    if chat is not None and not int(chat["moderation_enabled"]):
        pending = await app.db.drop_pending(chat_id, user_id)
        if pending is None:
            return
        if pending.get("prompt_message_id"):
            await delete_message(bot, chat_id, pending["prompt_message_id"])
        try:
            await clear_restrictions(bot, chat_id, user_id)
        except TelegramBadRequest:
            pass
        return

    now = int(time.time())
    pending = await app.db.finish_captcha(chat_id, user_id, success=False, now=now)
    if pending is None or await app.db.get_verified(chat_id, user_id):
        return
    chat = await app.db.get_chat(chat_id)
    mode = (chat or {}).get("captcha_fail") or "kick"
    try:
        if mode == "ban":
            await ban_member(bot, chat_id, user_id, 0)
        elif mode == "kick":
            await kick_member(bot, chat_id, user_id)
    except TelegramBadRequest as exc:
        logger.warning("Не наказал за просроченную проверку %s/%s: %s", chat_id, user_id, exc)

    if pending.get("prompt_message_id"):
        await delete_message(bot, chat_id, pending["prompt_message_id"])
    await fade(
        bot,
        chat_id,
        f"Участник не прошёл проверку: {FAIL_PUBLIC.get(mode, 'кик')}.",
        seconds=15,
    )
    await app.db.add_log(chat_id, user_id, "captcha_fail", reason=mode, target_name=str(user_id))


async def cancel_because_left(bot: Bot, app: App, chat_id: int, user_id: int) -> None:
    await app.db.forget_verified(chat_id, user_id)
    pending = await app.db.drop_pending(chat_id, user_id)
    if pending and pending.get("prompt_message_id"):
        await delete_message(bot, chat_id, pending["prompt_message_id"])


async def send_public_ad(bot: Bot, chat_id: int, ads: dict) -> int | None:
    if not (ads.get("text") or "").strip():
        return None
    try:
        message = await send_html(
            bot,
            chat_id,
            ad_message_text(ads["text"]),
            ad_keyboard(ads),
            preview=True,
        )
    except TelegramAPIError:
        logger.info("Реклама не ушла в чат %s", chat_id)
        return None
    return message.message_id


async def note_chat_message(bot: Bot, app: App, message: Message) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return
    if message.new_chat_members or message.left_chat_member:
        return
    bumped = await app.db.bump_chat_messages(message.chat.id, drop_after=AD_KEEP_MESSAGES)
    if bumped is None:
        return
    count, stale = bumped
    for message_id in stale:
        await delete_message(bot, message.chat.id, message_id)
    if count % AD_EVERY_MESSAGES != 0:
        return
    ads = await app.db.get_ads()
    if not _ads_separate(ads):
        return
    posted = await send_public_ad(bot, message.chat.id, ads)
    if posted is not None:
        await app.db.remember_posted_ad(message.chat.id, posted, count)


async def watch(bot: Bot, app: App) -> None:
    while True:
        try:
            now = int(time.time())
            for chat_id, user_id in await app.db.list_expired(now):
                await fail_captcha(bot, app, chat_id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой в таймере проверки")
        await asyncio.sleep(5)

