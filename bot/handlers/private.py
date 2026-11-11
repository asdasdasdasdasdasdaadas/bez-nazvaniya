from __future__ import annotations

import time
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.app import App
from bot.captcha import pass_captcha
from bot.logic import CYCLES, SPAM_KEYS, TOGGLES, next_in_cycle, split_payload
from bot.ops import (
    EMPTY_KEYBOARD,
    PREVIEW_OFF,
    clear_restrictions,
    delete_message,
    delete_prompt,
    edit_html,
    fade,
    is_chat_admin,
)
from bot.panel import (
    cancel_keyboard,
    commands_text,
    help_text,
    owner_keyboard,
    owner_text,
    spam_keyboard,
    spam_text,
    welcome_prompt,
)
from bot.samples import send_member_preview
from bot.screens import render_chat, render_home
from bot.states import Edit

router = Router()

WELCOME_LIMIT = 3500


def _single(data: str) -> int | None:
    _, _, raw = data.partition(":")
    try:
        return int(raw)
    except ValueError:
        return None


def _pair(data: str) -> tuple[int, str] | None:
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


async def _show(message: Message, text: str, markup, *, edit: bool) -> None:
    if edit:
        try:
            await edit_html(message, text, markup)
        except TelegramBadRequest:
            await message.answer(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                link_preview_options=PREVIEW_OFF,
            )
        return
    await message.answer(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        link_preview_options=PREVIEW_OFF,
    )


async def _guard(callback: CallbackQuery, bot, chat_id: int) -> bool:
    if await is_chat_admin(bot, chat_id, callback.from_user.id):
        return True
    await callback.answer("Только для админов этого чата", show_alert=True)
    return False


async def _redraw(callback: CallbackQuery, app: App, bot, chat_id: int, key: str) -> None:
    if key in SPAM_KEYS:
        chat = await app.db.get_chat(chat_id)
        if chat is None:
            return
        await _show(callback.message, spam_text(chat), spam_keyboard(chat), edit=True)
        return
    rendered = await render_chat(app, bot, chat_id)
    if rendered:
        await _show(callback.message, rendered[0], rendered[1], edit=True)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(message: Message, command: CommandObject, state: FSMContext, app: App, bot) -> None:
    await state.clear()
    parsed = split_payload(command.args)
    if parsed is None:
        text, markup = await render_home(app, message.from_user.id)
        await _show(message, text, markup, edit=False)
        return
    kind, chat_id = parsed
    if kind == "v":
        await message.answer("Проверка проходит в чате: нажмите там «Я не робот».")
        return
    if not await is_chat_admin(bot, chat_id, message.from_user.id):
        await message.answer("Настройки этого чата доступны его администраторам.")
        return
    await app.db.add_staff(chat_id, message.from_user.id)
    rendered = await render_chat(app, bot, chat_id)
    if rendered is None:
        await message.answer("Чат не подключён. Добавьте бота заново.")
        return
    await _show(message, rendered[0], rendered[1], edit=False)


@router.message(Command("help"), F.chat.type == ChatType.PRIVATE)
async def help_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(help_text(), parse_mode=ParseMode.HTML, link_preview_options=PREVIEW_OFF)


