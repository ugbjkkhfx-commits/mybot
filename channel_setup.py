"""Reliable Telegram channel setup overlay for the social downloader bot."""
from __future__ import annotations

import logging
import re
import urllib.parse

logger = logging.getLogger(__name__)
_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}\Z")


def parse_public_channel(text: str):
    """Return (username, canonical public link), or None for a non-public link."""
    value = (text or "").strip()
    if value.startswith("@"):
        username = value[1:]
    else:
        candidate = value if "://" in value else "https://" + value
        try:
            parsed = urllib.parse.urlsplit(candidate)
            if parsed.scheme not in ("http", "https") or parsed.hostname not in ("t.me", "telegram.me"):
                return None
            if parsed.username or parsed.password or parsed.port:
                return None
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 1 or not parts[0] or parsed.query or parsed.fragment:
                return None
            username = parts[0]
        except ValueError:
            return None
    if not _USERNAME_RE.fullmatch(username):
        return None
    return username, f"https://t.me/{username}"


def normalize_invite_link(text: str) -> str | None:
    """Validate and preserve Telegram's current (+hash) and legacy invite URLs."""
    value = (text or "").strip()
    candidate = value if "://" in value else "https://" + value
    try:
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("t.me", "telegram.me"):
            return None
        if parsed.username or parsed.password or parsed.port:
            return None
        valid_path = bool(re.fullmatch(r"/\+[A-Za-z0-9_-]+/?", parsed.path)) or bool(
            re.fullmatch(r"/joinchat/[A-Za-z0-9_-]+/?", parsed.path)
        )
        if not valid_path:
            return None
    except ValueError:
        return None
    return value if "://" in value else "https://" + value


def _reason(exc: Exception) -> str:
    detail = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {detail}"[:500]


def install(bot_module) -> None:
    """Install channel resolution/verification before starting the bot polling loop."""
    def verify_and_save(message, data: dict) -> None:
        try:
            chat_id = int(data["chat_id"])
            # Resolve by ID too, including forwarded private channels, and ensure the
            # target is really a channel before checking subscription permissions.
            chat = bot_module.bot.get_chat(chat_id)
            if getattr(chat, "type", None) != "channel":
                bot_module.bot.reply_to(message, "لم يتم الحفظ: المحادثة التي أرسلتها ليست قناة Telegram.")
                return
            me = bot_module.bot.get_me()
            member = bot_module.bot.get_chat_member(chat_id, me.id)
            if getattr(member, "status", "") not in ("administrator", "creator"):
                bot_module.bot.reply_to(
                    message,
                    "لم يتم الحفظ: البوت ليس مشرفًا في القناة. أضِفه مشرفًا حتى يستطيع فحص الاشتراك ومراقبة المغادرة.",
                )
                return
            # Verify that Telegram allows the bot to query membership, without changing
            # channel privacy or requiring the channel to be private.
            bot_module.bot.get_chat_member(chat_id, bot_module.ADMIN_ID)
        except Exception as exc:
            logger.warning("Channel verification failed: %s", exc)
            bot_module.bot.reply_to(message, f"تعذر التحقق من القناة أو الصلاحيات: {_reason(exc)}")
            return

        previous = bot_module._subscription_config()
        config = {
            "chat_id": chat_id,
            "title": str(data.get("title") or getattr(chat, "title", "قناة")),
            "public_link": data.get("public_link"),
            "invite_link": data.get("invite_link"),
            "enabled": bool(previous.get("enabled", True)),
            "verified": True,
        }
        bot_module.state_store.save_subscription(config)
        bot_module.admin_states.pop(bot_module.ADMIN_ID, None)
        bot_module.bot.reply_to(
            message,
            "✅ تم التحقق من القناة وحفظها.\n" + bot_module._subscription_status_text(),
            parse_mode="HTML",
            reply_markup=bot_module._subscription_admin_keyboard(),
        )

    def resolve_channel_setup(message) -> None:
        state, data = bot_module.admin_states.get(bot_module.ADMIN_ID, (None, {}))
        if state != "channel":
            return
        forwarded = getattr(message, "forward_from_chat", None)
        origin = getattr(message, "forward_origin", None)
        if forwarded is None and origin is not None:
            forwarded = getattr(origin, "chat", None)
        if forwarded is not None:
            if getattr(forwarded, "type", None) != "channel":
                bot_module.bot.reply_to(message, "الرسالة المعاد توجيهها ليست من قناة Telegram.")
                return
            data.update(chat_id=int(forwarded.id), title=getattr(forwarded, "title", "قناة خاصة"))
            bot_module.admin_states[bot_module.ADMIN_ID] = ("invite", data)
            bot_module.bot.reply_to(message, "تم التعرف على القناة. أرسل رابط الدعوة t.me/+... أو t.me/joinchat/...")
            return

        parsed = parse_public_channel(getattr(message, "text", "") or "")
        if parsed is None:
            bot_module.bot.reply_to(
                message,
                "الرابط غير صالح. أرسل https://t.me/Username أو @Username، أو أعد توجيه منشور من القناة الخاصة.",
            )
            return
        username, public_link = parsed
        try:
            chat = bot_module.bot.get_chat("@" + username)
        except Exception as exc:
            logger.warning("Could not resolve public channel @%s: %s", username, exc)
            bot_module.bot.reply_to(message, f"تعذر الوصول إلى القناة @{username}: {_reason(exc)}")
            return
        if getattr(chat, "type", None) != "channel":
            bot_module.bot.reply_to(message, "الرابط لا يشير إلى قناة Telegram.")
            return
        data.update(
            chat_id=int(chat.id),
            title=getattr(chat, "title", username),
            public_link=public_link,
            invite_link=None,
        )
        verify_and_save(message, data)

    bot_module._parse_channel_link = parse_public_channel
    bot_module._verify_and_save_channel = verify_and_save
    bot_module._resolve_channel_setup = resolve_channel_setup
