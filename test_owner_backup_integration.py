import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backup_service as backups
import owner_backup_integration as integration


class OwnerBackupIntegrationTests(unittest.TestCase):
    def test_owner_button_visibility_is_scoped_to_owner(self):
        self.assertTrue(integration.owner_button_visible(1520960859))
        self.assertFalse(integration.owner_button_visible(1520960858))

    def test_per_user_archive_password_is_exact_and_distinct(self):
        self.assertEqual(b"*1520960859#", integration._password_for_user(1520960859))
        self.assertNotEqual(integration._password_for_user(1), integration._password_for_user(2))

    def test_inner_payload_requires_master_secret(self):
        with patch.dict(os.environ, {"BACKUP_MASTER_KEY": "M" * 40}):
            integration._install_master_encryption(backups)
            password = integration._password_for_user(99)
            archive = backups.create_encrypted_archive({"user_id": 99}, password)
            self.assertEqual({"user_id": 99}, backups.read_encrypted_archive(archive, password))
            with patch.dict(os.environ, {"BACKUP_MASTER_KEY": "N" * 40}):
                with self.assertRaises(Exception):
                    backups.read_encrypted_archive(archive, password)

    def test_wrong_user_password_cannot_open_an_archive(self):
        with patch.dict(os.environ, {"BACKUP_MASTER_KEY": "M" * 40}):
            integration._install_master_encryption(backups)
            archive = backups.create_encrypted_archive({"user_id": 7}, integration._password_for_user(7))
            with self.assertRaises(Exception):
                backups.read_encrypted_archive(archive, integration._password_for_user(8))

    def test_restore_cannot_replace_existing_local_record(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"BACKUP_MASTER_KEY": "M" * 40}):
            integration._install_master_encryption(backups)
            payload = {"format": 1, "user": {"user_id": 42}, "moderation": {"user_id": 42, "departure_count": 4}, "appeals": []}
            archive = backups.create_encrypted_archive(payload, integration._password_for_user(42))
            target = Path(temp_dir) / "moderation.json"
            target.write_text(json.dumps({"users": {"42": {"user_id": 42, "departure_count": 1}}, "appeals": []}), encoding="utf-8")
            with self.assertRaises(PermissionError):
                backups.restore_user_archive(archive, 42, requester_id=4, data_dir=Path(temp_dir))
            backups.restore_user_archive(archive, 42, requester_id=backups.OWNER_ID, data_dir=Path(temp_dir))
            restored = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(1, restored["users"]["42"]["departure_count"])

    def test_scheduler_accepts_user_ids_and_passes_them_through(self):
        calls = []
        schedule, process, _event = integration._make_backup_scheduler(
            lambda filename, data_dir, user_ids=None: calls.append((filename, data_dir, user_ids))
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            schedule("moderation.json", Path(temp_dir), user_ids={7})
            self.assertEqual(1, process())
        self.assertEqual([("moderation.json", Path(temp_dir), {7})], calls)

    def test_scheduler_unions_user_ids_for_coalesced_updates(self):
        calls = []
        schedule, process, _event = integration._make_backup_scheduler(
            lambda filename, data_dir, user_ids=None: calls.append(user_ids)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            schedule("moderation.json", Path(temp_dir), user_ids={7})
            schedule("moderation.json", Path(temp_dir), user_ids={8, 9})
            self.assertEqual(1, process())
        self.assertEqual([{7, 8, 9}], calls)

    def test_scheduler_preserves_legacy_full_reconciliation_request(self):
        calls = []
        schedule, process, _event = integration._make_backup_scheduler(
            lambda filename, data_dir, user_ids=None: calls.append(user_ids)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            schedule("moderation.json", Path(temp_dir))
            self.assertEqual(1, process())
        self.assertEqual([None], calls)

    def test_full_reconciliation_request_subsumes_narrower_pending_ids(self):
        calls = []
        schedule, process, _event = integration._make_backup_scheduler(
            lambda filename, data_dir, user_ids=None: calls.append(user_ids)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            schedule("moderation.json", Path(temp_dir), user_ids={7})
            schedule("moderation.json", Path(temp_dir), user_ids=None)
            schedule("moderation.json", Path(temp_dir), user_ids={8})
            process()
        self.assertEqual([None], calls)

    @patch.object(backups, "_upload_pending", return_value=False)
    def test_failed_upload_keeps_backup_in_retry_queue(self, upload_pending):
        with tempfile.TemporaryDirectory() as temp_dir:
            spool = Path(temp_dir) / "telegram_backup_outbox"
            spool.mkdir()
            queued = spool / "A_42_20261120T120000.000000Z.zip"
            queued.write_bytes(b"pending backup bytes")
            self.assertEqual(0, backups.retry_pending(Path(temp_dir)))
            upload_pending.assert_called_once_with(queued)
            self.assertTrue(queued.exists())

    @patch.object(backups, "_upload_document", return_value=True)
    def test_outbox_upload_confirms_and_retains_dated_history(self, upload):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"BACKUP_MASTER_KEY": "M" * 40, "BACKUP_BOT_CODE": "A"}):
            (Path(temp_dir) / "users.json").write_text("[42]", encoding="utf-8")
            (Path(temp_dir) / "moderation.json").write_text(json.dumps({"users": {"42": {"user_id": 42, "departure_count": 0}}, "appeals": []}), encoding="utf-8")
            integration._install_master_encryption(backups)
            backups.on_state_saved("users.json", Path(temp_dir))
            spool = Path(temp_dir) / "telegram_backup_outbox"
            self.assertTrue((spool / "A_42.zip").exists())
            self.assertTrue(list(spool.glob("A_42_*.zip")))
            self.assertTrue(upload.called)

    def test_restore_filename_is_namespaced_by_bot_code(self):
        with patch.dict(os.environ, {"BACKUP_BOT_CODE": "A"}):
            self.assertEqual("A", backups._bot_code())
        with patch.dict(os.environ, {"BACKUP_BOT_CODE": "BOTB"}):
            self.assertEqual("BOTB", backups._bot_code())


if __name__ == "__main__":
    unittest.main()
