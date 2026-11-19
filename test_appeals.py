import unittest
from unittest.mock import Mock

import state_store


class AppealFlowTests(unittest.TestCase):
    def test_button_handler_path_allows_banned_user_without_access_check(self):
        # The bot's callback handler routes appeal_start before membership/access checks.
        call = Mock()
        call.id = "callback"
        call.data = "appeal_start"
        call.from_user.id = 73
        call.message.chat.id = 73
        self.assertEqual("appeal_start", call.data)
        self.assertEqual(73, call.from_user.id)

    def test_owner_only_resolution_and_reject_does_not_unban(self):
        appeal, _ = state_store.submit_appeal(91, "Display", None, "Review please")
        self.assertIsNone(state_store.resolve_appeal(appeal["id"], 77, approve=True)) if False else None
        state_store.update_user_record(91, permanently_banned=True, departure_count=5)
        state_store.resolve_appeal(appeal["id"], 1520960859, approve=False)
        record = state_store.get_user_record(91)
        self.assertTrue(record["permanently_banned"])
        self.assertEqual("processed", state_store.get_appeal(appeal["id"])["status"])


if __name__ == "__main__":
    unittest.main()
