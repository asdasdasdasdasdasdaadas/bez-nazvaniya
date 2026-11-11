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
from datetime import datetime

import bot.ads as ads_module
from bot.handlers.reactions import on_reaction
from bot.logic import MSK, SpamTracker, msk_day
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



def _msk(hour, minute=0, day=11):
    return int(datetime(2026, 11, day, hour, minute, tzinfo=MSK).timestamp())


def test_msk_day_resets_at_moscow_midnight():
    assert msk_day(_msk(23, 59)) == "2026-11-11"
    assert msk_day(_msk(0, 0, day=12)) == "2026-11-12"


def test_daily_limit_and_gap():
    async def scenario():
        db = Database(_tmp_db())
        await db.connect()
        try:
            def claim(t):
                return db.claim_ad(5, day=msk_day(t), now=t, limit=3, gap=7200)

            results = [
                await claim(_msk(9)),
                await claim(_msk(9, 30)),   # меньше 2 часов
                await claim(_msk(11)),
                await claim(_msk(13)),
                await claim(_msk(16)),      # четвёртый за день
                await claim(_msk(9, day=12)),  # новый день
            ]
            await db.record_ad_view(6, day=msk_day(_msk(9)), now=_msk(9))
            other = await db.claim_ad(6, day=msk_day(_msk(10)), now=_msk(10), limit=3, gap=7200)
            return results, other
        finally:
            await db.close()

    results, other = asyncio.run(scenario())
    assert results == [True, False, True, True, False, True]
    assert other is False  # реклама в проверке считается и держит паузу


def test_public_captcha_has_no_ad():
    async def scenario():
        db = Database(_tmp_db())
        await db.connect()
        try:
            await db.upsert_chat(-100, "Чат", "supergroup", None)
            await db.update_chat(-100, captcha_private=0)
            await db.update_ads(enabled=1, in_welcome=1, text="Лучший VPN",
                                button_text="Подключить", button_url="https://example.com")
            app = App(db, 1, "bot", 999, SpamTracker(), AdminCache())
            sent = []

            class PublicBot:
                async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
                    sent.append((text, reply_markup))
                    return SimpleNamespace(message_id=1)

            async def ensure(bot, db_, bot_id, chat):
                return await db_.get_chat(chat.id)

            async def no(*_args):
                return False

            saved = (captcha.ensure_chat, captcha.is_chat_admin, captcha.lock_member)
            captcha.ensure_chat, captcha.is_chat_admin, captcha.lock_member = ensure, no, no
            try:
                user = SimpleNamespace(id=5, is_bot=False, username=None, full_name="Вася", first_name="Вася")
                await captcha.begin_captcha(PublicBot(), app, SimpleNamespace(id=-100), user)
            finally:
                captcha.ensure_chat, captcha.is_chat_admin, captcha.lock_member = saved
            return sent
        finally:
            await db.close()

    [(text, markup)] = asyncio.run(scenario())
    assert "Лучший VPN" not in text
    assert [row[0].text for row in markup.inline_keyboard] == ["Я не робот"]


def _ads_scenario(fake, steps):
    async def scenario():
        db = Database(_tmp_db())
        await db.connect()
        ads_module._exempt.clear()
        try:
            await db.upsert_chat(-100, "Чат", "supergroup", None)
            await db.upsert_chat(-200, "Второй", "supergroup", None)
            await db.update_ads(enabled=1, personal=1, text="Реклама")
            app = App(db, 1, "bot", 999, SpamTracker(), AdminCache())
            result = await steps(app, db)
            sent = [(call[1], call[2]["receiver_user_id"]) for call in fake.calls if call[0] == "ephemeral"]
            return sent, result
        finally:
            await db.close()

    return asyncio.run(scenario())


def test_any_action_shows_ad_up_to_three_times_a_day():
    fake = FakeBot(ephemeral_fails=False)

    async def steps(app, db):
        t = _msk(9)
        shown = []
        for minutes in (0, 10, 130, 135, 260, 400, 600):
            shown.append(await ads_module.offer_ad(fake, app, -100, 30, now=t + minutes * 60))
        # Лимит общий на человека, а не на чат.
        shown.append(await ads_module.offer_ad(fake, app, -200, 30, now=t + 700 * 60))
        return shown

    sent, shown = _ads_scenario(fake, steps)
    assert shown == [True, False, True, False, True, False, False, False]
    assert sent == [(-100, 30)] * 3


def test_admins_tagged_and_unverified_get_no_ads():
    fake = FakeBot(ephemeral_fails=False, admins={10}, tags={20: "модератор"})

    async def steps(app, db):
        await db.add_pending(-100, 60, 10**10)
        return [
            await ads_module.offer_ad(fake, app, -100, 10),
            await ads_module.offer_ad(fake, app, -100, 20),
            await ads_module.offer_ad(fake, app, -100, 30, sender_tag="VIP"),
            await ads_module.offer_ad(fake, app, -100, 60),
            await ads_module.offer_ad(fake, app, -100, 40),
        ]

    sent, shown = _ads_scenario(fake, steps)
    assert shown == [False, False, False, False, True]
    assert sent == [(-100, 40)]


def test_no_ads_when_off_or_moderation_paused():
    fake = FakeBot(ephemeral_fails=False)

    async def steps(app, db):
        await db.update_ads(personal=0)
        off = await ads_module.offer_ad(fake, app, -100, 30)
        await db.update_ads(personal=1)
        await db.update_chat(-100, moderation_enabled=0)
        paused = await ads_module.offer_ad(fake, app, -100, 30)
        return off, paused

    sent, result = _ads_scenario(fake, steps)
    assert result == (False, False) and sent == []


def test_message_and_reaction_trigger_ad():
    fake = FakeBot(ephemeral_fails=False)

    async def steps(app, db):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=30, is_bot=False),
            chat=SimpleNamespace(id=-100),
            text="привет",
            sender_tag=None,
            new_chat_members=None,
            left_chat_member=None,
        )
        await captcha.note_chat_message(fake, app, message)

        def reaction(user_id, new):
            return SimpleNamespace(
                user=SimpleNamespace(id=user_id, is_bot=False),
                chat=SimpleNamespace(id=-100),
                new_reaction=new,
            )

        await on_reaction(reaction(40, [{"type": "emoji", "emoji": "👍"}]), fake, app)
        await on_reaction(reaction(50, []), fake, app)

    sent, _ = _ads_scenario(fake, steps)
    assert sent == [(-100, 30), (-100, 40)]


def test_old_public_ads_are_deleted_on_start():
    deleted = []

    class DeletingBot:
        async def delete_message(self, chat_id, message_id):
            deleted.append((chat_id, message_id))
            return True

    async def scenario():
        db = Database(_tmp_db())
        await db.connect()
        try:
            await db.conn.execute(
                "INSERT INTO posted_ads (chat_id, message_id, at_count) VALUES (-1, 11, 150), (-2, 22, 300)"
            )
            await db.conn.commit()
            app = App(db, 1, "bot", 999, SpamTracker(), AdminCache())
            saved = ads_module.asyncio.sleep

            async def no_sleep(_):
                return None

            ads_module.asyncio.sleep = no_sleep
            try:
                removed = await ads_module.cleanup_public_ads(DeletingBot(), app)
                again = await ads_module.cleanup_public_ads(DeletingBot(), app)
            finally:
                ads_module.asyncio.sleep = saved
            return removed, again
        finally:
            await db.close()

    removed, again = asyncio.run(scenario())
    assert sorted(deleted) == [(-2, 22), (-1, 11)]
    assert (removed, again) == (2, 0)
