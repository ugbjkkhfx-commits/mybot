import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import state_store


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(state_store, "_DATA_DIR", Path(self.temp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_subscription_survives_reload(self):
        settings = {"chat_id": -100123, "title": "Test", "enabled": True, "verified": True}
        state_store.save_subscription(settings)
        self.assertEqual(state_store.get_subscription(), settings)

    def test_departure_is_counted_once_and_ban_persists(self):
        for count in range(1, 6):
            record, fresh = state_store.record_departure(42, f"{count}:member>left", display_name="Name", username="old")
            self.assertTrue(fresh)
            self.assertEqual(record["departure_count"], count)
        duplicate, fresh = state_store.record_departure(42, "5:member>left", display_name="Renamed", username="new")
        self.assertFalse(fresh)
        self.assertTrue(duplicate["permanently_banned"])
        self.assertEqual(state_store.get_user_record(42)["username"], "old")

    def test_return_and_manual_unban_preserve_counter(self):
        state_store.record_departure(77, "event", display_name="A")
        state_store.set_temporary_restriction(77, False)
        self.assertFalse(state_store.get_user_record(77)["temporarily_restricted"])
        state_store.lift_permanent_ban(77)
        self.assertEqual(state_store.get_user_record(77)["departure_count"], 1)

    def test_atomic_json_round_trip_and_corrupt_backup(self):
        state_store.save_subscription({"enabled": False})
        self.assertTrue((Path(self.temp.name) / "subscription.json").exists())
        (Path(self.temp.name) / "moderation.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(state_store.get_moderation(), {"users": {}})
        self.assertTrue((Path(self.temp.name) / "moderation.json.corrupt").exists())


if __name__ == "__main__":
    unittest.main()