@router.callback_query(F.data == "m")
async def home(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    await state.clear()
    text, markup = await render_home(app, callback.from_user.id)
    await callback.answer()
    await _show(callback.message, text, markup, edit=True)


@router.callback_query(F.data == "help")
async def help_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    from aiogram.types import InlineKeyboardMarkup

    from bot.panel import btn

    markup = InlineKeyboardMarkup(inline_keyboard=[[btn("В меню", "m")]])
    await callback.answer()
    await _show(callback.message, help_text(), markup, edit=True)


@router.callback_query(F.data.startswith("c:"))
async def open_chat(callback: CallbackQuery, state: FSMContext, app: App, bot) -> None:
    await state.clear()
    chat_id = _single(callback.data)
    if chat_id is None:
        await callback.answer()
        return
    if not await _guard(callback, bot, chat_id):
        return
    await app.db.add_staff(chat_id, callback.from_user.id)
    rendered = await render_chat(app, bot, chat_id)
    if rendered is None:
        await callback.answer("Чат не подключён", show_alert=True)
        return
    await callback.answer()
    await _show(callback.message, rendered[0], rendered[1], edit=True)


@router.callback_query(F.data.startswith("s:"))
async def open_spam(callback: CallbackQuery, state: FSMContext, app: App, bot) -> None:
    await state.clear()
    chat_id = _single(callback.data)
    if chat_id is None or not await _guard(callback, bot, chat_id):
        return
    chat = await app.db.get_chat(chat_id)
    if chat is None:
        await callback.answer("Чат не подключён", show_alert=True)
        return
    await callback.answer()
    await _show(callback.message, spam_text(chat), spam_keyboard(chat), edit=True)


@router.callback_query(F.data.startswith("h:"))
async def open_commands(callback: CallbackQuery, state: FSMContext, app: App, bot) -> None:
    await state.clear()
    chat_id = _single(callback.data)
    if chat_id is None or not await _guard(callback, bot, chat_id):
        return
    from aiogram.types import InlineKeyboardMarkup

    from bot.panel import btn

    markup = InlineKeyboardMarkup(inline_keyboard=[[btn("К чату", f"c:{chat_id}")]])
    await callback.answer()
    await _show(callback.message, commands_text(), markup, edit=True)


@router.callback_query(F.data.startswith("t:"))
async def toggle(callback: CallbackQuery, state: FSMContext, app: App, bot) -> None:
    await state.clear()
    parsed = _pair(callback.data)
    if parsed is None:
        await callback.answer()
        return
    chat_id, key = parsed
    if key not in TOGGLES or not await _guard(callback, bot, chat_id):
        return
    chat = await app.db.get_chat(chat_id)
    if chat is None:
        await callback.answer("Чат не подключён", show_alert=True)
        return
    field = TOGGLES[key]
    new = 0 if chat[field] else 1
    await app.db.update_chat(chat_id, **{field: new})
    if key == "mod":
        await callback.answer("Модерация включена" if new else "Модерация выключена")
    elif key == "prv":
        await callback.answer(
            "Проверку видит только новичок" if new else "Проверку видит весь чат"
        )
    else:
        await callback.answer("Теперь: вкл" if new else "Теперь: выкл")
    if new == 0 and key in {"cap", "mod"}:
        await _release_pending(bot, app, chat_id)
    if key == "mod":
        group_text = (
            "Модерация снова включена."
            if new
            else "Модерация приостановлена. Проверка и антиспам молчат, настройки сохранены."
        )
        try:
            await fade(bot, chat_id, group_text, 25)
        except TelegramAPIError:
            pass
    await _redraw(callback, app, bot, chat_id, key)


async def _release_pending(bot, app: App, chat_id: int) -> None:
    rows = await app.db.cancel_chat_pending(chat_id, int(time.time()))
    for row in rows:
        await delete_prompt(bot, chat_id, row["user_id"], row)
        try:
            await clear_restrictions(bot, chat_id, row["user_id"])
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("y:"))
async def cycle(callback: CallbackQuery, state: FSMContext, app: App, bot) -> None:
    await state.clear()
    parsed = _pair(callback.data)
    if parsed is None:
        await callback.answer()
        return
    chat_id, key = parsed
    if key not in CYCLES or not await _guard(callback, bot, chat_id):
        return
    chat = await app.db.get_chat(chat_id)
    if chat is None:
        await callback.answer("Чат не подключён", show_alert=True)
        return
    field, options = CYCLES[key]
    new = next_in_cycle(options, chat[field])
    await app.db.update_chat(chat_id, **{field: new})
    await callback.answer("Сохранено")
    await _redraw(callback, app, bot, chat_id, key)


@router.callback_query(F.data.startswith("w:"))
async def ask_welcome(callback: CallbackQuery, state: FSMContext, app: App, bot) -> None:
    chat_id = _single(callback.data)
    if chat_id is None or not await _guard(callback, bot, chat_id):
        return
    chat = await app.db.get_chat(chat_id)
    if chat is None:
        await callback.answer("Чат не подключён", show_alert=True)
        return
    await state.set_state(Edit.welcome)
    await state.update_data(chat_id=chat_id, back="chat", prompt_id=callback.message.message_id)
    await callback.answer()
    ads = await app.db.get_ads()
    user = callback.from_user
    inside = bool(ads["enabled"] and ads["in_welcome"] and (ads["text"] or "").strip())
    prompt = welcome_prompt(
        chat["welcome_text"],
        name=user.full_name or "Имя",
        chat=chat["title"] or "чат",
        username=f"@{user.username}" if user.username else "без username",
        ad_text=ads["text"] if inside else None,
    )
    await _show(callback.message, prompt, cancel_keyboard(), edit=True)


