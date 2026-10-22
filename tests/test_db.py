import asyncio
import os
import tempfile

from bot.db import Database


def test_chat_settings_survive_readd_and_captcha_is_single_winner():
    asyncio.run(_scenario())


async def _scenario():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    db = Database(path)
    await db.connect()
    try:
        await db.upsert_chat(-100, "Тест", "supergroup", 5)
        await db.update_chat(-100, captcha_enabled=0, welcome_text="своё")
        await db.upsert_chat(-100, "Тест 2", "supergroup", 9)
        chat = await db.get_chat(-100)
        assert chat["captcha_enabled"] == 0
        assert chat["moderation_enabled"] == 1
        assert chat["welcome_text"] == "своё"
        assert chat["title"] == "Тест 2"
        assert chat["active"] == 1

        assert await db.add_pending(-100, 7, 100) is True
        assert await db.add_pending(-100, 7, 200) is False
        assert await db.finish_captcha(-100, 7, success=False, now=50) is None
        row = await db.finish_captcha(-100, 7, success=True, now=150)
        assert row is not None
        assert await db.finish_captcha(-100, 7, success=True, now=150) is None
        assert await db.get_verified(-100, 7) is not None
        await db.forget_verified(-100, 7)
        assert await db.get_verified(-100, 7) is None
        await db.mark_verified(-100, 7)

        await db.add_pending(-100, 8, 10)
        expired = await db.finish_captcha(-100, 8, success=False, now=10)
        assert expired is not None
        assert await db.get_verified(-100, 8) is None

        assert await db.add_warning(-100, 7) == 1
        assert await db.add_warning(-100, 7) == 2
        await db.reset_warnings(-100, 7)
        assert await db.add_warning(-100, 7) == 1

        await db.update_ads(enabled=1, text="реклама", every_n=3)
        ads = await db.get_ads()
        assert ads["enabled"] == 1
        assert ads["text"] == "реклама"
        assert await db.bump_ad_cursor(-100) == 1

        await db.add_staff(-100, 5)
        chats = await db.chats_for_user(5)
        assert [item["chat_id"] for item in chats] == [-100]
        await db.update_chat(-100, active=0)
        assert await db.chats_for_user(5) == []
    finally:
        await db.close()
        os.remove(path)


def test_old_database_gains_moderation_switch():
    asyncio.run(_old_database())


async def _old_database():
    import sqlite3

    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        )
        """
    )
    raw.execute("INSERT INTO chats (chat_id, title, created_at) VALUES (-5, 'старый', 1)")
    raw.commit()
    raw.close()
    db = Database(path)
    await db.connect()
    try:
        chat = await db.get_chat(-5)
        assert chat["moderation_enabled"] == 1
        await db.update_chat(-5, moderation_enabled=0)
        assert (await db.get_chat(-5))["moderation_enabled"] == 0
    finally:
        await db.close()
        os.remove(path)


def test_chat_ad_is_removed_after_350_messages():
    asyncio.run(_ad_lifetime())


async def _ad_lifetime():
    from bot.logic import AD_EVERY_MESSAGES, AD_KEEP_MESSAGES

    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    db = Database(path)
    await db.connect()
    try:
        await db.upsert_chat(-7, "Чат", "supergroup", 1)
        for _ in range(AD_EVERY_MESSAGES - 1):
            _count, stale = await db.bump_chat_messages(-7, drop_after=AD_KEEP_MESSAGES)
            assert stale == []
        count, stale = await db.bump_chat_messages(-7, drop_after=AD_KEEP_MESSAGES)
        assert count == AD_EVERY_MESSAGES
        assert stale == []
        await db.remember_posted_ad(-7, 42, count)
        for _ in range(AD_KEEP_MESSAGES - 1):
            _count, stale = await db.bump_chat_messages(-7, drop_after=AD_KEEP_MESSAGES)
            assert 42 not in stale
        _count, stale = await db.bump_chat_messages(-7, drop_after=AD_KEEP_MESSAGES)
        assert stale == [42]
        _count, stale = await db.bump_chat_messages(-7, drop_after=AD_KEEP_MESSAGES)
        assert stale == []
    finally:
        await db.close()
        os.remove(path)
