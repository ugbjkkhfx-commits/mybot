from __future__ import annotations


def owner_contact_url(username: str | None) -> str | None:
    """Return a real Telegram profile URL only when an owner username is configured."""
    value = (username or "").strip().lstrip("@")
    if not value:
        return None
    if not value.replace("_", "a").isalnum() or not value[0].isalpha() or len(value) < 5:
        raise ValueError("Configure a valid Telegram username without inventing one")
    return f"https://t.me/{value}"


def make_contact_button(button_factory, username: str | None):
    """Build a direct Telegram link button, or an honest setup-not-configured callback."""
    url = owner_contact_url(username)
    if url:
        return button_factory("📩 التواصل مع المالك", url=url)
    return button_factory("📩 التواصل مع المالك", callback_data="contact_owner")


def handle_unconfigured_contact(bot, chat_id: int) -> None:
    bot.send_message(
        chat_id,
        "لم يُضف اسم مستخدم Telegram للمالك بعد. عند ضبط OWNER_TELEGRAM_USERNAME سيؤدي الزر إلى فتح محادثته الخاصة على Telegram لتكتب له مباشرة.",
    )
