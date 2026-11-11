from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.app import App
from bot.logic import OWNER_TOGGLES, valid_http_url
from bot.ops import PREVIEW_OFF, edit_html
from bot.panel import ad_prompt, button_editor_keyboard, button_prompt, owner_keyboard, owner_text
from bot.samples import send_member_preview
from bot.states import Edit

router = Router()
AD_LIMIT = 1000


def _owner(callback: CallbackQuery, app: App) -> bool:
    return callback.from_user.id == app.owner_id


async def _panel(message: Message, app: App, *, edit: bool, extra: str = "") -> None:
    ads = await app.db.get_ads()
    text = owner_text(ads) + extra
    if edit:
        try:
            await edit_html(message, text, owner_keyboard(ads))
            return
        except TelegramBadRequest:
            pass
    await message.answer(
        text,
        reply_markup=owner_keyboard(ads),
        parse_mode=ParseMode.HTML,
        link_preview_options=PREVIEW_OFF,
    )


@router.message(Command("owner"), F.chat.type == ChatType.PRIVATE)
async def owner_command(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    if message.from_user.id != app.owner_id:
        await message.answer("Эта панель только у создателя бота.")
        return
    await _panel(message, app, edit=False)


@router.callback_query(F.data == "o")
async def owner_screen(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    await state.clear()
    if not _owner(callback, app):
        await callback.answer("Только для создателя бота", show_alert=True)
        return
    await callback.answer()
    await _panel(callback.message, app, edit=True)


@router.callback_query(F.data.startswith("ot:"))
async def owner_toggle(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    await state.clear()
    if not _owner(callback, app):
        await callback.answer("Только для создателя бота", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key not in OWNER_TOGGLES:
        await callback.answer()
        return
    ads = await app.db.get_ads()
    field = OWNER_TOGGLES[key]
    new = 0 if ads[field] else 1
    await app.db.update_ads(**{field: new})
    await callback.answer("Теперь: вкл" if new else "Теперь: выкл")
    await _panel(callback.message, app, edit=True)


@router.callback_query(F.data == "ow")
async def ask_ad_text(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    if not _owner(callback, app):
        await callback.answer("Только для создателя бота", show_alert=True)
        return
    ads = await app.db.get_ads()
    await state.set_state(Edit.ad_text)
    await state.update_data(back="owner", prompt_id=callback.message.message_id)
    await callback.answer()
    try:
        await edit_html(callback.message, ad_prompt(ads["text"]), _cancel_only())
    except TelegramBadRequest:
        await callback.message.answer(ad_prompt(ads["text"]), reply_markup=_cancel_only())


@router.callback_query(F.data == "ob")
async def ask_button(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    if not _owner(callback, app):
        await callback.answer("Только для создателя бота", show_alert=True)
        return
    ads = await app.db.get_ads()
    await state.set_state(Edit.ad_button)
    await state.update_data(back="owner", prompt_id=callback.message.message_id)
    await callback.answer()
    markup = button_editor_keyboard(ads)
    try:
        await edit_html(callback.message, button_prompt(ads), markup)
    except TelegramBadRequest:
        await callback.message.answer(button_prompt(ads), reply_markup=markup)


def _cancel_only() -> InlineKeyboardMarkup:
    from bot.panel import cancel_keyboard

    return cancel_keyboard()


@router.callback_query(F.data == "op")
async def preview(callback: CallbackQuery, app: App, bot) -> None:
    if not _owner(callback, app):
        await callback.answer("Только для создателя бота", show_alert=True)
        return
    await callback.answer()
    ads = await app.db.get_ads()
    await send_member_preview(
        bot,
        callback.from_user,
        title="Тестовый чат",
        template=None,
        ads=ads,
        fail_mode="kick",
        spam_action="mute",
        spam_mute_seconds=3600,
        sample_chat=True,
    )


@router.callback_query(F.data == "preview")
async def preview_button(callback: CallbackQuery) -> None:
    await callback.answer("Это пример. В чат никого не пускает.", show_alert=True)


@router.message(Edit.ad_text, F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def save_ad_text(message: Message, state: FSMContext, app: App, bot) -> None:
    if message.from_user.id != app.owner_id:
        await state.clear()
        return
    incoming = message.text.strip()
    if incoming != "-" and len(incoming) > AD_LIMIT:
        await message.answer(f"Слишком длинное. Максимум {AD_LIMIT} символов.")
        return
    await app.db.update_ads(text="" if incoming == "-" else incoming)
    await _finish_edit(message, state, app, bot)


@router.message(Edit.ad_text, F.chat.type == ChatType.PRIVATE, ~F.text)
async def ad_text_not_text(message: Message) -> None:
    await message.answer("Пришлите текст или нажмите «Отмена».")


@router.message(Edit.ad_button, F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def save_button(message: Message, state: FSMContext, app: App, bot) -> None:
    if message.from_user.id != app.owner_id:
        await state.clear()
        return
    incoming = message.text.strip()
    if incoming == "-":
        await app.db.update_ads(button_text="", button_url="")
        await _finish_edit(message, state, app, bot)
        return
    if "|" not in incoming:
        await message.answer("Нужен формат: Текст кнопки | https://example.com")
        return
    label, url = incoming.split("|", 1)
    label = (label.strip() or "Открыть")[:40]
    url = url.strip()
    if not valid_http_url(url) or len(url) > 500:
        await message.answer("Ссылка должна начинаться с http:// или https://")
        return
    await app.db.update_ads(button_text=label, button_url=url)
    await _finish_edit(message, state, app, bot)


@router.message(Edit.ad_button, F.chat.type == ChatType.PRIVATE, ~F.text)
async def ad_button_not_text(message: Message) -> None:
    await message.answer("Пришлите текст или нажмите «Отмена».")


async def _finish_edit(message: Message, state: FSMContext, app: App, bot) -> None:
    data = await state.get_data()
    prompt_id = data.get("prompt_id")
    await state.clear()
    ads = await app.db.get_ads()
    if prompt_id:
        try:
            await bot.edit_message_text(
                owner_text(ads),
                chat_id=message.chat.id,
                message_id=prompt_id,
                reply_markup=owner_keyboard(ads),
                parse_mode=ParseMode.HTML,
                link_preview_options=PREVIEW_OFF,
            )
        except TelegramBadRequest:
            await _panel(message, app, edit=False)
    else:
        await _panel(message, app, edit=False)
    await message.answer("Сохранено.")
