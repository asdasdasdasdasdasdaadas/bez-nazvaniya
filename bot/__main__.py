from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    ErrorEvent,
    Message,
)
from aiogram import BaseMiddleware

from bot.ads import cleanup_public_ads
from bot.app import App
from bot.captcha import note_chat_message, watch
from bot.config import Settings, load_settings
from bot.db import Database
from bot.handlers import setup
from bot.logic import SpamTracker
from bot.ops import AdminCache

logger = logging.getLogger(__name__)


class InjectMiddleware(BaseMiddleware):
    def __init__(self, app: App) -> None:
        self.app = app

    async def __call__(self, handler, event, data):
        data["app"] = self.app
        return await handler(event, data)


class AdActionMiddleware(BaseMiddleware):
    """После обработки сообщения или правки в группе — повод показать автору рекламу."""

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        message = getattr(event, "message", None) or getattr(event, "edited_message", None)
        if not isinstance(message, Message):
            return result
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return result
        app = data.get("app")
        bot = data.get("bot")
        if app is None or bot is None:
            return result
        try:
            await note_chat_message(bot, app, message)
        except Exception:
            logger.exception("Не показал рекламу по сообщению")
        return result


async def _commands(bot: Bot, owner_id: int) -> None:
    private = [
        BotCommand(command="start", description="Меню бота"),
        BotCommand(command="help", description="Как пользоваться"),
    ]
    admin = [
        BotCommand(command="settings", description="Настройки модерации"),
        BotCommand(command="mute", description="Мут: ответом или @user"),
        BotCommand(command="ban", description="Бан: ответом или @user"),
        BotCommand(command="kick", description="Исключить из чата"),
        BotCommand(command="unmute", description="Снять мут"),
        BotCommand(command="unban", description="Разбанить"),
        BotCommand(command="warn", description="Предупреждение"),
        BotCommand(command="log", description="Журнал наказаний"),
    ]
    await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(admin, scope=BotCommandScopeAllChatAdministrators())
    try:
        await bot.set_my_commands(
            private + [BotCommand(command="owner", description="Реклама")],
            scope=BotCommandScopeChat(chat_id=owner_id),
        )
    except TelegramBadRequest:
        logger.info("Команда /owner появится после первого сообщения создателя боту")


async def _on_error(event: ErrorEvent) -> None:
    logger.exception("Сбой обработчика", exc_info=event.exception)
    callback = event.update.callback_query
    if callback is None:
        return
    try:
        await callback.answer("Не получилось. Попробуйте ещё раз.", show_alert=True)
    except TelegramAPIError:
        return


async def run(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db = Database(settings.db_path)
    await db.connect()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
        if not me.username:
            raise SystemExit("У бота нет username. Задайте его в @BotFather.")
        app = App(
            db=db,
            owner_id=settings.owner_id,
            bot_username=me.username,
            bot_id=me.id,
            tracker=SpamTracker(),
            admins=AdminCache(),
        )
        dp = Dispatcher(storage=MemoryStorage())
        dp.update.middleware(InjectMiddleware(app))
        dp.update.middleware(AdActionMiddleware())
        setup(dp)
        dp.error.register(_on_error)
        watcher = asyncio.create_task(watch(bot, app))
        cleanup = asyncio.create_task(cleanup_public_ads(bot, app))
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await _commands(bot, settings.owner_id)
            logger.info("Бот @%s запущен", me.username)
            await dp.start_polling(
                bot,
                allowed_updates=[
                    "message",
                    "edited_message",
                    "callback_query",
                    "my_chat_member",
                    "chat_member",
                    "message_reaction",
                ],
            )
        finally:
            for task in (watcher, cleanup):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    finally:
        await db.close()
        await bot.session.close()


def main() -> None:
    settings = load_settings()
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
