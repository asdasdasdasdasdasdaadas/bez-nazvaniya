from __future__ import annotations

import asyncio
import logging
import time
from html import escape

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.methods.base import TelegramMethod
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from bot.db import Database

logger = logging.getLogger(__name__)

PREVIEW_OFF = LinkPreviewOptions(is_disabled=True)
PREVIEW_ON = LinkPreviewOptions(is_disabled=False)
EMPTY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[])
MUTED = ChatPermissions(can_send_messages=False)

_background: set[asyncio.Task] = set()
_rights_warned: dict[int, float] = {}


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def mention(user_id: int, name: str) -> str:
    safe = escape(name or "участник")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def explain_telegram_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "not enough rights" in text or "chat_admin_required" in text:
        return (
            "Не хватает прав. Сделайте бота администратором "
            "с правом удалять сообщения и ограничивать участников."
        )
    if "supergroup" in text:
        return (
            "Мут работает в супергруппах. В настройках группы включите "
            "«история чата для новых участников» — Telegram сам превратит группу в супергруппу. "
            "Бан при этом доступен и в обычной группе."
        )
    if "administrator" in text or "chat owner" in text:
        return "Нельзя наказать администратора или владельца чата."
    if "user_not_participant" in text or "user not found" in text:
        return "Этого человека нет в чате."
    return "Telegram не дал выполнить действие. Проверьте права бота."


class AdminCache:
    def __init__(self) -> None:
        self._items: dict[int, tuple[float, set[int]]] = {}

    def drop(self, chat_id: int) -> None:
        self._items.pop(chat_id, None)

    async def ids(self, bot: Bot, chat_id: int) -> set[int] | None:
        now = time.monotonic()
        cached = self._items.get(chat_id)
        if cached and now - cached[0] < 45:
            return cached[1]
        try:
            admins = await bot.get_chat_administrators(chat_id)
        except TelegramBadRequest:
            logger.warning("Не удалось получить админов чата %s", chat_id)
            return None
        found = {item.user.id for item in admins}
        self._items[chat_id] = (now, found)
        return found


async def send_html(
    bot: Bot,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    preview: bool = False,
    disable_notification: bool = False,
) -> Message:
    return await bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        link_preview_options=PREVIEW_ON if preview else PREVIEW_OFF,
        disable_notification=disable_notification,
    )


class DeleteEphemeralMessage(TelegramMethod[bool]):
    """Bot API 10.2. В aiogram этого метода пока нет."""

    __returning__ = bool
    __api_method__ = "deleteEphemeralMessage"

    chat_id: int
    receiver_user_id: int
    ephemeral_message_id: int


async def send_ephemeral(
    bot: Bot,
    chat_id: int,
    user_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    preview: bool = False,
) -> int | None:
    """Сообщение в группе, которое видит только user_id. Возвращает ephemeral_message_id.

    Бот должен быть админом чата. Telegram не гарантирует доставку,
    а клиент может спрятать сообщение после перезапуска.
    """
    method = SendMessage(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        link_preview_options=PREVIEW_ON if preview else PREVIEW_OFF,
        ephemeral_message_parameters={"receiver_user_id": user_id},
    )
    sent = await bot(method)
    value = getattr(sent, "ephemeral_message_id", None)
    return int(value) if value else None


async def delete_ephemeral(bot: Bot, chat_id: int, user_id: int, ephemeral_id: int | None) -> None:
    if not ephemeral_id:
        return
    try:
        await bot(
            DeleteEphemeralMessage(
                chat_id=chat_id,
                receiver_user_id=user_id,
                ephemeral_message_id=ephemeral_id,
            )
        )
    except TelegramAPIError:
        return


async def delete_prompt(bot: Bot, chat_id: int, user_id: int, pending: dict | None) -> None:
    """Убирает сообщение проверки: обычное или эфемерное."""
    if not pending:
        return
    await delete_message(bot, chat_id, pending.get("prompt_message_id"))
    await delete_ephemeral(bot, chat_id, user_id, pending.get("ephemeral_id"))


