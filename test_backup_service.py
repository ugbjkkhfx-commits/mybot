import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backup_service as backups


class EncryptedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"BACKUP_MASTER_KEY": "x" * 48, "BACKUP_BOT_CODE": "A"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_user_archive_round_trips_with_user_password_and_master_secret(self):
        payload = {"user": {"user_id": 1520960859, "display_name": "Test"}}
        archive = backups.create_encrypted_archive(payload, backups._file_password(1520960859, backups._master_key()))
        self.assertEqual(payload, backups.read_encrypted_archive(archive, backups._file_password(1520960859, backups._master_key())))
        self.assertNotEqual(backups._file_password(1520960859, backups._master_key()), backups._file_password(22, backups._master_key()))

    def test_wrong_password_is_rejected(self):
        archive = backups.create_encrypted_archive({"private": "data"}, b"correct")
        with self.assertRaises(Exception):
            backups.read_encrypted_archive(archive, b"wrong")

    @patch.object(backups, "_upload_document", return_value=True)
    def test_user_file_is_queued_and_uploaded(self, upload):
        (self.data_dir / "users.json").write_text("[42]", encoding="utf-8")
        (self.data_dir / "moderation.json").write_text(json.dumps({"users": {"42": {"user_id": 42, "display_name": "Ada"}}, "appeals": []}), encoding="utf-8")
        backups.on_state_saved("users.json", self.data_dir)
        self.assertTrue((self.data_dir / "telegram_backup_outbox" / "A_42.zip").exists())
        self.assertTrue(any(upload.call_args.args[0].name.startswith("A_42_") for _ in [0]))

    def test_bot_codes_are_isolated(self):
        code_a = backups._bot_code()
        with patch.dict(os.environ, {"BACKUP_BOT_CODE": "BOTB"}):
            code_b = backups._bot_code()
        self.assertNotEqual(code_a, code_b)
        self.assertEqual("A", code_a)

    def test_restore_is_owner_only_and_does_not_replace_existing_record(self):
        payload = {"format": 1, "user": {"user_id": 42}, "moderation": {"user_id": 42, "departure_count": 4}, "appeals": []}
        encrypted = backups.create_encrypted_archive(payload, backups._file_password(42, backups._master_key()))
        with self.assertRaises(PermissionError):
            backups.restore_user_archive(encrypted, 42, requester_id=9, data_dir=self.data_dir)
        target = self.data_dir / "moderation.json"
        target.write_text(json.dumps({"users": {"42": {"user_id": 42, "departure_count": 1}}, "appeals": []}), encoding="utf-8")
        backups.restore_user_archive(encrypted, 42, requester_id=backups.OWNER_ID, data_dir=self.data_dir)
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(1, saved["users"]["42"]["departure_count"])


if __name__ == "__main__":
    unittest.main()
