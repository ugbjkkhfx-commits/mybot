import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

import backup_service
import runtime_fixes


class RuntimeFixesTests(unittest.TestCase):
    def test_handler_exception_is_logged_and_returns_safe_message(self):
        module = SimpleNamespace()
        module.bot = SimpleNamespace(message_handlers=[], send_message=Mock())
        module.handle_start = Mock(side_effect=RuntimeError("internal details"))
        module.bot.message_handlers = [{"function": module.handle_start}]
        message = SimpleNamespace(from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=42))
        runtime_fixes._wrap_handler(module, "handle_start", "safe response")
        module.handle_start(message)
        module.bot.send_message.assert_called_once_with(42, "safe response")
        self.assertIs(module.bot.message_handlers[0]["function"], module.handle_start)

    def test_backup_notice_categories_are_stable(self):
        self.assertEqual(runtime_fixes._notice_category("BACKUP_MASTER_KEY must be configured with 32 bytes"), "backup-master-key")
        self.assertEqual(runtime_fixes._notice_category("Backup upload failed: first"), "backup-general")

    def test_dated_backup_remains_local_and_latest_backup_uploads(self):
        service = SimpleNamespace(_upload_pending=Mock(return_value=True))
        runtime_fixes._install_single_upload_policy(service)
        with TemporaryDirectory() as directory:
            dated = Path(directory) / "A_42_20261120T120000.123456Z.zip"
            latest = Path(directory) / "A_42.zip"
            self.assertFalse(service._upload_pending(dated))
            self.assertTrue(service._upload_pending(latest))
        service._upload_pending.assert_called_once_with(latest)

    def make_subscription_module(self, *, update_error=None, record_error=None):
        config = {"enabled": True, "verified": True, "chat_id": -100123, "public_link": "https://t.me/channel"}
        state = SimpleNamespace(
            update_user_record=Mock(side_effect=update_error),
            set_temporary_restriction=Mock(),
        )
        bot = SimpleNamespace(
            get_chat_member=Mock(side_effect=RuntimeError("Telegram unavailable")),
            send_message=Mock(),
        )
        module = SimpleNamespace(
            state_store=state,
            _profile=lambda _user: {},
            _get_record=Mock(side_effect=record_error) if record_error else Mock(return_value={}),
            _subscription_config=Mock(return_value=config),
            _send_permanent_ban=Mock(),
            _subscription_keyboard=Mock(return_value="keyboard"),
            _is_member=Mock(),
            SUBSCRIPTION_NOTICE="subscribe",
            bot=bot,
        )
        return module

    def test_moderation_write_failure_does_not_hide_subscription_prompt(self):
        module = self.make_subscription_module(update_error=OSError("moderation storage unavailable"))
        runtime_fixes._install_subscription_prompt_fallback(module)
        self.assertFalse(module._check_access(SimpleNamespace(id=42), 42))
        module.bot.send_message.assert_called_once_with(42, "subscribe", reply_markup="keyboard")

    def test_moderation_read_failure_does_not_hide_subscription_prompt(self):
        module = self.make_subscription_module(record_error=OSError("moderation read failed"))
        runtime_fixes._install_subscription_prompt_fallback(module)
        self.assertFalse(module._check_access(SimpleNamespace(id=43), 43))
        module.bot.send_message.assert_called_once_with(43, "subscribe", reply_markup="keyboard")


if __name__ == "__main__":
    unittest.main()
