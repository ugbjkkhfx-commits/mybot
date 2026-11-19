import unittest
from unittest.mock import Mock

from owner_contact import handle_unconfigured_contact, make_contact_button, owner_contact_url


class OwnerContactTests(unittest.TestCase):
    def test_configured_username_builds_direct_telegram_profile_link(self):
        factory = Mock(return_value="button")
        result = make_contact_button(factory, "@owner_name")
        self.assertEqual("button", result)
        factory.assert_called_once_with("📩 التواصل مع المالك", url="https://t.me/owner_name")
        self.assertEqual("https://t.me/owner_name", owner_contact_url("owner_name"))

    def test_missing_username_does_not_invent_a_link(self):
        factory = Mock(return_value="button")
        make_contact_button(factory, "")
        factory.assert_called_once_with("📩 التواصل مع المالك", callback_data="contact_owner")
        self.assertIsNone(owner_contact_url(None))

    def test_unconfigured_callback_explains_direct_contact_setup(self):
        bot = Mock()
        handle_unconfigured_contact(bot, 123)
        text = bot.send_message.call_args.args[1]
        self.assertIn("محادثته الخاصة على Telegram", text)
        self.assertIn("OWNER_TELEGRAM_USERNAME", text)


if __name__ == "__main__":
    unittest.main()
