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
        self.assertFalse(duplicate_counted)
        self.assertEqual("old_name", duplicate["username"])
        self.assertEqual(1, state_store.get_user_record(42)["departure_count"])

    def test_fifth_departure_and_appeal_approval_retain_history(self):
        for index in range(5):
            record, counted = state_store.record_departure(84, f"event-{index}", 5)
            self.assertTrue(counted)
        appeal, created = state_store.submit_appeal(84, "User", "name", "Please review")
        self.assertTrue(created)
        self.assertEqual(5, state_store.get_user_record(84)["departure_count"])
        resolved = state_store.resolve_appeal(appeal["id"], 1520960859, approve=True)
        self.assertEqual("processed", resolved["status"])
        self.assertFalse(state_store.get_user_record(84)["permanently_banned"])
        self.assertEqual(5, state_store.get_user_record(84)["departure_count"])
        self.assertEqual(5, len(state_store.get_user_record(84)["departure_events"]))

    def test_appeal_persists_and_pending_duplicate_is_blocked(self):
        appeal, created = state_store.submit_appeal(7, "Name", None, "Review my ban")
        duplicate, duplicate_created = state_store.submit_appeal(7, "Name", None, "Second message")
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(appeal["id"], duplicate["id"])
        self.assertEqual("Review my ban", state_store.list_appeals("pending")[0]["text"])

    def test_reject_appeal_keeps_ban(self):
        state_store.update_user_record(13, permanently_banned=True, departure_count=5)
        appeal, _ = state_store.submit_appeal(13, "Name", "user", "Please")
        state_store.resolve_appeal(appeal["id"], 1520960859, approve=False)
        self.assertTrue(state_store.get_user_record(13)["permanently_banned"])
        self.assertEqual("rejected", state_store.get_appeal(appeal["id"])["resolution"])

    def test_subscription_and_users_persist(self):
        state_store.save_subscription({"chat_id": -100123, "enabled": True, "verified": True})
        self.assertEqual(-100123, state_store.get_subscription()["chat_id"])
        state_store.save_json_set(state_store.USERS_FILE, {4, 9})
        self.assertEqual({4, 9}, state_store.load_json_set(state_store.USERS_FILE))

    def test_corrupt_moderation_quarantines_and_restores_backup(self):
        state_store.record_departure(84, "departure-1", 5)
        expected = state_store.get_user_record(84)
        path = Path(self.tempdir.name) / state_store.MODERATION_FILE
        path.write_text("{broken", encoding="utf-8")
        restored = state_store.get_user_record(84)
        self.assertEqual(expected["departure_count"], restored["departure_count"])
        self.assertTrue(list(Path(self.tempdir.name).glob("moderation.json.corrupt.*")))

    def test_corrupt_file_without_backup_is_preserved_and_write_blocked(self):
        path = Path(self.tempdir.name) / state_store.MODERATION_FILE
        path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.update_user_record(9, username="must-not-write")
        self.assertTrue(list(Path(self.tempdir.name).glob("moderation.json.corrupt.*")))
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.save_moderation({"users": {}})

    def test_corrupt_subscription_recovers_last_valid_backup(self):
        original = {"chat_id": -100123, "enabled": True, "verified": True}
        state_store.save_subscription(original)
        state_store.save_subscription({**original, "enabled": False})
        path = Path(self.tempdir.name) / state_store.SUBSCRIPTION_FILE
        path.write_text("[]", encoding="utf-8")
        self.assertEqual(original, state_store.get_subscription())
        self.assertTrue(list(Path(self.tempdir.name).glob("subscription.json.corrupt.*")))


if __name__ == "__main__":
    unittest.main()
