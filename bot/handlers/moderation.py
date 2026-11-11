from __future__ import annotations

import time
from dataclasses import dataclass
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.app import App
from bot.logic import format_ts, human_seconds, parse_duration_prefix, parse_moderation_args
from bot.ops import (
    EMPTY_KEYBOARD,
    PREVIEW_OFF,
    ban_member,
    can_restrict_members,
    clear_restrictions,
    delete_message,
    delete_prompt,
    ensure_chat,
    explain_telegram_error,
    fade,
    is_chat_admin,
    kick_member,
    member_status,
    mention,
    mute_member,
    unban_member,
)
from bot.panel import panel_url, undo_keyboard, url_btn
from bot.logic import pack_chat_id
from bot.screens import render_chat

router = Router()
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

LOG_LABEL = {
    "mute": "мут",
    "ban": "бан",
    "kick": "кик",
    "warn": "варн",
    "unmute": "снял мут",
    "unban": "разбан",
    "verify": "проверка",
    "captcha_fail": "не прошёл проверку",
    "spam_mute": "мут за спам",
    "spam_ban": "бан за спам",
}


class Cmd(BaseFilter):
    def __init__(self, *names: str) -> None:
        self.names = {name.lower().lstrip("/") for name in names}

    async def __call__(self, message: Message):
        text = message.text or ""
        if not text.startswith("/"):
            return False
        head, _, rest = text.partition(" ")
        name = head[1:].split("@", 1)[0].lower()
        if name not in self.names:
            return False
        return {"args": rest.strip()}


def _anonymous(message: Message) -> bool:
    return message.sender_chat is not None and message.sender_chat.id == message.chat.id


@dataclass
class Actor:
    id: int | None
    name: str


@dataclass
class Target:
    id: int
    name: str
    username: str | None


def _normalize(seconds: int | None, default: int) -> int:
    value = default if seconds is None else seconds
    if 0 < value < 30:
        return 30
    return max(value, 0)


async def _actor(message: Message, bot: Bot, *, restrict: bool) -> tuple[Actor | None, str | None]:
    if _anonymous(message):
        return Actor(None, "анонимный админ"), None
    user = message.from_user
    if user is None or user.is_bot:
        return None, "Команду может отправить администратор."
    if restrict:
        allowed = await can_restrict_members(bot, message.chat.id, user.id)
        if not allowed:
            member = await member_status(bot, message.chat.id, user.id)
            if member and member.status == ChatMemberStatus.ADMINISTRATOR:
                return None, "У вашей роли нет права ограничивать участников."
            return None, "Эта команда только для админов."
    elif not await is_chat_admin(bot, message.chat.id, user.id):
        return None, "Эта команда только для админов."
    return Actor(user.id, user.full_name), None


async def _deny(bot: Bot, message: Message, text: str) -> None:
    await delete_message(bot, message.chat.id, message.message_id)
    await fade(bot, message.chat.id, text, seconds=8)


async def _target(message: Message, bot: Bot, args: str) -> tuple[Target | None, int | None, str, str | None]:
    if message.reply_to_message is not None:
        reply = message.reply_to_message
        seconds, used = parse_duration_prefix(args.split())
        reason = " ".join(args.split()[used:]).strip()
        user = reply.from_user
        if user is None or user.is_bot:
            return None, seconds, reason, "Ответьте на сообщение участника."
        return Target(user.id, user.full_name, user.username), seconds, reason, None

    token, seconds, reason = parse_moderation_args(args)
    if token is None:
        return None, seconds, reason, "Ответьте на сообщение участника или укажите @username."
    if token.startswith("@"):
        try:
            found = await bot.get_chat(token)
        except TelegramBadRequest:
            return None, seconds, reason, "Не нашёл такого пользователя. Ответьте на его сообщение."
        if found.type != ChatType.PRIVATE:
            return None, seconds, reason, "Это не пользователь."
        name = getattr(found, "full_name", None) or found.first_name or found.username or token
        return Target(found.id, name, found.username), seconds, reason, None
    user_id = int(token)
    member = await member_status(bot, message.chat.id, user_id)
    if member is not None:
        return Target(member.user.id, member.user.full_name, member.user.username), seconds, reason, None
    return Target(user_id, str(user_id), None), seconds, reason, None


