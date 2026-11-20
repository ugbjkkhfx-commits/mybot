"""Production runtime hardening for start, subscription and backup delivery."""
from __future__ import annotations

import logging
import re
import threading
import time

OWNER_ID = 1520960859
LOG = logging.getLogger(__name__)
_NOTICE_INTERVAL = 30 * 60
_notice_lock = threading.Lock()
_last_notice: dict[str, float] = {}
_DATED_BACKUP_RE = re.compile(r"_[0-9]{8}T[0-9]{6}\.[0-9]+Z?\.zip$")


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


def _install_single_upload_policy(backup_service) -> None:
    """Keep dated archives locally, but send only the latest archive to Telegram."""
    if getattr(backup_service, "_runtime_single_upload_installed", False):
        return
    original_upload_pending = backup_service._upload_pending

    def upload_latest_only(path, *args, **kwargs):
        # Dated archives remain intact in the durable outbox as local history, but
        # should not be sent now or later by retry_pending. The un-dated latest file
        # is the single Telegram delivery per user/settings snapshot.
        if _DATED_BACKUP_RE.search(path.name):
            LOG.info("Retaining dated backup locally without uploading: %s", path.name)
            return False
        return original_upload_pending(path, *args, **kwargs)

    backup_service._upload_pending = upload_latest_only
    backup_service._runtime_single_upload_installed = True


def _install_registration_write_resilience(module) -> None:
    """Do not let a user-list persistence failure suppress the /start response."""
    if getattr(module, "_runtime_registration_resilience_installed", False):
        return
    original_save = module.save_json_set

    def save_user_ids(path, values):
        try:
            return original_save(path, values)
        except Exception:
            if path == module.USERS_FILE:
                LOG.exception("Could not persist registered user list; continuing /start")
                return None
            raise

    module.save_json_set = save_user_ids
    module._runtime_registration_resilience_installed = True


def _install_subscription_prompt_fallback(module) -> None:
    """Keep forced-subscription gating and show its join/check prompt on API failures."""
    if getattr(module, "_runtime_subscription_fallback_installed", False):
        return

    def check_access(user, chat_id: int, *, show_prompt: bool = True) -> bool:
        if int(user.id) == OWNER_ID:
            return True
        try:
            module.state_store.update_user_record(user.id, **module._profile(user))
            record = module._get_record(user.id)
            if record.get("permanently_banned"):
                module._send_permanent_ban(chat_id)
                return False
            config = module._subscription_config()
        except Exception:
            LOG.exception("Could not load access/subscription state for user_id=%s", user.id)
            try:
                module.bot.send_message(chat_id, "تعذر التحقق من إعدادات الدخول مؤقتًا. حاول مرة أخرى بعد قليل.")
            except Exception:
                LOG.exception("Could not send access-state error to user_id=%s", user.id)
            return False

        if not config.get("enabled"):
            return True
        if not config.get("verified") or not config.get("chat_id"):
            LOG.error("Forced subscription is enabled but channel configuration is not verified")
            if show_prompt:
                module.bot.send_message(chat_id, module.SUBSCRIPTION_NOTICE,
                                        reply_markup=module._subscription_keyboard(config))
            return False
        try:
            member = module.bot.get_chat_member(int(config["chat_id"]), int(user.id))
        except Exception:
            LOG.exception("Membership lookup failed for forced-subscription user_id=%s", user.id)
            if show_prompt:
                module.bot.send_message(chat_id, module.SUBSCRIPTION_NOTICE,
                                        reply_markup=module._subscription_keyboard(config))
            return False

        if module._is_member(member):
            if record.get("temporarily_restricted"):
                try:
                    module.state_store.set_temporary_restriction(user.id, False)
                except Exception:
                    LOG.exception("Could not clear temporary restriction for user_id=%s", user.id)
            return True
        if show_prompt:
            module.bot.send_message(chat_id, module.SUBSCRIPTION_NOTICE,
                                    reply_markup=module._subscription_keyboard(config))
        return False

    module._check_access = check_access
    module._runtime_subscription_fallback_installed = True


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
                module.bot.send_message(message.chat.id, user_error)
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
    _install_single_upload_policy(backup_service)
    _install_registration_write_resilience(module)
    _install_subscription_prompt_fallback(module)
    # Install async snapshots only after limiting uploads; retry files are retained.
    owner_backup_integration.install(module)

    _wrap_handler(module, "handle_start", "تعذر بدء البوت مؤقتًا. حاول مرة أخرى بعد قليل.")
    _wrap_handler(module, "handle_text", "حصل خطأ مؤقت أثناء معالجة رسالتك. حاول مرة أخرى بعد قليل.")

    general_commands = [
        module.telebot.types.BotCommand("start", "رسالة البدء"),
        module.telebot.types.BotCommand("help", "المساعدة"),
    ]
    owner_commands = general_commands + [
        module.telebot.types.BotCommand("stats", "إحصائيات المالك"),
        module.telebot.types.BotCommand("owner", "لوحة المالك"),
    ]
    module.bot.set_my_commands(general_commands, scope=module.telebot.types.BotCommandScopeDefault())
    module.bot.set_my_commands(
        owner_commands,
        scope=module.telebot.types.BotCommandScopeChat(chat_id=OWNER_ID),
    )
    module._runtime_fixes_installed = True
