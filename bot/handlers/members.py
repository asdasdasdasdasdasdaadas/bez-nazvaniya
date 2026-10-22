from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatMemberUpdated, InlineKeyboardMarkup, Message

from bot.app import App
from bot.captcha import begin_captcha, cancel_because_left
from bot.logic import pack_chat_id
from bot.ops import delete_message, missing_rights, send_html, sync_staff
from bot.panel import add_to_group_url, panel_url, url_btn

logger = logging.getLogger(__name__)
router = Router()

LEFT = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
JOINED = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.RESTRICTED,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
}
ADMIN = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
PRIVACY = (
    "Чтобы антиспам видел обычные сообщения, в @BotFather выполните "
    "/setprivacy → Disable. Если бот уже был в группе, удалите его и добавьте снова."
)


def _kind(chat) -> str:
    return str(getattr(chat.type, "value", chat.type))


@router.my_chat_member()
async def on_my_membership(event: ChatMemberUpdated, app: App, bot) -> None:
    chat = event.chat
    app.admins.drop(chat.id)
    if chat.type == ChatType.CHANNEL:
        await _tell_channel(event, bot)
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    new = event.new_chat_member.status
    old = event.old_chat_member.status
    if new in LEFT:
        await app.db.update_chat(chat.id, active=0)
        return

    adder = event.from_user
    added_by = adder.id if adder and not adder.is_bot else None
    await app.db.upsert_chat(chat.id, chat.title or "", _kind(chat), added_by)
    if new in ADMIN:
        await sync_staff(bot, app.db, chat.id, added_by)
        if old not in ADMIN:
            await _notify_ready(event, app, bot)
        return
    await _notify_need_admin(event, app, bot)


async def _tell_channel(event: ChatMemberUpdated, bot) -> None:
    text = (
        "В канале я не модерирую посты. Для комментариев добавьте меня "
        "администратором в группу обсуждения и отключите Privacy Mode в @BotFather."
    )
    user = event.from_user
    if user and not user.is_bot:
        try:
            await send_html(bot, user.id, text)
            return
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    try:
        await send_html(bot, event.chat.id, text)
    except TelegramAPIError:
        logger.info("Не смог объяснить ограничение канала %s", event.chat.id)


async def _notify_ready(event: ChatMemberUpdated, app: App, bot) -> None:
    chat = event.chat
    try:
        missing = await missing_rights(bot, chat.id, app.bot_id)
    except TelegramBadRequest:
        missing = ["не удалось проверить права"]
    title = escape(chat.title or "чат")
    lines = [f"Чат «{title}» подключён."]
    if missing:
        lines.append("Пока не хватает прав: " + ", ".join(missing) + ".")
    lines.extend([
        "",
        "В настройках: проверка «Я не робот», антиспам и текст приветствия.",
        "В группе: /mute /ban /kick /unmute /unban /warn /settings.",
        "",
        PRIVACY,
    ])
    text = "\n".join(lines)
    buttons = [[url_btn("Открыть настройки", panel_url(app.bot_username, pack_chat_id(chat.id)))]]
    if missing:
        buttons.append([url_btn("Выдать права", add_to_group_url(app.bot_username))])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    delivered = False
    user = event.from_user
    if user and not user.is_bot:
        try:
            await send_html(bot, user.id, text, markup)
            delivered = True
        except (TelegramForbiddenError, TelegramBadRequest):
            delivered = False
    if delivered:
        return
    try:
        await send_html(bot, chat.id, text, markup)
    except TelegramAPIError:
        logger.warning("Не смог сообщить о подключении в %s", chat.id)


async def _notify_need_admin(event: ChatMemberUpdated, app: App, bot) -> None:
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[url_btn("Сделать администратором", add_to_group_url(app.bot_username))]]
    )
    try:
        await send_html(
            bot,
            event.chat.id,
            "Я в группе, но без прав администратора проверка, мут и бан не работают.",
            markup,
        )
    except TelegramAPIError:
        logger.info("Не смог попросить права в %s", event.chat.id)


@router.chat_member()
async def on_member(event: ChatMemberUpdated, app: App, bot) -> None:
    if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    app.admins.drop(event.chat.id)
    if new in ADMIN:
        await app.db.add_staff(event.chat.id, user.id)
    elif old in ADMIN:
        await app.db.remove_staff(event.chat.id, user.id)
    if old in LEFT and new in JOINED:
        await begin_captcha(bot, app, event.chat, user)
    elif old in JOINED and new in LEFT:
        await cancel_because_left(bot, app, event.chat.id, user.id)


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.new_chat_members | F.left_chat_member,
)
async def on_service(message: Message, app: App, bot) -> None:
    row = await app.db.get_chat(message.chat.id)
    if row and row["active"] and int(row["moderation_enabled"]) and row["delete_service"]:
        await delete_message(bot, message.chat.id, message.message_id)
    for user in message.new_chat_members or []:
        await begin_captcha(bot, app, message.chat, user)
    if message.left_chat_member and not message.left_chat_member.is_bot:
        await cancel_because_left(bot, app, message.chat.id, message.left_chat_member.id)
