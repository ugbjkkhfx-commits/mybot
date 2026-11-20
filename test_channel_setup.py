import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import channel_setup


class ChannelLinkParsingTests(unittest.TestCase):
    def test_public_url_and_at_username(self):
        self.assertEqual(
            channel_setup.parse_public_channel("https://t.me/Zoom_Software"),
            ("Zoom_Software", "https://t.me/Zoom_Software"),
        )
        self.assertEqual(
            channel_setup.parse_public_channel("@Zoom_Software"),
            ("Zoom_Software", "https://t.me/Zoom_Software"),
        )

    def test_private_invite_link_formats_are_preserved(self):
        self.assertEqual(channel_setup.normalize_invite_link("https://t.me/+AbC_123-x"), "https://t.me/+AbC_123-x")
        self.assertEqual(channel_setup.normalize_invite_link("https://t.me/joinchat/AbC_123-x"), "https://t.me/joinchat/AbC_123-x")


class ChannelSetupFlowTests(unittest.TestCase):
    def make_module(self, chat):
        module = SimpleNamespace()
        module.ADMIN_ID = 7
        module.admin_states = {7: ("channel", {})}
        module.state_store = SimpleNamespace(save_subscription=Mock())
        module._subscription_config = Mock(return_value={"enabled": True})
        module._subscription_status_text = Mock(return_value="status")
        module._subscription_admin_keyboard = Mock(return_value="keyboard")
        module.bot = SimpleNamespace(
            get_chat=Mock(return_value=chat),
            get_me=Mock(return_value=SimpleNamespace(id=50)),
            get_chat_member=Mock(return_value=SimpleNamespace(status="administrator")),
            reply_to=Mock(),
        )
        return module

    def test_public_channel_lookup_and_save(self):
        chat = SimpleNamespace(id=-100123, title="Zoom Software", type="channel")
        module = self.make_module(chat)
        channel_setup.install(module)
        message = SimpleNamespace(text="https://t.me/Zoom_Software", forward_from_chat=None, forward_origin=None)

        module._resolve_channel_setup(message)

        module.bot.get_chat.assert_any_call("@Zoom_Software")
        module.state_store.save_subscription.assert_called_once()
        saved = module.state_store.save_subscription.call_args.args[0]
        self.assertEqual(saved["chat_id"], -100123)
        self.assertEqual(saved["public_link"], "https://t.me/Zoom_Software")
        self.assertTrue(saved["enabled"])

    def test_private_channel_verification_and_save(self):
        chat = SimpleNamespace(id=-100456, title="Private channel", type="channel")
        module = self.make_module(chat)
        channel_setup.install(module)
        message = SimpleNamespace()

        module._verify_and_save_channel(
            message,
            {"chat_id": -100456, "title": "Private channel", "invite_link": "https://t.me/+secret_hash"},
        )

        module.bot.get_chat.assert_called_once_with(-100456)
        module.state_store.save_subscription.assert_called_once()
        saved = module.state_store.save_subscription.call_args.args[0]
        self.assertEqual(saved["invite_link"], "https://t.me/+secret_hash")
        self.assertIsNone(saved["public_link"])

    def test_permission_error_is_reported_without_overwriting_existing_setup(self):
        chat = SimpleNamespace(id=-100456, title="Private channel", type="channel")
        module = self.make_module(chat)
        module.bot.get_chat_member.side_effect = RuntimeError("not enough rights")
        channel_setup.install(module)

        module._verify_and_save_channel(SimpleNamespace(), {"chat_id": -100456})

        module.state_store.save_subscription.assert_not_called()
        self.assertIn("not enough rights", module.bot.reply_to.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
