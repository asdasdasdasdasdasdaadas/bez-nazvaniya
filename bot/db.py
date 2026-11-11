from __future__ import annotations

import asyncio
import time

import aiosqlite

CHAT_FIELDS = {
    "title",
    "chat_type",
    "active",
    "moderation_enabled",
    "captcha_enabled",
    "welcome_text",
    "captcha_timeout",
    "captcha_fail",
    "delete_service",
    "spam_links",
    "spam_forwards",
    "spam_flood_count",
    "spam_flood_window",
    "spam_action",
    "spam_caps",
    "spam_repeats",
    "spam_stickers",
    "spam_mute_seconds",
    "newcomer_seconds",
    "warn_limit",
    "warn_action",
    "ad_cursor",
    "captcha_private",
}

AD_FIELDS = {
    "enabled",
    "in_welcome",
    "separate",
    "text",
    "button_text",
    "button_url",
    "every_n",
    "personal",
    "personal_cooldown",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    chat_type TEXT NOT NULL DEFAULT 'supergroup',
    added_by INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    moderation_enabled INTEGER NOT NULL DEFAULT 1,
    captcha_enabled INTEGER NOT NULL DEFAULT 1,
    welcome_text TEXT,
    captcha_timeout INTEGER NOT NULL DEFAULT 120,
    captcha_fail TEXT NOT NULL DEFAULT 'kick',
    delete_service INTEGER NOT NULL DEFAULT 1,
    spam_links INTEGER NOT NULL DEFAULT 1,
    spam_forwards INTEGER NOT NULL DEFAULT 0,
    spam_flood_count INTEGER NOT NULL DEFAULT 5,
    spam_flood_window INTEGER NOT NULL DEFAULT 8,
    spam_action TEXT NOT NULL DEFAULT 'mute',
    spam_caps INTEGER NOT NULL DEFAULT 0,
    spam_repeats INTEGER NOT NULL DEFAULT 1,
    spam_stickers INTEGER NOT NULL DEFAULT 0,
    spam_mute_seconds INTEGER NOT NULL DEFAULT 3600,
    newcomer_seconds INTEGER NOT NULL DEFAULT 600,
    warn_limit INTEGER NOT NULL DEFAULT 3,
    warn_action TEXT NOT NULL DEFAULT 'mute',
    ad_cursor INTEGER NOT NULL DEFAULT 0,
    msg_count INTEGER NOT NULL DEFAULT 0,
    captcha_private INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS staff (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS pending (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    prompt_message_id INTEGER,
    dm_message_id INTEGER,
    ephemeral_id INTEGER,
    deadline INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_deadline ON pending(deadline);

CREATE TABLE IF NOT EXISTS verified (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    verified_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS warnings (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS mod_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    admin_id INTEGER,
    action TEXT NOT NULL,
    until_ts INTEGER,
    reason TEXT,
    target_name TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ads (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    in_welcome INTEGER NOT NULL DEFAULT 1,
    separate INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    button_text TEXT NOT NULL DEFAULT '',
    button_url TEXT NOT NULL DEFAULT '',
    every_n INTEGER NOT NULL DEFAULT 1,
    personal INTEGER NOT NULL DEFAULT 0,
    personal_cooldown INTEGER NOT NULL DEFAULT 21600
);

CREATE TABLE IF NOT EXISTS posted_ads (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    at_count INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS ad_seen (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    shown_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
"""

MIGRATIONS = (
    ("chats", "moderation_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("chats", "msg_count", "INTEGER NOT NULL DEFAULT 0"),
    ("chats", "captcha_private", "INTEGER NOT NULL DEFAULT 1"),
    ("pending", "ephemeral_id", "INTEGER"),
    ("ads", "personal", "INTEGER NOT NULL DEFAULT 0"),
    ("ads", "personal_cooldown", "INTEGER NOT NULL DEFAULT 21600"),
)


def _dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row)


class Database:
    """Один замок на все запросы: так проверки «кто первый нажал» не пересекаются."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.execute("INSERT OR IGNORE INTO ads (id) VALUES (1)")
        await self.conn.commit()

    async def _migrate(self) -> None:
        assert self.conn is not None
        for table, column, kind in MIGRATIONS:
            cursor = await self.conn.execute(f"PRAGMA table_info({table})")
            names = {row[1] for row in await cursor.fetchall()}
            if names and column not in names:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def upsert_chat(
        self,
        chat_id: int,
        title: str,
        chat_type: str,
        added_by: int | None,
    ) -> None:
        assert self.conn is not None
        now = int(time.time())
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO chats (chat_id, title, chat_type, added_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title=excluded.title,
                    chat_type=excluded.chat_type,
                    active=1
                """,
                (chat_id, title, chat_type, added_by, now),
            )
            await self.conn.commit()

    async def update_chat(self, chat_id: int, **fields) -> None:
        assert self.conn is not None
        unknown = set(fields) - CHAT_FIELDS
        if unknown:
            raise ValueError(f"Неизвестные поля чата: {unknown}")
        if not fields:
            return
        columns = ", ".join(f"{name}=?" for name in fields)
        values = list(fields.values()) + [chat_id]
        async with self._lock:
            await self.conn.execute(f"UPDATE chats SET {columns} WHERE chat_id=?", values)
            await self.conn.commit()

    async def get_chat(self, chat_id: int) -> dict | None:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
            return _dict(await cursor.fetchone())

    async def chats_for_user(self, user_id: int) -> list[dict]:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                """
                SELECT c.* FROM chats c
                JOIN staff s ON s.chat_id = c.chat_id
                WHERE s.user_id=? AND c.active=1
                """,
                (user_id,),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
        rows.sort(key=lambda row: (row["title"] or "").casefold())
        return rows

    async def active_chats(self) -> list[dict]:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute("SELECT * FROM chats WHERE active=1")
            rows = [dict(row) for row in await cursor.fetchall()]
        rows.sort(key=lambda row: (row["title"] or "").casefold())
        return rows

    async def add_staff(self, chat_id: int, user_id: int) -> None:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                "INSERT OR IGNORE INTO staff (chat_id, user_id) VALUES (?, ?)",
                (chat_id, user_id),
            )
            await self.conn.commit()

    async def remove_staff(self, chat_id: int, user_id: int) -> None:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM staff WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            await self.conn.commit()

    async def replace_staff(self, chat_id: int, user_ids: list[int]) -> None:
        assert self.conn is not None
        unique = list(dict.fromkeys(user_ids))
        async with self._lock:
            await self.conn.execute("DELETE FROM staff WHERE chat_id=?", (chat_id,))
            if unique:
                await self.conn.executemany(
                    "INSERT INTO staff (chat_id, user_id) VALUES (?, ?)",
                    [(chat_id, user_id) for user_id in unique],
                )
            await self.conn.commit()

    async def add_pending(self, chat_id: int, user_id: int, deadline: int) -> bool:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                "INSERT OR IGNORE INTO pending (chat_id, user_id, deadline) VALUES (?, ?, ?)",
                (chat_id, user_id, deadline),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def get_pending(self, chat_id: int, user_id: int) -> dict | None:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM pending WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            return _dict(await cursor.fetchone())

    async def drop_pending(self, chat_id: int, user_id: int) -> dict | None:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM pending WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            row = _dict(await cursor.fetchone())
            if row is None:
                return None
            await self.conn.execute(
                "DELETE FROM pending WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            await self.conn.commit()
            return row

    async def list_expired(self, now: int) -> list[tuple[int, int]]:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT chat_id, user_id FROM pending WHERE deadline<=?",
                (now,),
            )
            rows = await cursor.fetchall()
            return [(int(row["chat_id"]), int(row["user_id"])) for row in rows]

    async def finish_captcha(
        self,
        chat_id: int,
        user_id: int,
        *,
        success: bool,
        now: int,
    ) -> dict | None:
        """Кто первый закрыл проверку, тот и победил. Второй вызов вернёт None."""
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM pending WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            if not success and int(row["deadline"]) > now:
                return None
            data = dict(row)
            await self.conn.execute(
                "DELETE FROM pending WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            if success:
                await self.conn.execute(
                    """
                    INSERT INTO verified (chat_id, user_id, verified_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id, user_id) DO UPDATE SET verified_at=excluded.verified_at
                    """,
                    (chat_id, user_id, now),
                )
            await self.conn.commit()
            return data

    async def cancel_chat_pending(self, chat_id: int, now: int) -> list[dict]:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute("SELECT * FROM pending WHERE chat_id=?", (chat_id,))
            rows = [dict(row) for row in await cursor.fetchall()]
            await self.conn.execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))
            for row in rows:
                await self.conn.execute(
                    """
                    INSERT INTO verified (chat_id, user_id, verified_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id, user_id) DO UPDATE SET verified_at=excluded.verified_at
                    """,
                    (chat_id, row["user_id"], now),
                )
            await self.conn.commit()
            return rows

    async def set_captcha_messages(
        self,
        chat_id: int,
        user_id: int,
        *,
        prompt_id: int | None = None,
        dm_id: int | None = None,
        ephemeral_id: int | None = None,
    ) -> None:
        assert self.conn is not None
        sets: list[str] = []
        values: list[int] = []
        if prompt_id is not None:
            sets.append("prompt_message_id=?")
            values.append(prompt_id)
        if dm_id is not None:
            sets.append("dm_message_id=?")
            values.append(dm_id)
        if ephemeral_id is not None:
            sets.append("ephemeral_id=?")
            values.append(ephemeral_id)
        if not sets:
            return
        values.extend([chat_id, user_id])
        async with self._lock:
            await self.conn.execute(
                f"UPDATE pending SET {', '.join(sets)} WHERE chat_id=? AND user_id=?",
                values,
            )
            await self.conn.commit()

    async def get_verified(self, chat_id: int, user_id: int) -> dict | None:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM verified WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            return _dict(await cursor.fetchone())

    async def mark_verified(self, chat_id: int, user_id: int, now: int | None = None) -> None:
        assert self.conn is not None
        moment = int(time.time()) if now is None else now
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO verified (chat_id, user_id, verified_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET verified_at=excluded.verified_at
                """,
                (chat_id, user_id, moment),
            )
            await self.conn.commit()

    async def forget_verified(self, chat_id: int, user_id: int) -> None:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM verified WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            await self.conn.commit()

    async def add_warning(self, chat_id: int, user_id: int) -> int:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO warnings (chat_id, user_id, count) VALUES (?, ?, 1)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET count = count + 1
                """,
                (chat_id, user_id),
            )
            cursor = await self.conn.execute(
                "SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            row = await cursor.fetchone()
            await self.conn.commit()
            return int(row["count"])

    async def reset_warnings(self, chat_id: int, user_id: int) -> None:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM warnings WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            await self.conn.commit()

    async def add_log(
        self,
        chat_id: int,
        user_id: int,
        action: str,
        *,
        admin_id: int | None = None,
        until_ts: int | None = None,
        reason: str | None = None,
        target_name: str | None = None,
    ) -> None:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO mod_log (
                    chat_id, user_id, admin_id, action, until_ts, reason, target_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    admin_id,
                    action,
                    until_ts,
                    reason,
                    target_name,
                    int(time.time()),
                ),
            )
            await self.conn.commit()

    async def recent_logs(self, chat_id: int, limit: int = 15) -> list[dict]:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM mod_log WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_ads(self) -> dict:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute("SELECT * FROM ads WHERE id=1")
            row = await cursor.fetchone()
            if row is None:
                await self.conn.execute("INSERT OR IGNORE INTO ads (id) VALUES (1)")
                await self.conn.commit()
                cursor = await self.conn.execute("SELECT * FROM ads WHERE id=1")
                row = await cursor.fetchone()
            return dict(row)

    async def update_ads(self, **fields) -> None:
        assert self.conn is not None
        unknown = set(fields) - AD_FIELDS
        if unknown:
            raise ValueError(f"Неизвестные поля рекламы: {unknown}")
        if not fields:
            return
        columns = ", ".join(f"{name}=?" for name in fields)
        values = list(fields.values()) + [1]
        async with self._lock:
            await self.conn.execute(f"UPDATE ads SET {columns} WHERE id=?", values)
            await self.conn.commit()

    async def bump_ad_cursor(self, chat_id: int) -> int:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                "UPDATE chats SET ad_cursor = ad_cursor + 1 WHERE chat_id=?",
                (chat_id,),
            )
            cursor = await self.conn.execute(
                "SELECT ad_cursor FROM chats WHERE chat_id=?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            await self.conn.commit()
            return int(row["ad_cursor"]) if row else 0

    async def bump_chat_messages(
        self,
        chat_id: int,
        *,
        drop_after: int,
    ) -> tuple[int, list[int]] | None:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT active, moderation_enabled, msg_count FROM chats WHERE chat_id=?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            if row is None or not row["active"] or not int(row["moderation_enabled"]):
                return None
            count = int(row["msg_count"]) + 1
            await self.conn.execute(
                "UPDATE chats SET msg_count=? WHERE chat_id=?",
                (count, chat_id),
            )
            cursor = await self.conn.execute(
                "SELECT message_id FROM posted_ads WHERE chat_id=? AND at_count + ? <= ?",
                (chat_id, drop_after, count),
            )
            stale = [int(item["message_id"]) for item in await cursor.fetchall()]
            if stale:
                marks = ",".join("?" for _ in stale)
                await self.conn.execute(
                    f"DELETE FROM posted_ads WHERE chat_id=? AND message_id IN ({marks})",
                    [chat_id, *stale],
                )
            await self.conn.commit()
            return count, stale

    async def remember_posted_ad(self, chat_id: int, message_id: int, at_count: int) -> None:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO posted_ads (chat_id, message_id, at_count)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET at_count=excluded.at_count
                """,
                (chat_id, message_id, at_count),
            )
            await self.conn.commit()

    async def claim_personal_ad(self, chat_id: int, user_id: int, *, now: int, cooldown: int) -> bool:
        """True, если этому человеку в этом чате пора показать рекламу. Сразу отмечает показ."""
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT shown_at FROM ad_seen WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            row = await cursor.fetchone()
            if row is not None and now - int(row["shown_at"]) < cooldown:
                return False
            await self.conn.execute(
                """
                INSERT INTO ad_seen (chat_id, user_id, shown_at) VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET shown_at=excluded.shown_at
                """,
                (chat_id, user_id, now),
            )
            await self.conn.commit()
            return True
