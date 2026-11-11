from __future__ import annotations

from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.logic import (
    DEFAULT_WELCOME,
    ad_message_text,
    build_welcome_text,
    human_seconds,
    short_title,
)
from bot.logic import ADS_PER_DAY, AD_MIN_GAP

ADD_RIGHTS = "delete_messages+restrict_members"

FAIL_LABEL = {
    "kick": "кик",
    "ban": "бан",
    "mute": "остаётся без права писать",
}
ACTION_LABEL = {
    "delete": "удалить",
    "mute": "мут",
    "ban": "бан",
}
SPAM_REASON = {
    "links": "ссылки здесь нельзя",
    "forwards": "пересылки здесь нельзя",
    "flood": "слишком быстро пишете",
    "caps": "слишком много капса",
    "repeat": "одинаковые сообщения",
    "stickers": "слишком много стикеров",
    "newcomer_links": "новым участникам пока нельзя ссылки",
    "newcomer_forwards": "новым участникам пока нельзя пересылки",
}


def on(flag: int) -> str:
    return "вкл" if flag else "выкл"


def add_to_group_url(username: str) -> str:
    return f"https://t.me/{username}?startgroup&admin={ADD_RIGHTS}"


def verify_url(username: str, packed_chat_id: str) -> str:
    return f"https://t.me/{username}?start=v_{packed_chat_id}"


def panel_url(username: str, packed_chat_id: str) -> str:
    return f"https://t.me/{username}?start=c_{packed_chat_id}"


def _rows(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def ad_url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url, style="success")


def ads_line(ads: dict) -> str:
    if not ads["enabled"]:
        return "Реклама создателя бота: выключена."
    if not (ads["text"] or "").strip():
        return "Реклама создателя бота: включена, но текст ещё не задан."
    parts = []
    if ads["in_welcome"]:
        parts.append("в проверке")
    if ads.get("personal"):
        parts.append(f"каждому участнику до {ADS_PER_DAY} раз в день")
    if not parts:
        return "Реклама создателя бота: включена, места показа выключены."
    return (
        "Реклама создателя бота: " + " и ".join(parts) + ". "
        "Видит только сам человек, админам и участникам с тегом не показывается."
    )


def rights_line(missing: list[str]) -> str:
    if not missing:
        return "Права бота: удаление, мут и бан на месте."
    return "Не хватает прав: " + ", ".join(missing) + "."


def home_text(count: int) -> str:
    if count == 0:
        where = "Пока нет подключённых чатов. Нажмите «Добавить в группу»."
    else:
        where = "Выберите чат ниже — откроются настройки."
    return (
        "<b>Модератор чата и комментариев</b>\n\n"
        "1. Добавьте бота в группу. Кнопка сразу запросит права администратора.\n"
        "2. Новый участник видит в чате проверку с приветствием и нажимает «Я не робот».\n"
        "3. В настройках задаётся, что считать спамом.\n"
        "4. В самом чате работают мут, бан, кик и предупреждения.\n\n"
        "Для комментариев канала добавьте бота администратором в группу обсуждения.\n"
        "Если чата нет в списке, напишите в нём /settings.\n\n"
        f"{where}"
    )


