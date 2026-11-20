"""Production runtime hardening for start responses, commands and backup notices."""
from __future__ import annotations

import logging
import threading
import time

OWNER_ID = 1520960859
LOG = logging.getLogger(__name__)
_NOTICE_INTERVAL = 30 * 60
_notice_lock = threading.Lock()
_last_notice: dict[str, float] = {}


def _notice_category(text: str) -> str:
    lowered = text.lower()
    if "backup_master_key" in lowered:
        return "backup-master-key"
    if "backup" in lowered or "نسخ" in text:
        return "backup-general"
    return text.split(":", 1)[0][:100]


def _install_throttled_owner_notices(backup_service) -> None:
    if getattr(backup_service, "_runtime_notice_throttle_installed", False):
        return
    original_notify = backup_service._notify_owner

    def notify_owner_limited(text: str) -> None:
        # _notify_owner is owner-only by construction; rate-limit noisy repeat errors
        # while leaving full diagnostics in the hosting logs.
        category = _notice_category(str(text))
        now = time.monotonic()
        with _notice_lock:
            if now - _last_notice.get(category, 0.0) < _NOTICE_INTERVAL:
                LOG.warning("Suppressed repeated owner backup notice (%s): %s", category, text)
                return
            _last_notice[category] = now
        original_notify(str(text))

    backup_service._notify_owner = notify_owner_limited
    backup_service._runtime_notice_throttle_installed = True


def _wrap_handler(module, name: str, user_error: str) -> None:
    original = getattr(module, name)

    def guarded(message):
        try:
            return original(message)
        except Exception:
            user = getattr(message, "from_user", None)
            user_id = getattr(user, "id", "unknown")
            LOG.exception("Unhandled %s failure for user_id=%s", name, user_id)
            try:
                module.bot.send_message(
                    message.chat.id,
                    user_error,
                )
            except Exception:
                LOG.exception("Could not send safe %s failure response to user_id=%s", name, user_id)

    guarded.__name__ = f"guarded_{name}"
    setattr(module, name, guarded)
    for group in module.bot.message_handlers:
        if group.get("function") is original:
            group["function"] = guarded
            break


def install(module) -> None:
    """Install safeguards before polling begins; safe to call once per process."""
    if getattr(module, "_runtime_fixes_installed", False):
        return

    import backup_service
    import owner_backup_integration

    _install_throttled_owner_notices(backup_service)
    # This wires asynchronous encrypted snapshots and the durable retry queue before
    # any user handler writes state (so backups cannot stall /start handling).
    owner_backup_integration.install(module)

    _wrap_handler(
        module,
        "handle_start",
        "تعذر بدء البوت مؤقتًا. حاول مرة أخرى بعد قليل.",
    )
    _wrap_handler(
        module,
        "handle_text",
        "حصل خطأ مؤقت أثناء معالجة رسالتك. حاول مرة أخرى بعد قليل.",
    )

    general_commands = [
        module.telebot.types.BotCommand("start", "رسالة البدء"),
        module.telebot.types.BotCommand("help", "المساعدة"),
    ]
    owner_commands = general_commands + [
        module.telebot.types.BotCommand("stats", "إحصائيات المالك"),
        module.telebot.types.BotCommand("owner", "لوحة المالك"),
    ]
    module.bot.set_my_commands(
        general_commands,
        scope=module.telebot.types.BotCommandScopeDefault(),
    )
    module.bot.set_my_commands(
        owner_commands,
        scope=module.telebot.types.BotCommandScopeChat(chat_id=OWNER_ID),
    )
    module._runtime_fixes_installed = True
