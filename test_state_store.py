import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import state_store


class StateStoreIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_dir = state_store.DATA_DIR
        self.old_unsafe = set(state_store._UNSAFE_FILES)
        state_store.DATA_DIR = Path(self.tempdir.name)
        state_store._UNSAFE_FILES.clear()

    def tearDown(self):
        state_store.DATA_DIR = self.old_dir
        state_store._UNSAFE_FILES.clear()
        state_store._UNSAFE_FILES.update(self.old_unsafe)
        self.tempdir.cleanup()

    def test_missing_file_can_be_created_and_valid_file_is_preserved(self):
        state_store.save_subscription({"chat_id": -100123, "enabled": True, "verified": True})
        self.assertTrue((Path(self.tempdir.name) / state_store.SUBSCRIPTION_FILE).exists())
        self.assertEqual(-100123, state_store.get_subscription()["chat_id"])

    def test_corruption_quarantines_original_and_persists_lock_across_reload(self):
        path = Path(self.tempdir.name) / state_store.MODERATION_FILE
        path.write_text("{corrupt", encoding="utf-8")
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.get_moderation()
        quarantined = list(Path(self.tempdir.name).glob("moderation.json.corrupt.*"))
        marker = Path(self.tempdir.name) / "moderation.json.corrupt.lock"
        self.assertEqual(1, len(quarantined))
        self.assertTrue(marker.exists())
        self.assertEqual("{corrupt", quarantined[0].read_text(encoding="utf-8"))

        # Simulate a process restart: in-memory unsafe state is lost, the marker remains.
        state_store._UNSAFE_FILES.clear()
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.save_moderation({"users": {}, "appeals": []})
        self.assertTrue(path.exists())
        self.assertEqual("{corrupt", path.read_text(encoding="utf-8"))

    def test_restart_with_marker_and_missing_original_does_not_create_empty_file(self):
        marker = Path(self.tempdir.name) / "users.json.corrupt.lock"
        marker.write_text(json.dumps({"filename": "users.json"}), encoding="utf-8")
        state_store._UNSAFE_FILES.clear()
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.load_json_set(state_store.USERS_FILE)
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.save_json_set(state_store.USERS_FILE, {123})
        self.assertFalse((Path(self.tempdir.name) / "users.json").exists())

    def test_corrupt_data_is_not_overwritten_on_write_attempt(self):
        path = Path(self.tempdir.name) / state_store.SUBSCRIPTION_FILE
        path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(state_store.DataIntegrityError):
            state_store.save_subscription({"chat_id": -99, "enabled": True, "verified": True})
        self.assertEqual("not-json", path.read_text(encoding="utf-8"))
        self.assertTrue(list(Path(self.tempdir.name).glob("subscription.json.corrupt.*")))

    def test_moderation_write_keeps_previous_valid_backup(self):
        state_store.update_user_record(12, departure_count=2, permanently_banned=False)
        state_store.update_user_record(12, departure_count=3)
        backups = list(Path(self.tempdir.name).glob("moderation.json.backup.*.json"))
        self.assertTrue(backups)
        self.assertEqual(2, json.loads(backups[0].read_text(encoding="utf-8"))["users"]["12"]["departure_count"])


class BlankAppealTests(unittest.TestCase):
    def test_blank_submission_keeps_waiting_state_and_does_not_persist(self):
        state_store.DATA_DIR = Path(tempfile.mkdtemp())
        waiting = {45}
        fake_bot = Mock()
        bot_globals = {"appeal_states": waiting, "bot": fake_bot}
        caller = {"message": Mock()}
        caller["message"].chat.id = 45
        caller_frame = Mock()
        caller_frame.f_globals = bot_globals
        caller_frame.f_locals = caller
        submit_frame = Mock()
        submit_frame.f_back = caller_frame
        with patch("state_store.inspect.currentframe", return_value=submit_frame):
            with self.assertRaises(ValueError):
                state_store.submit_appeal(45, "Name", None, "   \n  ")
        self.assertIn(45, waiting)
        self.assertEqual([], state_store.list_appeals())
        fake_bot.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
