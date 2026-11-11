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


def _ads_personal(ads: dict) -> bool:
    return bool(ads["enabled"] and ads.get("personal") and (ads["text"] or "").strip())


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
    lead = f"{mention(user.id, name)}, подтвердите, что вы человек."
    text = f"{lead}\n\n{body}" if body else lead
    markup = check_keyboard(chat.id, user.id, ads, _ads_inside(ads))

    if int(row.get("captcha_private", 1)):
        try:
            ephemeral_id = await send_ephemeral(bot, chat.id, user.id, text, markup)
        except TelegramAPIError as exc:
            logger.warning("Эфемерная проверка не ушла в %s, показываю всем: %s", chat.id, exc)
            ephemeral_id = None
        if ephemeral_id:
            await app.db.set_captcha_messages(chat.id, user.id, ephemeral_id=ephemeral_id)
            return

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
    ads = await app.db.get_ads()
    await send_personal_ad(bot, app, message, ads)
    if count % AD_EVERY_MESSAGES != 0:
        return
    if not _ads_separate(ads):
        return
    posted = await send_public_ad(bot, message.chat.id, ads)
    if posted is not None:
        await app.db.remember_posted_ad(message.chat.id, posted, count)


async def send_personal_ad(bot: Bot, app: App, message: Message, ads: dict) -> None:
    """Реклама автору сообщения. Видит только он, остальные в чате — нет."""
    if (message.text or "").startswith("/"):
        return
    await offer_personal_ad(
        bot,
        app,
        message.chat.id,
        message.from_user.id,
        ads,
        sender_tag=getattr(message, "sender_tag", None),
    )


async def ad_exempt(bot: Bot, app: App, chat_id: int, user_id: int) -> bool:
    """Админам и участникам с тегом личную рекламу не показываем."""
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


async def offer_personal_ad(
    bot: Bot,
    app: App,
    chat_id: int,
    user_id: int,
    ads: dict,
    *,
    sender_tag: str | None = None,
) -> bool:
    """Показывает рекламу человеку в чате эфемерно, если пора. True — если отправили."""
    if not _ads_personal(ads):
        return False
    if sender_tag:  # у автора сообщения есть тег или звание
        return False
    # Сначала отметка паузы: так лишний раз не спрашиваем Telegram про участника.
    if not await app.db.claim_personal_ad(
        chat_id,
        user_id,
        now=int(time.time()),
        cooldown=int(ads.get("personal_cooldown") or 0),
    ):
        return False
    if await ad_exempt(bot, app, chat_id, user_id):
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
        logger.info("Личная реклама не ушла в %s: %s", chat_id, exc)
        return False
    return True


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

