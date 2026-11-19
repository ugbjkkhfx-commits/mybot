import tempfile
import unittest
from pathlib import Path

import state_store


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_data_dir = state_store.DATA_DIR
        state_store.DATA_DIR = Path(self.tempdir.name)

    def tearDown(self):
        state_store.DATA_DIR = self.old_data_dir
        self.tempdir.cleanup()

    def test_departure_is_counted_once_and_persists(self):
        first, counted = state_store.record_departure(42, "event-1", 5, username="old_name")
        duplicate, duplicate_counted = state_store.record_departure(42, "event-1", 5, username="new_name")
        self.assertTrue(counted)
        self.assertFalse(duplicate_counted)
        self.assertEqual(1, first["departure_count"])
        self.assertEqual(1, duplicate["departure_count"])
        self.assertEqual("old_name", duplicate["username"])
        self.assertEqual(1, state_store.get_user_record(42)["departure_count"])

    def test_fifth_departure_permanently_bans_and_lift_keeps_history(self):
        for index in range(5):
            record, counted = state_store.record_departure(84, f"event-{index}", 5)
            self.assertTrue(counted)
        self.assertEqual(5, record["departure_count"])
        self.assertTrue(record["permanently_banned"])
        lifted = state_store.lift_permanent_ban(84, 1520960859)
        self.assertFalse(lifted["permanently_banned"])
        self.assertEqual(5, lifted["departure_count"])
        self.assertEqual(1520960859, lifted["ban_lifted_by"])

    def test_subscription_is_persistent(self):
        config = {"chat_id": -100123, "enabled": True, "verified": True}
        state_store.save_subscription(config)
        self.assertEqual(config, state_store.get_subscription())


if __name__ == "__main__":
    unittest.main()
