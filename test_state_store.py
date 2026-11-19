import json
import tempfile
import unittest
from pathlib import Path

import state_store


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_data_dir = state_store.DATA_DIR
        self.old_unsafe = set(state_store._UNSAFE_FILES)
        state_store.DATA_DIR = Path(self.tempdir.name)
        state_store._UNSAFE_FILES.clear()

    def tearDown(self):
        state_store.DATA_DIR = self.old_data_dir
        state_store._UNSAFE_FILES.clear()
        state_store._UNSAFE_FILES.update(self.old_unsafe)
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

    def test_subscription_is_persistent_and_previous_value_is_backed_up(self):
        original = {"chat_id": -100123, "enabled": True, "verified": True}
        state_store.save_subscription(original)
        state_store.save_subscription({**original, "enabled": False})
        backups = list(Path(self.tempdir.name).glob("subscription.json.backup.*.json"))
        self.assertTrue(backups)
        self.assertEqual(original, json.loads(backups[0].read_text(encoding="utf-8")))

    def test_corrupt_moderation_is_quarantined_and_restored_from_good_backup(self):
        state_store.record_departure(84, "departure-1", 5)
        expected = state_store.get_user_record(84)
        path = Path(self.tempdir.name) / state_store.MODERATION_FILE
        path.write_text("{broken", encoding="utf-8")
        restored = state_store.get_user_record(84)
        self.assertEqual(expected["departure_count"], restored["departure_count"])
        self.assertEqual(expected["departure_events"], restored["departure_events"])
        self.assertTrue(list(Path(self.tempdir.name).glob("moderation.json.corrupt.*")))

    def test_corrupt_file_without_backup_raises_and_is_not_overwritten(self):
        path = Path(self.tempdir.name) / state_store.MODERATION_FILE
        path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.update_user_record(9, username="must-not-write")
        self.assertTrue(list(Path(self.tempdir.name).glob("moderation.json.corrupt.*")))
        self.assertFalse(path.exists())
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.save_moderation({"users": {}})

    def test_corrupt_subscription_recovers_last_valid_state(self):
        original = {"chat_id": -100123, "enabled": True, "verified": True}
        state_store.save_subscription(original)
        state_store.save_subscription({**original, "enabled": False})
        path = Path(self.tempdir.name) / state_store.SUBSCRIPTION_FILE
        path.write_text("[]", encoding="utf-8")
        self.assertEqual(original, state_store.get_subscription())
        self.assertTrue(list(Path(self.tempdir.name).glob("subscription.json.corrupt.*")))

    def test_invalid_moderation_schema_is_detected(self):
        path = Path(self.tempdir.name) / state_store.MODERATION_FILE
        path.write_text(json.dumps({"users": {"84": {"departure_count": "five"}}}), encoding="utf-8")
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.get_moderation()

    def test_user_id_sets_are_persistent(self):
        state_store.save_json_set(state_store.USERS_FILE, {4, 9})
        self.assertEqual({4, 9}, state_store.load_json_set(state_store.USERS_FILE))


if __name__ == "__main__":
    unittest.main()
