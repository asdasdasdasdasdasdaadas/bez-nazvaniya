from __future__ import annotations

from html import escape

from aiogram import Bot
from aiogram.types import User

from bot.captcha import FAIL_TEXT, check_body
from bot.logic import (
    AD_EVERY_MESSAGES,
    AD_KEEP_MESSAGES,
    ad_message_text,
    cooldown_label,
    human_seconds,
)
from bot.ops import mention, send_html
from bot.panel import SPAM_REASON, ad_keyboard, sample_check_keyboard


def ads_inside(ads: dict) -> bool:
    return bool(ads.get("enabled") and ads.get("in_welcome") and (ads.get("text") or "").strip())


def ads_separate(ads: dict) -> bool:
    return bool(ads.get("enabled") and ads.get("separate") and (ads.get("text") or "").strip())


async def send_member_preview(
    bot: Bot,
    viewer: User,
    *,
    title: str,
    template: str | None,
    ads: dict,
    fail_mode: str,
    spam_action: str,
    spam_mute_seconds: int,
    captcha_enabled: bool = True,
    sample_chat: bool = False,
) -> None:
    name = viewer.full_name or viewer.first_name or "участник"
    username = f"@{viewer.username}" if viewer.username else "без username"
    chat_title = title or "чат"
    intro = (
        f"<b>Превью · {escape(chat_title)}</b>\n"
        "Так сообщение выглядит в чате. Кнопка «Я не робот» здесь ничего не делает. "
        "Если в настройках чата «Видит: только новичок», в чате его видит только сам участник. "
        "В личку это не отправляется."
    )
    if sample_chat:
        intro += "\nИмя и название чата подставлены как пример."
    if not captcha_enabled:
        intro += "\nПроверка в этом чате выключена, участникам это сообщение не отправляется."
    await send_html(bot, viewer.id, intro)

    inside = ads_inside(ads)
    chat = {"welcome_text": template, "title": chat_title}
    body = check_body(chat, name, username, ads)
    lead = f"{mention(viewer.id, name)}, подтвердите, что вы человек."
    await send_html(
        bot,
        viewer.id,
        f"{lead}\n\n{body}" if body else lead,
        sample_check_keyboard(ads, inside),
    )

    raw = (ads.get("text") or "").strip()
    if not raw:
        await send_html(bot, viewer.id, "Текст рекламы пустой, пост в чат не отправляется.")
    elif ads_separate(ads):
        await send_html(
            bot,
            viewer.id,
            f"<i>Отдельный пост каждые {AD_EVERY_MESSAGES} сообщений. "
            f"Предыдущий удаляется через {AD_KEEP_MESSAGES}.</i>\n\n"
            + ad_message_text(raw),
            ad_keyboard(ads),
            preview=True,
        )
    else:
        await send_html(bot, viewer.id, "Отдельный пост в чат сейчас выключен.")

    if raw and ads.get("enabled") and ads.get("personal"):
        await send_html(
            bot,
            viewer.id,
            "<i>Лично автору сообщения, "
            f"{cooldown_label(int(ads.get('personal_cooldown') or 0))}. "
            "В чате это видит только он.</i>\n\n"
            + ad_message_text(raw),
            ad_keyboard(ads),
            preview=True,
        )

    fail = FAIL_TEXT.get(fail_mode, FAIL_TEXT["kick"])
    await send_html(bot, viewer.id, "<i>Если кнопку не нажать</i>\n\n" + escape(fail))

    notice = f"{mention(viewer.id, name)}: {SPAM_REASON['links']}. Сообщение удалено."
    if spam_action == "mute":
        notice += f" Мут на {human_seconds(int(spam_mute_seconds))}."
    elif spam_action == "ban":
        notice += " Бан."
    await send_html(bot, viewer.id, "<i>Замечание антиспама</i>\n\n" + notice)
