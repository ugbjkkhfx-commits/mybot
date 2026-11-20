import unittest
from types import SimpleNamespace
from unittest.mock import Mock

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
        self.assertEqual(
            runtime_fixes._notice_category("BACKUP_MASTER_KEY must be configured with 32 bytes"),
            "backup-master-key",
        )
        self.assertEqual(runtime_fixes._notice_category("Backup upload failed: first"), "backup-general")


if __name__ == "__main__":
    unittest.main()