@router.callback_query(F.data.startswith("p:"))
async def preview_chat(callback: CallbackQuery, app: App, bot) -> None:
    chat_id = _single(callback.data)
    if chat_id is None or not await _guard(callback, bot, chat_id):
        return
    chat = await app.db.get_chat(chat_id)
    if chat is None:
        await callback.answer("Чат не подключён", show_alert=True)
        return
    await callback.answer()
    await send_member_preview(
        bot,
        callback.from_user,
        title=chat["title"] or "чат",
        template=chat["welcome_text"],
        ads=await app.db.get_ads(),
        fail_mode=chat["captcha_fail"],
        spam_action=chat["spam_action"],
        spam_mute_seconds=int(chat["spam_mute_seconds"]),
        captcha_enabled=bool(chat["captcha_enabled"]),
    )


def _captcha_ids(data: str) -> tuple[int, int] | None:
    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


@router.callback_query(F.data.startswith("vr:"))
async def verify(callback: CallbackQuery, app: App, bot) -> None:
    parsed = _captcha_ids(callback.data)
    if parsed is None:
        await callback.answer()
        return
    chat_id, user_id = parsed
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для вас.", show_alert=True)
        return
    result = await pass_captcha(bot, app, chat_id, callback.from_user.id)
    alerts = {
        "join": "Сначала вступите в чат.",
        "late": "Время вышло. Вступите в чат ещё раз.",
        "already": "Вы уже прошли проверку.",
        "rights": "Не смог снять ограничение. Попросите админа написать /unmute.",
    }
    if result != "ok":
        await callback.answer(alerts.get(result, "Не получилось."), show_alert=True)
        return
    chat = await app.db.get_chat(chat_id)
    title = escape((chat or {}).get("title") or "чат")
    await callback.answer("Можно писать.")
    if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        return
    try:
        await edit_html(
            callback.message,
            f"Проверка пройдена. Можно писать в «{title}».",
            EMPTY_KEYBOARD,
        )
    except TelegramBadRequest:
        return


@router.callback_query(F.data == "x")
async def cancel(callback: CallbackQuery, state: FSMContext, app: App, bot) -> None:
    data = await state.get_data()
    await state.clear()
    if data.get("back") == "owner":
        if callback.from_user.id != app.owner_id:
            await callback.answer("Недоступно", show_alert=True)
            return
        ads = await app.db.get_ads()
        await callback.answer("Отменено")
        await _show(callback.message, owner_text(ads), owner_keyboard(ads), edit=True)
        return
    chat_id = data.get("chat_id")
    if isinstance(chat_id, int):
        rendered = await render_chat(app, bot, chat_id)
        if rendered:
            await callback.answer("Отменено")
            await _show(callback.message, rendered[0], rendered[1], edit=True)
            return
    text, markup = await render_home(app, callback.from_user.id)
    await callback.answer("Отменено")
    await _show(callback.message, text, markup, edit=True)


@router.message(Edit.welcome, F.chat.type == ChatType.PRIVATE)
async def save_welcome(message: Message, state: FSMContext, app: App, bot) -> None:
    if not message.text:
        await message.answer("Пришлите текст или нажмите «Отмена».")
        return
    if message.text.startswith("/"):
        return
    data = await state.get_data()
    chat_id = data.get("chat_id")
    if not isinstance(chat_id, int):
        await state.clear()
        await message.answer("Не понял, для какого чата текст. Откройте /start.")
        return
    if not await is_chat_admin(bot, chat_id, message.from_user.id):
        await state.clear()
        await message.answer("Вы уже не админ этого чата.")
        return
    incoming = message.text.strip()
    if incoming == "-":
        await app.db.update_chat(chat_id, welcome_text=None)
    elif len(incoming) > WELCOME_LIMIT:
        await message.answer(f"Слишком длинное. Максимум {WELCOME_LIMIT} символов.")
        return
    else:
        await app.db.update_chat(chat_id, welcome_text=incoming)
    prompt_id = data.get("prompt_id")
    await state.clear()
    rendered = await render_chat(app, bot, chat_id)
    if rendered and prompt_id:
        try:
            await bot.edit_message_text(
                rendered[0],
                chat_id=message.chat.id,
                message_id=prompt_id,
                reply_markup=rendered[1],
                parse_mode=ParseMode.HTML,
                link_preview_options=PREVIEW_OFF,
            )
        except TelegramBadRequest:
            await _show(message, rendered[0], rendered[1], edit=False)
    elif rendered:
        await _show(message, rendered[0], rendered[1], edit=False)
    await message.answer("Сохранено.")


@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def fallback(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        return
    await message.answer("Нажмите /start, чтобы открыть меню.")