def home_keyboard(chats: list[dict], is_owner: bool, username: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for chat in chats[:30]:
        title = short_title(chat["title"] or "Чат", 32)
        if not int(chat.get("moderation_enabled", 1)):
            title = f"{title} · выкл"
        rows.append([btn(title, f"c:{chat['chat_id']}")])
    rows.append([url_btn("Добавить в группу", add_to_group_url(username))])
    if is_owner:
        rows.append([btn("Реклама", "o")])
    rows.append([btn("Как это работает", "help")])
    return _rows(rows)


def chat_text(chat: dict, ads: dict, missing: list[str]) -> str:
    title = escape(chat["title"] or "чат")
    welcome = "свой текст" if (chat["welcome_text"] or "").strip() else "нет"
    service = "удаляются" if chat["delete_service"] else "остаются"
    check = "включена" if chat["captcha_enabled"] else "выключена"
    seen_by = "только новичок" if int(chat.get("captcha_private", 1)) else "весь чат"
    if int(chat.get("moderation_enabled", 1)):
        paused = ""
    else:
        paused = "Модерация выключена. Бот в чате молчит, настройки сохранены.\n\n"
    return (
        f"<b>{title}</b>\n\n"
        f"{paused}"
        f"Проверка «Я не робот»: {check}\n"
        f"Проверку видит: {seen_by}\n"
        f"Время на кнопку: {human_seconds(int(chat['captcha_timeout']))}\n"
        f"Если не нажал: {FAIL_LABEL.get(chat['captcha_fail'], chat['captcha_fail'])}\n"
        f"Приветствие: {welcome}\n"
        f"Сообщения о входе: {service}\n\n"
        f"{ads_line(ads)}\n"
        f"{rights_line(missing)}"
    )


def chat_keyboard(chat: dict, username: str, missing: list[str]) -> InlineKeyboardMarkup:
    chat_id = chat["chat_id"]
    paused = not int(chat.get("moderation_enabled", 1))
    rows = [
        [btn("Включить модерацию" if paused else "Отключить модерацию", f"t:{chat_id}:mod")],
        [btn(f"Проверка: {on(chat['captcha_enabled'])}", f"t:{chat_id}:cap")],
        [btn(
            "Видит: только новичок" if int(chat.get("captcha_private", 1)) else "Видит: весь чат",
            f"t:{chat_id}:prv",
        )],
        [btn(f"Если не нажал: {FAIL_LABEL.get(chat['captcha_fail'], 'кик')}", f"y:{chat_id}:fail")],
        [btn(f"Время: {human_seconds(int(chat['captcha_timeout']))}", f"y:{chat_id}:cto")],
        [btn("Текст приветствия", f"w:{chat_id}")],
        [btn("Превью для участника", f"p:{chat_id}")],
        [btn(
            "Вход в чат: скрывать" if chat["delete_service"] else "Вход в чат: показывать",
            f"t:{chat_id}:svc",
        )],
        [btn("Антиспам", f"s:{chat_id}")],
        [btn("Мут, бан и команды", f"h:{chat_id}")],
    ]
    if missing:
        rows.append([url_btn("Выдать права", add_to_group_url(username))])
    rows.append([btn("Все чаты", "m")])
    return _rows(rows)


def spam_text(chat: dict) -> str:
    title = escape(chat["title"] or "чат")
    return (
        f"<b>Антиспам · {title}</b>\n\n"
        "Нажмите строку, чтобы сменить значение.\n"
        "Флуд — это сколько сообщений подряд человек может прислать за окно времени.\n"
        "Стикеры срабатывают, если их больше трёх за 15 секунд.\n"
        "Новичок — тот, кто только что нажал «Я не робот». "
        "Пока идёт его срок, ссылки и пересылки удаляются даже если общие фильтры выключены.\n"
        "Варны копятся командой /warn. На нуле предупреждения выключены."
    )


def spam_keyboard(chat: dict) -> InlineKeyboardMarkup:
    chat_id = chat["chat_id"]
    newbie = int(chat["newcomer_seconds"])
    newbie_label = "выкл" if newbie <= 0 else human_seconds(newbie)
    warns = int(chat["warn_limit"])
    warn_label = "выкл" if warns <= 0 else str(warns)
    return _rows([
        [btn(f"Ссылки: {on(chat['spam_links'])}", f"t:{chat_id}:lnk")],
        [btn(f"Пересылки: {on(chat['spam_forwards'])}", f"t:{chat_id}:fwd")],
        [btn(f"Повторы: {on(chat['spam_repeats'])}", f"t:{chat_id}:rep")],
        [btn(f"КАПС: {on(chat['spam_caps'])}", f"t:{chat_id}:cps")],
        [btn(f"Стикеры: {on(chat['spam_stickers'])}", f"t:{chat_id}:stk")],
        [btn(f"Флуд: {chat['spam_flood_count']} сообщ.", f"y:{chat_id}:flc")],
        [btn(f"Окно флуда: {chat['spam_flood_window']} сек", f"y:{chat_id}:flw")],
        [btn(f"Наказание: {ACTION_LABEL.get(chat['spam_action'], chat['spam_action'])}", f"y:{chat_id}:act")],
        [btn(f"Мут за спам: {human_seconds(int(chat['spam_mute_seconds']))}", f"y:{chat_id}:mut")],
        [btn(f"Новички: {newbie_label}", f"y:{chat_id}:new")],
        [btn(f"Варны до наказания: {warn_label}", f"y:{chat_id}:wln")],
        [btn(f"За варны: {ACTION_LABEL.get(chat['warn_action'], chat['warn_action'])}", f"y:{chat_id}:wac")],
        [btn("К чату", f"c:{chat_id}")],
    ])


def commands_text() -> str:
    return (
        "<b>Команды в группе</b>\n\n"
        "Пишите ответом на сообщение человека или укажите @username.\n\n"
        "/mute — мут\n"
        "/mute 30m — на 30 минут\n"
        "/mute 2ч спам — на 2 часа, причина «спам»\n"
        "/ban — бан навсегда\n"
        "/ban 1д — бан на день\n"
        "/kick — исключить\n"
        "/unmute — снять мут\n"
        "/unban — разбанить\n"
        "/warn — предупреждение\n"
        "/log — журнал\n"
        "/settings — эта панель\n\n"
        "По-русски тоже можно: /мут /бан /кик /размут /разбан /варн /журнал /настройки.\n\n"
        "Время: 30m, 2ч, 1д, 30 минут, 2 часа, навсегда.\n"
        "Если у /mute время не указано, берётся срок мута из антиспама. "
        "/ban без времени — навсегда.\n\n"
        "Нужно право ограничивать участников. Обычный мут работает в супергруппах."
    )


def help_text() -> str:
    return (
        "<b>Как пользоваться</b>\n\n"
        "Сначала откройте бота и добавьте его в группу кнопкой. "
        "После этого у админов группы в /start появляется этот чат.\n\n"
        "Новому участнику бот закрывает отправку, пока он не нажмёт «Я не робот» в чате.\n\n"
        "Антиспам видит обычные сообщения, только если в @BotFather выключен Privacy Mode: "
        "/setprivacy → Disable. После этого бота нужно удалить из чата и добавить снова.\n\n"
        "Комментарии канала живут в группе обсуждения. Добавьте бота админом туда же."
    )


def owner_text(ads: dict) -> str:
    text = (ads["text"] or "").strip()
    preview = "не задан" if not text else f"задан, {len(text)} симв."
    if ads["button_url"]:
        button = f"{ads['button_text'] or 'Открыть'}"
    else:
        button = "нет"
    return (
        "<b>Реклама создателя бота</b>\n\n"
        "Вся реклама эфемерная: в группе её видит только тот, кому она показана. "
        "Обычных сообщений с рекламой бот не отправляет.\n\n"
        "В проверке — блок в сообщении «Я не робот», которое видит только новичок. "
        "Если в чате проверку видят все, рекламы в ней нет.\n"
        f"Каждому в чате — до {ADS_PER_DAY} раз в день на человека, "
        f"не чаще раза в {human_seconds(AD_MIN_GAP)}. Показ в момент, когда человек "
        "пишет, исправляет сообщение или ставит реакцию: он в сети, и реклама доходит. "
        "Счётчик обнуляется в полночь по Москве, реклама в проверке тоже считается. "
        "Админам, владельцу и участникам с тегом не показывается.\n\n"
        f"Общий выключатель: {on(ads['enabled'])}\n"
        f"В проверке: {on(ads['in_welcome'])}\n"
        f"Каждому в чате: {on(ads.get('personal', 0))}\n"
        f"Текст: {preview}\n"
        f"Кнопка: {escape(button)}"
    )


def owner_keyboard(ads: dict) -> InlineKeyboardMarkup:
    return _rows([
        [btn(f"Реклама: {on(ads['enabled'])}", "ot:en")],
        [btn(f"В проверке: {on(ads['in_welcome'])}", "ot:iw")],
        [btn(f"Каждому в чате: {on(ads.get('personal', 0))}", "ot:pm")],
        [btn("Текст рекламы", "ow")],
        [btn("Кнопка", "ob")],
        [btn("Превью писем", "op")],
        [btn("В меню", "m")],
    ])


def welcome_prompt(
    current: str | None,
    *,
    name: str = "Имя",
    chat: str = "Название чата",
    username: str = "@username",
    ad_text: str | None = None,
) -> str:
    template = (current or "").strip() or DEFAULT_WELCOME
    shown = template if len(template) <= 800 else template[:800] + "…"
    rendered = build_welcome_text(
        template,
        name=name,
        chat=chat,
        username=username,
        ad_text=ad_text,
    )
    if len(rendered) > 1500:
        rendered = rendered[:1500] + "…"
    return (
        "Пришлите новый текст приветствия.\n"
        "Можно использовать {name}, {chat} и {username}.\n"
        "Чтобы убрать свой текст, пришлите минус: -\n\n"
        "Сейчас:\n"
        f"<pre>{escape(shown)}</pre>\n\n"
        "Так это увидит участник:\n"
        f"{rendered}"
    )


def ad_prompt(current: str) -> str:
    raw = current.strip()
    shown = raw or "пусто"
    if len(shown) > 500:
        shown = shown[:500] + "…"
    rendered = ""
    if raw:
        sample = ad_message_text(raw)
        if len(sample) > 1200:
            sample = sample[:1200] + "…"
        rendered = f"\n\nТак это увидят:\n{sample}"
    return (
        "Пришлите текст рекламы, до 1000 символов.\n"
        "Чтобы убрать текст, пришлите минус: -\n\n"
        "Сейчас:\n"
        f"<pre>{escape(shown)}</pre>"
        f"{rendered}"
    )


def button_prompt(ads: dict) -> str:
    if ads["button_url"]:
        current = f"{ads['button_text'] or 'Открыть'} | {ads['button_url']}"
    else:
        current = "нет"
    return (
        "Пришлите подпись и ссылку через вертикальную черту.\n"
        "Пример: Наш канал | https://t.me/durov\n"
        "Чтобы убрать кнопку, пришлите минус: -\n\n"
        f"Сейчас: {escape(current)}"
    )


def sample_check_keyboard(ads: dict, include_ad_button: bool) -> InlineKeyboardMarkup:
    rows = [[btn("Я не робот", "preview")]]
    if include_ad_button and ads.get("button_url"):
        rows.append([ad_url_btn((ads.get("button_text") or "Открыть")[:40], ads["button_url"])])
    return _rows(rows)


def button_editor_keyboard(ads: dict) -> InlineKeyboardMarkup:
    rows = []
    if ads.get("button_url"):
        rows.append([ad_url_btn((ads.get("button_text") or "Открыть")[:40], ads["button_url"])])
    rows.append([btn("Отмена", "x")])
    return _rows(rows)


def check_keyboard(chat_id: int, user_id: int, ads: dict, include_ad_button: bool) -> InlineKeyboardMarkup:
    rows = [[btn("Я не робот", f"vr:{chat_id}:{user_id}")]]
    if include_ad_button and ads.get("button_url"):
        rows.append([ad_url_btn((ads.get("button_text") or "Открыть")[:40], ads["button_url"])])
    return _rows(rows)


def ad_keyboard(ads: dict) -> InlineKeyboardMarkup | None:
    if not ads.get("button_url"):
        return None
    return _rows([[ad_url_btn((ads.get("button_text") or "Открыть")[:40], ads["button_url"])]])




def cancel_keyboard() -> InlineKeyboardMarkup:
    return _rows([[btn("Отмена", "x")]])


def undo_keyboard(kind: str, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    prefix = "um" if kind == "mute" else "ub"
    label = "Снять мут" if kind == "mute" else "Разбанить"
    return _rows([[btn(label, f"{prefix}:{chat_id}:{user_id}")]])
