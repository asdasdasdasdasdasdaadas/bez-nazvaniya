from bot.logic import (
    SpamTracker,
    build_welcome_text,
    contains_link,
    human_seconds,
    is_caps,
    next_in_cycle,
    pack_chat_id,
    parse_moderation_args,
    split_payload,
    unpack_chat_id,
)


def test_pack_chat_id():
    assert pack_chat_id(-100123) == "m100123"
    assert unpack_chat_id("m100123") == -100123
    assert unpack_chat_id("p15") == 15
    assert unpack_chat_id("bad") is None
    assert unpack_chat_id("m") is None
    assert split_payload("v_m100123") == ("v", -100123)
    assert split_payload("c_p15") == ("c", 15)
    assert split_payload("hello") is None


def test_duration_and_target():
    assert parse_moderation_args("1h флуд") == (None, 3600, "флуд")
    assert parse_moderation_args("@user 2д реклама") == ("@user", 172800, "реклама")
    assert parse_moderation_args("30 минут") == (None, 1800, "")
    assert parse_moderation_args("1 ч причина") == (None, 3600, "причина")
    assert parse_moderation_args("навсегда оскорбления") == (None, 0, "оскорбления")
    assert parse_moderation_args("") == (None, None, "")
    assert parse_moderation_args("123456789") == ("123456789", None, "")
    assert parse_moderation_args("2 часа флуд") == (None, 7200, "флуд")


def test_links_and_caps():
    assert contains_link("смотри https://evil.tld")
    assert contains_link("t.me/spam")
    assert not contains_link("просто текст")
    assert is_caps("ПРИВЕТ ЭТО СПАМ КАПСОМ НАПИСАНО")
    assert not is_caps("Hello")
    assert not is_caps("Это обычное сообщение без крика сегодня")


def test_welcome_escapes_name_and_appends_ad():
    text = build_welcome_text(
        "Привет, {name}",
        name="<b>Анна</b>",
        chat="Чат",
        username="@ann",
        ad_text="купи",
    )
    assert "&lt;b&gt;Анна&lt;/b&gt;" in text
    assert "купи" in text
    assert "Реклама" not in text
    assert "Я не робот" not in text

    plain = build_welcome_text(
        "",
        name="Анна",
        chat="Чат",
        username="@ann",
        ad_text="купи",
    )
    assert plain == "купи"
    assert "Привет" not in plain
    assert "входите" not in plain


def test_welcome_prompt_renders_sample():
    from bot.panel import ad_prompt, welcome_prompt

    text = welcome_prompt(
        "Привет, {name}",
        name="Иван",
        chat="Круг",
        username="@iv",
        ad_text="акция",
    )
    assert "Иван" in text
    assert "{name}" in text
    assert "акция" in text
    assert "Реклама" not in text
    assert "смотри сюда" in ad_prompt("смотри сюда")
    assert "Реклама" not in ad_prompt("смотри сюда")
    assert "пусто" in ad_prompt("  ")


def test_cycle_and_duration_label():
    assert next_in_cycle([1, 2, 3], 2) == 3
    assert next_in_cycle([1, 2, 3], 3) == 1
    assert next_in_cycle([1, 2, 3], 9) == 1
    assert human_seconds(0) == "навсегда"
    assert human_seconds(120) == "2 минуты"
    assert human_seconds(3600) == "1 час"


def test_spam_tracker():
    now = {"value": 0.0}

    def clock():
        return now["value"]

    tracker = SpamTracker(clock=clock)
    assert tracker.flood(1, 2, 3, 10) is False
    assert tracker.flood(1, 2, 3, 10) is False
    assert tracker.flood(1, 2, 3, 10) is True
    now["value"] = 11
    assert tracker.flood(1, 2, 3, 10) is False

    assert tracker.repeat(1, 2, "одинаковый текст") is False
    assert tracker.repeat(1, 2, "одинаковый текст") is False
    assert tracker.repeat(1, 2, "одинаковый текст") is True
    assert tracker.claim("notice", 1, 2, 30) is True
    assert tracker.claim("notice", 1, 2, 30) is False
    now["value"] = 50
    assert tracker.claim("notice", 1, 2, 30) is True