async def _blocked(bot: Bot, chat_id: int, actor: Actor, target: Target) -> str | None:
    if actor.id is not None and actor.id == target.id:
        return "Себе эту команду применить нельзя."
    member = await member_status(bot, chat_id, target.id)
    if member is None:
        return None
    if member.user.is_bot:
        return "Нельзя наказать бота."
    if member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
        return "Нельзя наказать администратора."
    return None


async def _publish(bot: Bot, message: Message, text: str, markup=None) -> None:
    sent = False
    if message.reply_to_message is not None:
        try:
            await message.reply_to_message.reply(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            sent = True
        except TelegramBadRequest:
            sent = False
    if not sent:
        await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    await delete_message(bot, message.chat.id, message.message_id)


def _result(action: str, target: Target, seconds: int, reason: str, extra: str = "") -> str:
    who = mention(target.id, target.name)
    if action == "mute":
        span = "навсегда" if seconds <= 0 else f"на {human_seconds(seconds)}"
        text = f"🔇 {who} в муте {span}."
    elif action == "ban":
        span = "навсегда" if seconds <= 0 else f"на {human_seconds(seconds)}"
        text = f"⛔ {who} заблокирован {span}."
    elif action == "kick":
        text = f"👋 {who} исключён из чата."
    elif action == "unmute":
        text = f"🔊 С {who} снят мут."
    elif action == "unban":
        text = f"✅ {who} снова может войти в чат."
    else:
        text = who
    if reason:
        text += f"\nПричина: {escape(reason)}"
    return text + extra


async def _punish(message: Message, bot: Bot, app: App, args: str, action: str) -> None:
    actor, error = await _actor(message, bot, restrict=True)
    if error or actor is None:
        await _deny(bot, message, error or "Недостаточно прав.")
        return
    chat = await ensure_chat(bot, app.db, app.bot_id, message.chat)
    if chat is None:
        await _deny(bot, message, "Сначала сделайте бота администратором.")
        return
    if action not in {"unmute", "unban"} and not int(chat["moderation_enabled"]):
        await _deny(bot, message, "Модерация в этом чате выключена. Включить можно в /settings.")
        return
    target, seconds, reason, error = await _target(message, bot, args)
    if error or target is None:
        await _deny(bot, message, error or "Не понял, кого наказать.")
        return
    blocked = await _blocked(bot, message.chat.id, actor, target)
    if blocked and action not in {"unmute", "unban"}:
        await _deny(bot, message, blocked)
        return
    now = int(time.time())
    try:
        if action == "mute":
            seconds = _normalize(seconds, int(chat["spam_mute_seconds"]))
            await mute_member(bot, message.chat.id, target.id, seconds)
        elif action == "ban":
            seconds = _normalize(seconds, 0)
            await ban_member(bot, message.chat.id, target.id, seconds)
        elif action == "kick":
            seconds = 0
            await kick_member(bot, message.chat.id, target.id)
            pending = await app.db.drop_pending(message.chat.id, target.id)
            await delete_prompt(bot, message.chat.id, target.id, pending)
        elif action == "unmute":
            seconds = 0
            await clear_restrictions(bot, message.chat.id, target.id)
            pending = await app.db.drop_pending(message.chat.id, target.id)
            await app.db.mark_verified(message.chat.id, target.id)
            await delete_prompt(bot, message.chat.id, target.id, pending)
        elif action == "unban":
            seconds = 0
            await unban_member(bot, message.chat.id, target.id)
        else:
            return
    except TelegramBadRequest as exc:
        await _deny(bot, message, explain_telegram_error(exc))
        return
    until = None if seconds <= 0 else now + seconds
    await app.db.add_log(
        message.chat.id,
        target.id,
        action,
        admin_id=actor.id,
        until_ts=until,
        reason=reason or None,
        target_name=target.name,
    )
    markup = None
    if action == "mute":
        markup = undo_keyboard("mute", message.chat.id, target.id)
    elif action == "ban":
        markup = undo_keyboard("ban", message.chat.id, target.id)
    await _publish(bot, message, _result(action, target, seconds, reason), markup)


@router.message(Cmd("mute", "мут"))
async def mute_cmd(message: Message, bot: Bot, app: App, args: str) -> None:
    await _punish(message, bot, app, args, "mute")


@router.message(Cmd("ban", "бан"))
async def ban_cmd(message: Message, bot: Bot, app: App, args: str) -> None:
    await _punish(message, bot, app, args, "ban")


@router.message(Cmd("kick", "кик"))
async def kick_cmd(message: Message, bot: Bot, app: App, args: str) -> None:
    await _punish(message, bot, app, args, "kick")


@router.message(Cmd("unmute", "размут", "анмут"))
async def unmute_cmd(message: Message, bot: Bot, app: App, args: str) -> None:
    await _punish(message, bot, app, args, "unmute")


@router.message(Cmd("unban", "разбан"))
async def unban_cmd(message: Message, bot: Bot, app: App, args: str) -> None:
    await _punish(message, bot, app, args, "unban")


@router.message(Cmd("warn", "варн", "пред"))
async def warn_cmd(message: Message, bot: Bot, app: App, args: str) -> None:
    actor, error = await _actor(message, bot, restrict=True)
    if error or actor is None:
        await _deny(bot, message, error or "Недостаточно прав.")
        return
    chat = await ensure_chat(bot, app.db, app.bot_id, message.chat)
    if chat is None:
        await _deny(bot, message, "Сначала сделайте бота администратором.")
        return
    if not int(chat["moderation_enabled"]):
        await _deny(bot, message, "Модерация в этом чате выключена. Включить можно в /settings.")
        return
    if int(chat["warn_limit"]) <= 0:
        await _deny(bot, message, "Предупреждения выключены в настройках антиспама.")
        return
    target, _seconds, reason, error = await _target(message, bot, args)
    if error or target is None:
        await _deny(bot, message, error or "Не понял, кого предупредить.")
        return
    blocked = await _blocked(bot, message.chat.id, actor, target)
    if blocked:
        await _deny(bot, message, blocked)
        return
    count = await app.db.add_warning(message.chat.id, target.id)
    limit = int(chat["warn_limit"])
    who = mention(target.id, target.name)
    reason_line = f"\nПричина: {escape(reason)}" if reason else ""
    if count < limit:
        await app.db.add_log(
            message.chat.id,
            target.id,
            "warn",
            admin_id=actor.id,
            reason=reason or None,
            target_name=target.name,
        )
        await _publish(bot, message, f"⚠️ {who}: предупреждение {count}/{limit}.{reason_line}")
        return
    await app.db.reset_warnings(message.chat.id, target.id)
    follow = chat["warn_action"]
    seconds = int(chat["spam_mute_seconds"]) if follow == "mute" else 0
    try:
        if follow == "ban":
            await ban_member(bot, message.chat.id, target.id, 0)
        else:
            await mute_member(bot, message.chat.id, target.id, seconds)
    except TelegramBadRequest as exc:
        await _deny(bot, message, explain_telegram_error(exc))
        return
    action = "ban" if follow == "ban" else "mute"
    await app.db.add_log(
        message.chat.id,
        target.id,
        action,
        admin_id=actor.id,
        until_ts=None if seconds <= 0 else int(time.time()) + seconds,
        reason=reason or "лимит предупреждений",
        target_name=target.name,
    )
    extra = f"\nЛимит {limit}/{limit}, дальше { 'бан' if follow == 'ban' else 'мут' }."
    markup = undo_keyboard("ban" if follow == "ban" else "mute", message.chat.id, target.id)
    await _publish(bot, message, _result(action, target, seconds, reason) + extra, markup)


@router.message(Cmd("log", "лог", "журнал"))
async def log_cmd(message: Message, bot: Bot, app: App) -> None:
    _actor_row, error = await _actor(message, bot, restrict=False)
    if error:
        await _deny(bot, message, error)
        return
    rows = await app.db.recent_logs(message.chat.id, 15)
    if not rows:
        await _publish(bot, message, "Журнал пуст.")
        return
    lines = ["<b>Последние действия</b>"]
    for row in rows:
        name = escape(row["target_name"] or str(row["user_id"]))
        action = LOG_LABEL.get(row["action"], row["action"])
        extra = ""
        if row["until_ts"]:
            extra = f", до {format_ts(int(row['until_ts']))}"
        elif row["action"] in {"ban", "mute"} and not row["until_ts"]:
            extra = ", навсегда"
        reason = f" — {escape(row['reason'])}" if row["reason"] else ""
        lines.append(f"{format_ts(int(row['created_at']))} · {action} · {name}{extra}{reason}")
    await _publish(bot, message, "\n".join(lines))


@router.message(Cmd("settings", "настройки"))
@router.message(CommandStart())
async def settings_cmd(message: Message, bot: Bot, app: App) -> None:
    await _open_settings(message, bot, app)


@router.message(Cmd("help", "помощь"))
async def group_help(message: Message) -> None:
    await message.answer(
        "Я модератор этого чата.\n"
        "Новый участник нажимает «Я не робот» в чате.\n"
        "Админам: /settings — настройки, /mute /ban /kick /warn — наказания.",
        parse_mode=ParseMode.HTML,
    )


async def _open_settings(message: Message, bot: Bot, app: App) -> None:
    _actor_row, error = await _actor(message, bot, restrict=False)
    if error:
        await _deny(bot, message, error)
        return
    chat = await ensure_chat(bot, app.db, app.bot_id, message.chat)
    if chat is None:
        await message.answer("Сначала сделайте бота администратором: нужны удаление сообщений, мут и бан.")
        return
    if message.from_user and not message.from_user.is_bot:
        await app.db.add_staff(message.chat.id, message.from_user.id)
    url = panel_url(app.bot_username, pack_chat_id(message.chat.id))
    button = InlineKeyboardMarkup(inline_keyboard=[[url_btn("Открыть настройки", url)]])
    if _anonymous(message) or message.from_user is None or message.from_user.is_bot:
        await message.answer("Откройте панель кнопкой. Сначала напишите боту /start в личке.", reply_markup=button)
        return
    rendered = await render_chat(app, bot, message.chat.id)
    if rendered is None:
        await message.answer("Чат пока не подключён.", reply_markup=button)
        return
    try:
        await bot.send_message(
            message.from_user.id,
            rendered[0],
            reply_markup=rendered[1],
            parse_mode=ParseMode.HTML,
            link_preview_options=PREVIEW_OFF,
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer(
            "Не смог написать вам лично. Откройте бота через /start, затем нажмите кнопку.",
            reply_markup=button,
        )
        return
    await delete_message(bot, message.chat.id, message.message_id)
    await fade(bot, message.chat.id, "Открыл настройки в личных сообщениях.", 12)


def _undo_ids(data: str) -> tuple[str, int, int] | None:
    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        return parts[0], int(parts[1]), int(parts[2])
    except ValueError:
        return None


@router.callback_query(F.data.startswith("um:") | F.data.startswith("ub:"))
async def undo(callback: CallbackQuery, app: App, bot: Bot) -> None:
    parsed = _undo_ids(callback.data)
    if parsed is None:
        await callback.answer()
        return
    kind, chat_id, user_id = parsed
    if not await can_restrict_members(bot, chat_id, callback.from_user.id):
        await callback.answer("Только для админов с правом мута и бана", show_alert=True)
        return
    try:
        if kind == "um":
            await clear_restrictions(bot, chat_id, user_id)
            pending = await app.db.drop_pending(chat_id, user_id)
            await app.db.mark_verified(chat_id, user_id)
            await delete_prompt(bot, chat_id, user_id, pending)
            action = "unmute"
        else:
            await unban_member(bot, chat_id, user_id)
            action = "unban"
    except TelegramBadRequest as exc:
        await callback.answer(explain_telegram_error(exc), show_alert=True)
        return
    await app.db.add_log(
        chat_id,
        user_id,
        action,
        admin_id=callback.from_user.id,
        target_name=str(user_id),
    )
    await callback.answer("Готово")
    try:
        await callback.message.edit_reply_markup(reply_markup=EMPTY_KEYBOARD)
    except TelegramBadRequest:
        return
