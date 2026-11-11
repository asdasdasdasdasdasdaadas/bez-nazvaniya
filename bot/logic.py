from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html import escape

DEFAULT_WELCOME = ""

MAX_MUTE_SECONDS = 366 * 24 * 3600

LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|tg://|discord\.gg/)",
    re.IGNORECASE,
)

_SUFFIX = {
    "m": 60,
    "м": 60,
    "h": 3600,
    "ч": 3600,
    "d": 86400,
    "д": 86400,
    "w": 604800,
    "н": 604800,
}

_SINGLE = dict(_SUFFIX)

_WORDS = (
    ("мин", 60),
    ("час", 3600),
    ("сут", 86400),
    ("ден", 86400),
    ("дн", 86400),
    ("нед", 604800),
)

_SPECIAL = {
    "навсегда": 0,
    "перманент": 0,
    "permanent": 0,
    "perm": 0,
    "полчаса": 1800,
    "час": 3600,
    "сутки": 86400,
    "день": 86400,
    "неделя": 604800,
}

TOGGLES = {
    "mod": "moderation_enabled",
    "cap": "captcha_enabled",
    "prv": "captcha_private",
    "svc": "delete_service",
    "lnk": "spam_links",
    "fwd": "spam_forwards",
    "rep": "spam_repeats",
    "cps": "spam_caps",
    "stk": "spam_stickers",
}

CYCLES: dict[str, tuple[str, list]] = {
    "fail": ("captcha_fail", ["kick", "ban", "mute"]),
    "cto": ("captcha_timeout", [30, 60, 120, 300, 600]),
    "flc": ("spam_flood_count", [3, 5, 8, 12]),
    "flw": ("spam_flood_window", [5, 8, 15, 30, 60]),
    "act": ("spam_action", ["delete", "mute", "ban"]),
    "mut": ("spam_mute_seconds", [600, 1800, 3600, 21600, 86400]),
    "new": ("newcomer_seconds", [0, 600, 1800, 3600, 21600]),
    "wln": ("warn_limit", [0, 2, 3, 5]),
    "wac": ("warn_action", ["mute", "ban"]),
}

# 0 — после каждого сообщения человека.
PERSONAL_COOLDOWNS = [0, 3600, 21600, 86400]

OWNER_TOGGLES = {
    "en": "enabled",
    "iw": "in_welcome",
    "sp": "separate",
    "pm": "personal",
}

OWNER_CYCLES = {
    "n": ("every_n", [1, 2, 3, 5, 10, 20]),
    "pc": ("personal_cooldown", PERSONAL_COOLDOWNS),
}

AD_EVERY_MESSAGES = 150
AD_KEEP_MESSAGES = 350

SPAM_KEYS = {"lnk", "fwd", "rep", "cps", "stk", "flc", "flw", "act", "mut", "new", "wln", "wac"}

_COMPACT_DURATION = re.compile(r"(\d+)\s*([mмhчdдwн])")


def plural(value: int, one: str, few: str, many: str) -> str:
    number = abs(value) % 100
    if 11 <= number <= 14:
        return many
    tail = number % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def cooldown_label(seconds: int) -> str:
    if seconds <= 0:
        return "после каждого сообщения"
    named = {3600: "раз в час", 86400: "раз в сутки"}
    return named.get(seconds, f"раз в {human_seconds(seconds)}")


def human_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "навсегда"
    if seconds < 60:
        return f"{seconds} сек"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} {plural(days, 'день', 'дня', 'дней')}"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} {plural(hours, 'час', 'часа', 'часов')}"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} {plural(minutes, 'минута', 'минуты', 'минут')}"
    return f"{seconds} сек"


def format_ts(timestamp: int) -> str:
    moment = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=3)))
    return moment.strftime("%d.%m %H:%M")


def next_in_cycle(options: list, current):
    if current not in options:
        return options[0]
    index = options.index(current)
    return options[(index + 1) % len(options)]


def pack_chat_id(chat_id: int) -> str:
    if chat_id < 0:
        return f"m{-chat_id}"
    return f"p{chat_id}"


def unpack_chat_id(value: str) -> int | None:
    if len(value) < 2 or value[0] not in {"m", "p"} or not value[1:].isdigit():
        return None
    number = int(value[1:])
    return -number if value[0] == "m" else number


