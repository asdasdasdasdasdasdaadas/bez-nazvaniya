import asyncio
import json
import os
import sqlite3
import tempfile
from types import SimpleNamespace

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import Message

import bot.captcha as captcha
from bot.app import App
from bot.db import Database
from bot.logic import SpamTracker, cooldown_label
from bot.ops import AdminCache, DeleteEphemeralMessage


def _tmp_db() -> str:
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    return path


def test_old_database_gets_new_columns():
    path = _tmp_db()
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, title TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE pending (chat_id INTEGER, user_id INTEGER, prompt_message_id INTEGER,
                              deadline INTEGER NOT NULL, PRIMARY KEY (chat_id, user_id));
        CREATE TABLE ads (id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL DEFAULT 0,
                          in_welcome INTEGER NOT NULL DEFAULT 1, separate INTEGER NOT NULL DEFAULT 0,
                          text TEXT NOT NULL DEFAULT '', button_text TEXT NOT NULL DEFAULT '',
                          button_url TEXT NOT NULL DEFAULT '', every_n INTEGER NOT NULL DEFAULT 1);
        INSERT INTO chats (chat_id, title, created_at) VALUES (-1, 'old', 0);
        """
    )
    raw.commit()
    raw.close()

    async def scenario():
        db = Database(path)
        await db.connect()
        try:
            chat = await db.get_chat(-1)
            assert chat["captcha_private"] == 1
            assert chat["moderation_enabled"] == 1
            ads = await db.get_ads()
            assert ads["personal"] == 0
            assert ads["personal_cooldown"] == 21600
            await db.add_pending(-1, 5, 100)
            await db.set_captcha_messages(-1, 5, ephemeral_id=42)
            assert (await db.get_pending(-1, 5))["ephemeral_id"] == 42
        finally:
            await db.close()

    asyncio.run(scenario())


def test_personal_ad_cooldown():
    async def scenario():
        db = Database(_tmp_db())
        await db.connect()
        try:
            assert await db.claim_personal_ad(-1, 5, now=1000, cooldown=3600)
            assert not await db.claim_personal_ad(-1, 5, now=2000, cooldown=3600)
            assert await db.claim_personal_ad(-1, 6, now=2000, cooldown=3600)
            assert await db.claim_personal_ad(-2, 5, now=2000, cooldown=3600)
            assert await db.claim_personal_ad(-1, 5, now=4600, cooldown=3600)
            assert await db.claim_personal_ad(-1, 5, now=4600, cooldown=0)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_cooldown_label():
    assert cooldown_label(0) == "после каждого сообщения"
    assert cooldown_label(3600) == "раз в час"
    assert cooldown_label(21600) == "раз в 6 часов"
    assert cooldown_label(86400) == "раз в сутки"


def test_ephemeral_request_shape():
    async def scenario():
        bot = Bot("123:abc")
        try:
            method = SendMessage(
                chat_id=-100,
                text="hi",
                parse_mode="HTML",
                ephemeral_message_parameters={"receiver_user_id": 5},
            )
            fields = {
                str(meta["name"]): value
                for meta, _headers, value in bot.session.build_form_data(bot, method)._fields
            }
            assert json.loads(fields["ephemeral_message_parameters"]) == {"receiver_user_id": 5}
            assert DeleteEphemeralMessage.__api_method__ == "deleteEphemeralMessage"
            reply = Message.model_validate(
                {
                    "message_id": 0,
                    "date": 0,
                    "chat": {"id": -100, "type": "supergroup"},
                    "text": "x",
                    "ephemeral_message_id": 7,
                }
            )
            assert reply.ephemeral_message_id == 7
        finally:
            await bot.session.close()

    asyncio.run(scenario())


class FakeBot:
    def __init__(self, ephemeral_fails: bool, admins=(), tags=None) -> None:
        self.ephemeral_fails = ephemeral_fails
        self.calls = []
        self.admins = set(admins)
        self.tags = tags or {}

    async def get_chat_administrators(self, chat_id):
        return [SimpleNamespace(user=SimpleNamespace(id=uid)) for uid in self.admins]

    async def get_chat_member(self, chat_id, user_id):
        status = "administrator" if user_id in self.admins else "member"
        return SimpleNamespace(status=status, tag=self.tags.get(user_id))

    async def __call__(self, method):
        params = getattr(method, "model_extra", {}) or {}
        if "ephemeral_message_parameters" in params:
            self.calls.append(("ephemeral", method.chat_id, params["ephemeral_message_parameters"]))
            if self.ephemeral_fails:
                raise TelegramBadRequest(method=method, message="Bad Request: unknown parameter")
            return SimpleNamespace(message_id=0, ephemeral_message_id=77)
        self.calls.append((method.__api_method__, method.model_dump()))
        return True

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("public", chat_id))
        return SimpleNamespace(message_id=555)


def _run_begin(ephemeral_fails: bool, private: int):
    async def scenario():
        db = Database(_tmp_db())
        await db.connect()
        try:
            await db.upsert_chat(-100, "Чат", "supergroup", None)
            await db.update_chat(-100, captcha_private=private)
            app = App(db, 1, "bot", 999, SpamTracker(), AdminCache())
            fake = FakeBot(ephemeral_fails)

            async def ensure(bot, db_, bot_id, chat):
                return await db_.get_chat(chat.id)

            async def not_admin(*_args):
                return False

            async def lock(*_args):
                return None

            originals = (captcha.ensure_chat, captcha.is_chat_admin, captcha.lock_member)
            captcha.ensure_chat, captcha.is_chat_admin, captcha.lock_member = ensure, not_admin, lock
            try:
                user = SimpleNamespace(
                    id=5, is_bot=False, username="vasya", full_name="Вася", first_name="Вася"
                )
                await captcha.begin_captcha(fake, app, SimpleNamespace(id=-100), user)
            finally:
                captcha.ensure_chat, captcha.is_chat_admin, captcha.lock_member = originals
            return fake.calls, await db.get_pending(-100, 5)
        finally:
            await db.close()

    return asyncio.run(scenario())


def test_captcha_goes_only_to_newcomer():
    calls, pending = _run_begin(ephemeral_fails=False, private=1)
    assert calls == [("ephemeral", -100, {"receiver_user_id": 5})]
    assert pending["ephemeral_id"] == 77
    assert pending["prompt_message_id"] is None


def test_captcha_falls_back_to_public_message():
    calls, pending = _run_begin(ephemeral_fails=True, private=1)
    assert [call[0] for call in calls] == ["ephemeral", "public"]
    assert pending["prompt_message_id"] == 555
    assert pending["ephemeral_id"] is None


def test_captcha_public_when_setting_off():
    calls, pending = _run_begin(ephemeral_fails=False, private=0)
    assert calls == [("public", -100)]
    assert pending["prompt_message_id"] == 555


def test_personal_ad_sent_once_per_cooldown():
    async def scenario():
        db = Database(_tmp_db())
        await db.connect()
        try:
            await db.upsert_chat(-100, "Чат", "supergroup", None)
            await db.update_ads(
                enabled=1, personal=1, personal_cooldown=3600, text="Купи слона", in_welcome=0
            )
            app = App(db, 1, "bot", 999, SpamTracker(), AdminCache())
            fake = FakeBot(ephemeral_fails=False)
            user = SimpleNamespace(id=5, is_bot=False)

            def msg(text):
                return SimpleNamespace(
                    from_user=user,
                    chat=SimpleNamespace(id=-100),
                    text=text,
                    new_chat_members=None,
                    left_chat_member=None,
                )

            await captcha.note_chat_message(fake, app, msg("привет"))
            await captcha.note_chat_message(fake, app, msg("ещё раз"))
            await captcha.note_chat_message(fake, app, msg("/start"))
            sent = [call for call in fake.calls if call[0] == "ephemeral"]
            assert sent == [("ephemeral", -100, {"receiver_user_id": 5})]

            await db.update_chat(-100, moderation_enabled=0)
            await db.update_ads(personal_cooldown=0)
            await captcha.note_chat_message(fake, app, msg("пауза"))
            assert len([call for call in fake.calls if call[0] == "ephemeral"]) == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def _ads_scenario(fake, steps):
    """steps(app) — корутина; возвращает получателей эфемерной рекламы."""

    async def scenario():
        db = Database(_tmp_db())
        await db.connect()
        try:
            await db.upsert_chat(-100, "Чат", "supergroup", None)
            await db.update_ads(enabled=1, personal=1, personal_cooldown=0, text="Реклама")
            app = App(db, 1, "bot", 999, SpamTracker(), AdminCache())
            await steps(app)
            return [call[2]["receiver_user_id"] for call in fake.calls if call[0] == "ephemeral"]
        finally:
            await db.close()

    return asyncio.run(scenario())


def _message(user_id, text="привет", sender_tag=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        chat=SimpleNamespace(id=-100),
        text=text,
        sender_tag=sender_tag,
        new_chat_members=None,
        left_chat_member=None,
    )


def test_admins_and_tagged_members_get_no_ads():
    fake = FakeBot(ephemeral_fails=False, admins={10}, tags={20: "модератор"})

    async def steps(app):
        await captcha.note_chat_message(fake, app, _message(10))
        await captcha.note_chat_message(fake, app, _message(20))
        await captcha.note_chat_message(fake, app, _message(30, sender_tag="VIP"))
        await captcha.note_chat_message(fake, app, _message(40))

    assert _ads_scenario(fake, steps) == [40]


def test_reaction_shows_ad_to_reactor():
    from bot.handlers.reactions import on_reaction

    fake = FakeBot(ephemeral_fails=False, admins={10})

    def reaction(user_id, new):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, is_bot=False),
            chat=SimpleNamespace(id=-100),
            new_reaction=new,
        )

    async def steps(app):
        await on_reaction(reaction(50, [{"type": "emoji", "emoji": "👍"}]), fake, app)
        await on_reaction(reaction(60, []), fake, app)
        await on_reaction(reaction(10, [{"type": "emoji", "emoji": "👍"}]), fake, app)
        await app.db.update_chat(-100, moderation_enabled=0)
        await on_reaction(reaction(70, [{"type": "emoji", "emoji": "👍"}]), fake, app)

    assert _ads_scenario(fake, steps) == [50]


def test_welcome_contains_ad_when_only_newcomer_sees_it():
    async def scenario():
        db = Database(_tmp_db())
        await db.connect()
        try:
            await db.upsert_chat(-100, "Чат", "supergroup", None)
            await db.update_chat(-100, welcome_text="Добро пожаловать, {name}!")
            await db.update_ads(
                enabled=1,
                in_welcome=1,
                text="Лучший VPN",
                button_text="Подключить",
                button_url="https://example.com",
            )
            app = App(db, 1, "bot", 999, SpamTracker(), AdminCache())
            sent = []

            async def fake_send(bot, chat_id, user_id, text, markup=None, **kwargs):
                sent.append((user_id, text, markup))
                return 77

            async def ensure(bot, db_, bot_id, chat):
                return await db_.get_chat(chat.id)

            async def no(*_args):
                return False

            async def lock(*_args):
                return None

            saved = (captcha.ensure_chat, captcha.is_chat_admin, captcha.lock_member, captcha.send_ephemeral)
            captcha.ensure_chat, captcha.is_chat_admin, captcha.lock_member, captcha.send_ephemeral = (
                ensure, no, lock, fake_send,
            )
            try:
                user = SimpleNamespace(id=5, is_bot=False, username=None, full_name="Вася", first_name="Вася")
                await captcha.begin_captcha(None, app, SimpleNamespace(id=-100), user)
            finally:
                captcha.ensure_chat, captcha.is_chat_admin, captcha.lock_member, captcha.send_ephemeral = saved
            return sent
        finally:
            await db.close()

    [(user_id, text, markup)] = asyncio.run(scenario())
    assert user_id == 5
    assert "Добро пожаловать, Вася!" in text
    assert "Лучший VPN" in text
    buttons = [row[0] for row in markup.inline_keyboard]
    assert buttons[0].text == "Я не робот"
    assert buttons[1].text == "Подключить" and buttons[1].url == "https://example.com"
