from __future__ import annotations

"""Owner-only controls and startup wiring for encrypted Telegram backups."""

import base64
import contextvars
import hashlib
import json
import logging
import os
import re
from pathlib import Path

OWNER_ID = 1520960859
_backup_wrapped = False
_owner_view = contextvars.ContextVar("owner_view", default=False)
_log = logging.getLogger(__name__)


def owner_button_visible(user_id: int) -> bool:
    return int(user_id) == OWNER_ID


def build_owner_button(bot_module, user_id: int):
    """Return the owner settings button only for the configured owner."""
    if not owner_button_visible(user_id):
        return None
    return bot_module.telebot.types.InlineKeyboardButton(
        "⚙️ إعدادات المالك", callback_data="owner_settings"
    )


def add_owner_button(markup, bot_module, user_id: int):
    button = build_owner_button(bot_module, user_id)
    if button is not None:
        markup.add(button)
    return markup


def _replace_handler(handler_list, name: str, replacement) -> bool:
    for group in handler_list:
        if group.get("function").__name__ == name:
            group["function"] = replacement
            return True
    return False


def _password_for_user(user_id: int) -> bytes:
    # ZIP password is the requested exact per-user value. The archive payload is
    # independently encrypted with AES-GCM using BACKUP_MASTER_KEY.
    return f"*{int(user_id)}#".encode("utf-8")