def split_payload(payload: str | None) -> tuple[str, int] | None:
    if not payload or len(payload) < 3 or payload[1] != "_":
        return None
    kind = payload[0]
    if kind not in {"v", "c"}:
        return None
    chat_id = unpack_chat_id(payload[2:])
    if chat_id is None:
        return None
    return kind, chat_id


def contains_link(text: str) -> bool:
    return bool(text and LINK_RE.search(text))


def is_caps(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 12:
        return False
    upper = sum(1 for char in letters if char.isupper())
    return upper / len(letters) >= 0.75


def _cap_duration(seconds: int) -> int:
    if seconds <= 0:
        return 0
    return min(seconds, MAX_MUTE_SECONDS)


def parse_duration_prefix(tokens: list[str]) -> tuple[int | None, int]:
    if not tokens:
        return None, 0
    raw = tokens[0].lower().rstrip(".,")
    if raw in _SPECIAL:
        return _SPECIAL[raw], 1
    matched = _COMPACT_DURATION.fullmatch(raw)
    if matched:
        return _cap_duration(int(matched.group(1)) * _SUFFIX[matched.group(2)]), 1
    if raw.isdigit() and len(tokens) > 1 and int(raw) <= 100000:
        unit = tokens[1].lower().rstrip(".,")
        if unit in _SINGLE:
            return _cap_duration(int(raw) * _SINGLE[unit]), 2
        for prefix, mult in _WORDS:
            if unit.startswith(prefix):
                return _cap_duration(int(raw) * mult), 2
    return None, 0


def is_target(token: str) -> bool:
    if token.startswith("@") and len(token) > 1:
        return True
    return token.isdigit() and len(token) >= 5


def parse_moderation_args(text: str) -> tuple[str | None, int | None, str]:
    tokens = text.split()
    target = None
    if tokens and is_target(tokens[0]):
        target = tokens.pop(0)
    seconds, used = parse_duration_prefix(tokens)
    reason = " ".join(tokens[used:]).strip()
    return target, seconds, reason


def render_welcome(template: str, *, name: str, chat: str, username: str) -> str:
    safe = escape(template)
    return (
        safe.replace("{name}", escape(name))
        .replace("{chat}", escape(chat))
        .replace("{username}", escape(username))
    )


def build_welcome_text(
    template: str,
    *,
    name: str,
    chat: str,
    username: str,
    ad_text: str | None,
) -> str:
    body = render_welcome(template, name=name, chat=chat, username=username).strip()
    if ad_text and ad_text.strip():
        ad = escape(ad_text.strip())
        return f"{body}\n\n{ad}" if body else ad
    return body


def ad_message_text(text: str) -> str:
    return escape(text.strip())


def valid_http_url(url: str) -> bool:
    lowered = url.lower()
    return (lowered.startswith("https://") or lowered.startswith("http://")) and " " not in url


def short_title(title: str, limit: int = 40) -> str:
    clean = " ".join((title or "Чат").split()) or "Чат"
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


class SpamTracker:
    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._buckets: dict[tuple, deque[float]] = defaultdict(deque)
        self._repeats: dict[tuple[int, int], tuple[str, float, int]] = {}
        self._marks: dict[tuple, float] = {}

    def _prune(self, key: tuple, window: float) -> deque[float]:
        now = self._clock()
        bucket = self._buckets[key]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        return bucket

    def flood(self, chat_id: int, user_id: int, limit: int, window: float) -> bool:
        bucket = self._prune(("flood", chat_id, user_id), window)
        bucket.append(self._clock())
        return len(bucket) >= limit

    def stickers(self, chat_id: int, user_id: int, limit: int = 3, window: float = 15) -> bool:
        bucket = self._prune(("sticker", chat_id, user_id), window)
        bucket.append(self._clock())
        return len(bucket) >= limit

    def repeat(self, chat_id: int, user_id: int, text: str, window: float = 120) -> bool:
        normalized = " ".join(text.casefold().split())
        if len(normalized) < 4:
            return False
        key = (chat_id, user_id)
        now = self._clock()
        previous = self._repeats.get(key)
        if previous and previous[0] == normalized and now - previous[1] <= window:
            count = previous[2] + 1
            self._repeats[key] = (normalized, now, count)
            return count >= 3
        self._repeats[key] = (normalized, now, 1)
        return False

    def claim(self, kind: str, chat_id: int, user_id: int, cooldown: float) -> bool:
        key = (kind, chat_id, user_id)
        now = self._clock()
        last = self._marks.get(key)
        if last is not None and now - last < cooldown:
            return False
        self._marks[key] = now
        return True
