from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message, User

from bot.ads import offer_ad
from bot.app import App
from bot.logic import DEFAULT_WELCOME, build_welcome_text, msk_day
from bot.ops import (
    ban_member,
    clear_restrictions,
    delete_prompt,
    ensure_chat,
    fade,
    is_chat_admin,
    kick_member,
    lock_member,
    mention,
    send_ephemeral,
    send_html,
    warn_rights_once,
)
from bot.panel import check_keyboard

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


def check_body(chat: dict, user_name: str, username: str, ads: dict, *, with_ad: bool = True) -> str:
    inside = with_ad and _ads_inside(ads)
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
    lead = f"{mention(user.id, name)}, подтвердите, что вы человек."

    def compose(with_ad: bool):
        body = check_body(row, name, username, ads, with_ad=with_ad)
        text = f"{lead}\n\n{body}" if body else lead
        return text, check_keyboard(chat.id, user.id, ads, with_ad and _ads_inside(ads))

    if int(row.get("captcha_private", 1)):
        text, markup = compose(with_ad=True)
        try:
            ephemeral_id = await send_ephemeral(bot, chat.id, user.id, text, markup)
        except TelegramAPIError as exc:
            logger.warning("Эфемерная проверка не ушла в %s, показываю всем: %s", chat.id, exc)
            ephemeral_id = None
        if ephemeral_id:
            await app.db.set_captcha_messages(chat.id, user.id, ephemeral_id=ephemeral_id)
            if _ads_inside(ads):
                # Реклама в проверке тоже считается в дневной лимит показов.
                now = int(time.time())
                await app.db.record_ad_view(user.id, day=msk_day(now), now=now)
            return

    # Проверку видят все — рекламу в неё не кладём, её должен видеть только сам человек.
    text, markup = compose(with_ad=False)
    try:
        prompt = await send_html(bot, chat.id, text, markup)
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
        await delete_prompt(bot, chat_id, user_id, pending)
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

    await delete_prompt(bot, chat_id, user_id, pending)
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
        await delete_prompt(bot, chat_id, user_id, pending)
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

    await delete_prompt(bot, chat_id, user_id, pending)
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
    await delete_prompt(bot, chat_id, user_id, pending)


async def note_chat_message(bot: Bot, app: App, message: Message) -> None:
    """Любое сообщение или правка человека в чате — повод показать ему рекламу."""
    user = message.from_user
    if user is None or user.is_bot:
        return
    if message.new_chat_members or message.left_chat_member:
        return
    await offer_ad(
        bot,
        app,
        message.chat.id,
        user.id,
        sender_tag=getattr(message, "sender_tag", None),
    )


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