def _install_master_encryption(backup_service) -> None:
    """Require both the exact per-user password and a secret hosting master key."""
    if getattr(backup_service, "_owner_crypto_installed", False):
        return
    original_create = backup_service.create_encrypted_archive
    original_read = backup_service.read_encrypted_archive

    def master_key() -> bytes:
        secret = os.environ.get("BACKUP_MASTER_KEY", "").encode("utf-8")
        if len(secret) < 32:
            raise RuntimeError("BACKUP_MASTER_KEY must contain at least 32 random bytes")
        return hashlib.sha256(b"telegram-backup-inner-v1\0" + secret).digest()

    def create(payload, password: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        clear = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        encrypted = AESGCM(master_key()).encrypt(nonce, clear, b"mybot-backup-v1")
        envelope = {"format": "AES-GCM-1", "nonce": base64.b64encode(nonce).decode("ascii"),
                    "ciphertext": base64.b64encode(encrypted).decode("ascii")}
        return original_create(envelope, password)

    def read(data: bytes, password: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        envelope = original_read(data, password)
        if envelope.get("format") != "AES-GCM-1":
            raise ValueError("Unsupported or legacy backup format")
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        clear = AESGCM(master_key()).decrypt(nonce, ciphertext, b"mybot-backup-v1")
        payload = json.loads(clear.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Backup payload must be an object")
        return payload

    backup_service.create_encrypted_archive = create
    backup_service.read_encrypted_archive = read
    backup_service._file_password = lambda user_id, _key: _password_for_user(user_id)
    backup_service._owner_crypto_installed = True


def _restore_document_handler(bot_module, backup_service, message) -> None:
    user = getattr(message, "from_user", None)
    if not user or int(user.id) != OWNER_ID:
        return
    document = getattr(message, "document", None)
    if not document:
        return
    match = re.fullmatch(r"([A-Za-z0-9]{1,12})_([0-9]+)(?:_[0-9TZ.]+)?\.zip", document.file_name or "")
    if not match or match.group(1) != backup_service._bot_code():
        bot_module.bot.reply_to(message, "ملف النسخة ليس تابعًا لهذا البوت أو اسمه غير صالح.")
        return
    target_user_id = int(match.group(2))
    try:
        remote_file = bot_module.bot.get_file(document.file_id)
        encrypted_data = bot_module.bot.download_file(remote_file.file_path)
        restored = backup_service.restore_user_archive(
            encrypted_data, target_user_id, requester_id=OWNER_ID,
            data_dir=Path(os.environ.get("BOT_DATA_DIR", ".")),
        )
        bot_module.bot.reply_to(message, "✅ تم التحقق من النسخة. تمت استعادة البيانات الناقصة فقط." if restored
                                else "✅ تم التحقق من النسخة؛ لا توجد بيانات ناقصة لاستعادتها.")
    except Exception as exc:
        _log.exception("Owner backup restore failed")
        bot_module.bot.reply_to(message, f"تعذرت الاس��عادة ولم تُستبدل البيانات المحلية: {exc}")


def install(bot_module) -> None:
    """Wire admin-only keyboard, command, restore flow, and durable backup retry."""
    global _backup_wrapped
    if getattr(bot_module, "_owner_backup_integration_installed", False):
        return
    import backup_service

    _install_master_encryption(backup_service)
    original_keyboard = bot_module._main_keyboard

    def owner_aware_keyboard():
        markup = original_keyboard()
        if _owner_view.get():
            add_owner_button(markup, bot_module, OWNER_ID)
        return markup

    bot_module._main_keyboard = owner_aware_keyboard
    original_start = bot_module.handle_start

    def start_with_owner_button(message):
        token = _owner_view.set(owner_button_visible(message.from_user.id))
        try:
            return original_start(message)
        finally:
            _owner_view.reset(token)

    bot_module.handle_start = start_with_owner_button
    _replace_handler(bot_module.bot.message_handlers, "handle_start", start_with_owner_button)

    original_callbacks = bot_module.handle_callbacks

    def callbacks_with_owner_button(call):
        if call.data == "backup_restore_help":
            if int(call.from_user.id) != OWNER_ID:
                bot_module.bot.answer_callback_query(call.id, "هذا القسم للمالك فقط.", show_alert=True)
                return
            bot_module.bot.answer_callback_query(call.id)
            bot_module.bot.send_message(
                call.message.chat.id,
                "📦 لاستعادة نسخة احتياطية: أعد توجيه ملف ZIP من قناة النسخ إلى هذه المحادثة الخاصة، ثم أرسل /restore. "
                "سيتم التحقق من رمز البوت والتشفير والمالك، ولن تُستبدل أي سجلات محلية موجودة.",
            )
            return
        token = _owner_view.set(owner_button_visible(call.from_user.id))
        try:
            return original_callbacks(call)
        finally:
            _owner_view.reset(token)

    bot_module.handle_callbacks = callbacks_with_owner_button
    _replace_handler(bot_module.bot.callback_query_handlers, "handle_callbacks", callbacks_with_owner_button)

    # /owner is explicitly rebound to avoid command-filter/registration ordering
    # issues, and always opens the existing owner dashboard for the owner only.
    def owner_command(message):
        if message.chat.type == "private" and int(message.from_user.id) == OWNER_ID:
            bot_module.bot.send_message(message.chat.id, "لوحة المالك", reply_markup=bot_module._admin_main_keyboard())

    if not _replace_handler(bot_module.bot.message_handlers, "handle_owner", owner_command):
        bot_module.bot.message_handler(commands=["owner"])(owner_command)

    def restore_command(message):
        if int(message.from_user.id) == OWNER_ID and message.chat.type == "private":
            bot_module.bot.reply_to(message, "أعد توجيه ملف النسخة الاحتياطية ZIP من قناة النسخ الاحتياطي إلى هذه المحادثة.")

    bot_module.bot.message_handler(commands=["restore"])(restore_command)
    bot_module.bot.message_handler(content_types=["document"])(
        lambda message: _restore_document_handler(bot_module, backup_service, message)
    )

    # Add a restoration entry to the existing owner settings, without changing
    # subscription, appeals, moderation, or downloader flows.
    original_owner_settings = bot_module._owner_settings_keyboard

    def owner_settings_with_restore():
        markup = original_owner_settings()
        markup.add(bot_module.telebot.types.InlineKeyboardButton("📦 استعادة نسخة احتياطية", callback_data="backup_restore_help"))
        return markup

    bot_module._owner_settings_keyboard = owner_settings_with_restore

    try:
        data_dir = Path(os.environ.get("BOT_DATA_DIR", "."))
        backup_service.check_configuration_and_notify_owner()
        backup_service.start_background_retry(data_dir)
    except Exception:
        _log.exception("Could not initialize Telegram backup service")

    bot_module._owner_backup_integration_installed = True
    _backup_wrapped = True