async def edit_html(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            link_preview_options=PREVIEW_OFF,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        raise


async def delete_message(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        return


async def fade(bot: Bot, chat_id: int, text: str, seconds: int = 20) -> None:
    try:
        msg = await send_html(bot, chat_id, text, disable_notification=True)
    except TelegramBadRequest:
        return

    async def _later() -> None:
        await asyncio.sleep(seconds)
        await delete_message(bot, chat_id, msg.message_id)

    spawn(_later())


async def member_status(bot: Bot, chat_id: int, user_id: int):
    try:
        return await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest:
        return None


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await member_status(bot, chat_id, user_id)
    if member is None:
        return False
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


async def can_restrict_members(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await member_status(bot, chat_id, user_id)
    if member is None:
        return False
    if member.status == ChatMemberStatus.CREATOR:
        return True
    if member.status == ChatMemberStatus.ADMINISTRATOR:
        return bool(member.can_restrict_members)
    return False


async def missing_rights(bot: Bot, chat_id: int, bot_id: int) -> list[str]:
    member = await member_status(bot, chat_id, bot_id)
    if member is None:
        return ["бот не видит чат"]
    if member.status == ChatMemberStatus.CREATOR:
        return []
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return ["бот не администратор"]
    missing = []
    if not member.can_delete_messages:
        missing.append("удаление сообщений")
    if not member.can_restrict_members:
        missing.append("мут и бан")
    return missing


async def sync_staff(bot: Bot, db: Database, chat_id: int, extra_user_id: int | None = None) -> None:
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramBadRequest:
        if extra_user_id:
            await db.add_staff(chat_id, extra_user_id)
        return
    ids = [item.user.id for item in admins if not item.user.is_bot]
    if extra_user_id and extra_user_id not in ids:
        ids.append(extra_user_id)
    await db.replace_staff(chat_id, ids)


async def lock_member(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.restrict_chat_member(chat_id, user_id, permissions=MUTED)


async def clear_restrictions(bot: Bot, chat_id: int, user_id: int) -> None:
    chat = await bot.get_chat(chat_id)
    permissions = chat.permissions or ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )
    await bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=permissions,
        use_independent_chat_permissions=True,
    )


async def mute_member(bot: Bot, chat_id: int, user_id: int, seconds: int) -> None:
    kwargs = {}
    if seconds > 0:
        kwargs["until_date"] = int(time.time()) + seconds
    await bot.restrict_chat_member(chat_id, user_id, permissions=MUTED, **kwargs)


async def ban_member(bot: Bot, chat_id: int, user_id: int, seconds: int) -> None:
    kwargs = {}
    if seconds > 0:
        kwargs["until_date"] = int(time.time()) + seconds
    await bot.ban_chat_member(chat_id, user_id, **kwargs)


async def kick_member(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.ban_chat_member(chat_id, user_id)
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


async def unban_member(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


_misses: dict[int, float] = {}


async def ensure_chat(bot: Bot, db: Database, bot_id: int, chat) -> dict | None:
    row = await db.get_chat(chat.id)
    if row:
        return row if row["active"] else None
    now = time.monotonic()
    if now - _misses.get(chat.id, 0) < 300:
        return None
    me = await member_status(bot, chat.id, bot_id)
    if me is None or me.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        _misses[chat.id] = now
        return None
    kind = getattr(chat.type, "value", chat.type)
    await db.upsert_chat(chat.id, chat.title or "", str(kind), None)
    await sync_staff(bot, db, chat.id)
    _misses.pop(chat.id, None)
    return await db.get_chat(chat.id)


async def warn_rights_once(bot: Bot, chat_id: int) -> None:
    now = time.monotonic()
    if now - _rights_warned.get(chat_id, 0) < 3600:
        return
    _rights_warned[chat_id] = now
    try:
        await send_html(
            bot,
            chat_id,
            "Не могу ограничить нового участника. "
            "Выдайте боту право удалять сообщения и банить участников.",
        )
    except TelegramBadRequest:
        return
