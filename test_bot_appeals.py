from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import bot


class HandleTextAppealTests(unittest.TestCase):
    def setUp(self):
        self.old_waiting = set(bot.appeal_states)
        bot.appeal_states.clear()
        self.message = Mock()
        self.message.from_user.id = 777
        self.message.from_user.first_name = "Test"
        self.message.from_user.username = "test_user"
        self.message.chat.type = "private"
        self.message.chat.id = 777
        self.message.text = "   \n "

    def tearDown(self):
        bot.appeal_states.clear()
        bot.appeal_states.update(self.old_waiting)

    def test_blank_text_keeps_appeal_waiting_and_does_not_save(self):
        bot.appeal_states.add(777)
        with patch.object(bot, "_handle_admin_text", return_value=False), \
             patch.object(bot, "_check_access") as check_access, \
             patch.object(bot.bot, "send_message") as send_message, \
             patch.object(bot.state_store, "submit_appeal") as submit:
            bot.handle_text(self.message)
        self.assertIn(777, bot.appeal_states)
        submit.assert_not_called()
        check_access.assert_not_called()
        send_message.assert_called_once()
        self.assertIn("فارغ", send_message.call_args.args[1])

    def test_valid_text_is_saved_then_waiting_state_is_cleared(self):
        bot.appeal_states.add(777)
        self.message.text = "Please review"
        with patch.object(bot, "_handle_admin_text", return_value=False), \
             patch.object(bot, "_check_access") as check_access, \
             patch.object(bot.bot, "send_message"), \
             patch.object(bot, "notify_admin"), \
             patch.object(bot.state_store, "submit_appeal", return_value=({"id": 1, "display_name": "Test"}, True)) as submit:
            bot.handle_text(self.message)
        self.assertNotIn(777, bot.appeal_states)
        submit.assert_called_once_with(777, "Test", "test_user", "Please review")
        check_access.assert_not_called()


if __name__ == "__main__":
    unittest.main()
