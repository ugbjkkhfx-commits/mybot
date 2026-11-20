from __future__ import annotations

"""Encrypted Telegram backups with a durable local retry queue.

Set TELEGRAM_BOT_TOKEN, BACKUP_MASTER_KEY (a random secret of at least 32 bytes),
BACKUP_BOT_CODE (defaults to A), and BACKUP_CHANNEL_ID (defaults to the configured
backup channel) in the hosting environment. No credentials are written to archives.
"""

import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyzipper
import requests

LOG = logging.getLogger(__name__)
OWNER_ID = 1520960859
DEFAULT_CHANNEL_ID = -1001616456358
_RETRY_SECONDS = 60
_LOCK = threading.RLock()
_STARTED = False


def _bot_code() -> str:
    code = os.environ.get("BACKUP_BOT_CODE", "A")
    if not re.fullmatch(r"[A-Za-z0-9]{1,12}", code):
        raise ValueError("BACKUP_BOT_CODE must be 1-12 alphanumeric characters")
    return code


def _master_key() -> bytes:
    value = os.environ.get("BACKUP_MASTER_KEY", "")
    if len(value.encode("utf-8")) < 32:
        raise RuntimeError("BACKUP_MASTER_KEY must be configured with at least 32 bytes of random secret")
    return value.encode("utf-8")


def _file_password(user_id: int, master_key: bytes) -> bytes:
    """Mix the mandated user-specific password with the hosting-only secret."""
    user_password = f"*{int(user_id)}#".encode("utf-8")
    return hmac.new(master_key, b"telegram-user-backup\0" + user_password, hashlib.sha256).hexdigest().encode("ascii")


def _settings_password(master_key: bytes) -> bytes:
    return hmac.new(master_key, b"telegram-bot-settings-backup", hashlib.sha256).hexdigest().encode("ascii")


def create_encrypted_archive(payload: dict[str, Any], password: bytes) -> bytes:
    """Return a ZIP using WinZip AES-256 (not legacy ZipCrypto)."""
    output = io.BytesIO()
    with pyzipper.AESZipFile(output, "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as archive:
        archive.setpassword(password)
        archive.setencryption(pyzipper.WZ_AES, nbits=256)
        archive.writestr("data.json", json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
    return output.getvalue()


def read_encrypted_archive(data: bytes, password: bytes) -> dict[str, Any]:
    with pyzipper.AESZipFile(io.BytesIO(data), "r") as archive:
        archive.setpassword(password)
        raw = archive.read("data.json")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Backup payload must be a JSON object")
    return value


def _read_json(data_dir: Path, filename: str, default):
    try:
        return json.loads((data_dir / filename).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _notify_owner(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        LOG.error("Backup configuration: %s (owner notification unavailable: missing bot token)", text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": OWNER_ID, "text": text}, timeout=15)
    except requests.RequestException:
        LOG.exception("Unable to notify owner about backup configuration")


def verify_channel() -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False, "TELEGRAM_BOT_TOKEN is not configured"
    try:
        response = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        response.raise_for_status()
        me = response.json()
        if not me.get("ok"):
            return False, "Telegram getMe failed"
        channel_id = int(os.environ.get("BACKUP_CHANNEL_ID", DEFAULT_CHANNEL_ID))
        member_response = requests.get(
            f"https://api.telegram.org/bot{token}/getChatMember",
            params={"chat_id": channel_id, "user_id": me["result"]["id"]}, timeout=15,
        )
        member_response.raise_for_status()
        member = member_response.json()
        if not member.get("ok"):
            return False, "Telegram could not verify the bot's channel membership"
        result = member["result"]
        if result.get("status") not in ("administrator", "creator"):
            return False, "The bot must be an administrator in the backup channel"
        if result.get("status") != "creator" and result.get("can_post_messages") is False:
            return False, "The bot administrator lacks permission to post messages/files"
        return True, "Backup channel permissions verified"
    except (requests.RequestException, ValueError, KeyError) as exc:
        return False, f"Unable to verify backup channel permissions: {exc}"


def _snapshot_for_user(data_dir: Path, user_id: int) -> dict[str, Any]:
    moderation = _read_json(data_dir, "moderation.json", {})
    users = _read_json(data_dir, "users.json", [])
    blocked = _read_json(data_dir, "blocked.json", [])
    first_seen = _read_json(data_dir, "backup_first_seen.json", {})
    key = str(int(user_id))
    record = (moderation.get("users", {}) or {}).get(key, {})
    appeals = [appeal for appeal in moderation.get("appeals", []) if int(appeal.get("user_id", -1)) == int(user_id)]
    return {
        "format": 1,
        "user": {"user_id": int(user_id), "display_name": record.get("display_name", ""),
                 "username": record.get("username", ""), "first_seen": first_seen.get(key)},
        "moderation": record,
        "appeals": appeals,
        "is_registered": int(user_id) in users,
        "is_blocked": int(user_id) in blocked,
    }


def _snapshot_settings(data_dir: Path) -> dict[str, Any]:
    return {"format": 1, "bot_code": _bot_code(),
            "subscription": _read_json(data_dir, "subscription.json", {})}


def _queue_archive(path: Path, archive: bytes) -> None:
    _atomic_write(path, archive)


def _upload_document(path: Path, *, channel_id: int | None = None) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    target = int(channel_id if channel_id is not None else os.environ.get("BACKUP_CHANNEL_ID", DEFAULT_CHANNEL_ID))
    try:
        with path.open("rb") as handle:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": target}, files={"document": (path.name, handle, "application/zip")}, timeout=90,
            )
        response.raise_for_status()
        result = response.json()
        # Telegram's explicit ok/message_id response is the only success signal.
        return bool(result.get("ok") and result.get("result", {}).get("message_id"))
    except (requests.RequestException, ValueError, OSError):
        LOG.exception("Backup upload failed for %s", path.name)
        return False


def _make_user_backup(data_dir: Path, user_id: int) -> None:
    master = _master_key()
    code = _bot_code()
    current_name = f"{code}_{int(user_id)}.zip"
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    spool = data_dir / "telegram_backup_outbox"
    current_path = spool / current_name
    archived_path = spool / f"{code}_{int(user_id)}_{now}.zip"
    payload = _snapshot_for_user(data_dir, user_id)
    encrypted = create_encrypted_archive(payload, _file_password(user_id, master))
    _queue_archive(current_path, encrypted)
    _queue_archive(archived_path, encrypted)
    # Keep both the latest snapshot and dated history locally. Send dated copy first;
    # current file only becomes remotely current after Telegram confirms receipt.
    if _upload_document(archived_path):
        if not _upload_document(current_path):
            LOG.warning("Current user backup upload awaits retry: %s", current_name)
    else:
        LOG.warning("Dated user backup remains in local outbox: %s", archived_path)


def _make_settings_backup(data_dir: Path) -> None:
    master = _master_key()
    code = _bot_code()
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    spool = data_dir / "telegram_backup_outbox"
    current = spool / f"{code}_settings.zip"
    dated = spool / f"{code}_settings_{now}.zip"
    encrypted = create_encrypted_archive(_snapshot_settings(data_dir), _settings_password(master))
    _queue_archive(current, encrypted)
    _queue_archive(dated, encrypted)
    if _upload_document(dated):
        _upload_document(current)


def on_state_saved(filename: str, data_dir: Path) -> None:
    """Create/update encrypted snapshots after durable local state writes."""
    if filename not in ("users.json", "blocked.json", "moderation.json", "subscription.json"):
        return
    data_dir = Path(data_dir)
    try:
        with _LOCK:
            if filename == "subscription.json":
                _make_settings_backup(data_dir)
                return
            if filename == "moderation.json":
                user_ids = {int(key) for key in (_read_json(data_dir, "moderation.json", {}).get("users", {}) or {}) if str(key).isdigit()}
                user_ids.update(int(a["user_id"]) for a in _read_json(data_dir, "moderation.json", {}).get("appeals", []) if str(a.get("user_id", "")).isdigit())
            else:
                user_ids = {int(item) for item in _read_json(data_dir, "users.json", []) if str(item).isdigit()}
            if filename == "blocked.json":
                user_ids.update(int(item) for item in _read_json(data_dir, "blocked.json", []) if str(item).isdigit())
            if user_ids:
                first_seen_path = data_dir / "backup_first_seen.json"
                first_seen = _read_json(data_dir, "backup_first_seen.json", {})
                now = datetime.now(timezone.utc).isoformat()
                for user_id in user_ids:
                    first_seen.setdefault(str(user_id), now)
                _atomic_write(first_seen_path, json.dumps(first_seen, ensure_ascii=False, indent=2).encode("utf-8"))
                for user_id in sorted(user_ids):
                    _make_user_backup(data_dir, user_id)
    except Exception as exc:
        LOG.exception("Local state saved but Telegram backup could not be prepared")
        _notify_owner(f"⚠️ فشل إعداد النسخ الاحتياطي على Telegram؛ البيانات المحلية محفوظة وسيتم إعادة المحاولة. السبب: {exc}")


def retry_pending(data_dir: Path) -> int:
    """Retry durable outbox documents and return the number confirmed by Telegram."""
    sent = 0
    spool = Path(data_dir) / "telegram_backup_outbox"
    if not spool.exists():
        return 0
    for path in sorted(spool.glob("*.zip")):
        if _upload_document(path):
            sent += 1
            # Keep current archives, and retain dated archives locally as history.
            if re.fullmatch(r"[A-Za-z0-9]{1,12}_[0-9]+\.zip|[A-Za-z0-9]{1,12}_settings\.zip", path.name):
                path.unlink(missing_ok=True)
    return sent


def restore_user_archive(data: bytes, user_id: int, *, requester_id: int, data_dir: Path) -> bool:
    """Validate an owner-authorized archive and restore only absent local records.

    Existing local user records, registration/block status, and appeals are never
    replaced. The function refuses to restore settings or another user's archive.
    """
    if int(requester_id) != OWNER_ID:
        raise PermissionError("Only the configured owner may restore backups")
    payload = read_encrypted_archive(data, _file_password(int(user_id), _master_key()))
    profile = payload.get("user")
    record = payload.get("moderation")
    if not isinstance(profile, dict) or int(profile.get("user_id", -1)) != int(user_id) or not isinstance(record, dict):
        raise ValueError("Backup integrity/schema validation failed")
    target = Path(data_dir)
    moderation_path = target / "moderation.json"
    moderation = _read_json(target, "moderation.json", {"users": {}, "appeals": []})
    moderation.setdefault("users", {})
    moderation.setdefault("appeals", [])
    key = str(int(user_id))
    changed = False
    if key not in moderation["users"]:
        moderation["users"][key] = record
        changed = True
    known_appeals = {str(item.get("id")) for item in moderation["appeals"]}
    for appeal in payload.get("appeals", []):
        if not isinstance(appeal, dict) or int(appeal.get("user_id", -1)) != int(user_id):
            raise ValueError("Backup contains an invalid appeal record")
        if str(appeal.get("id")) not in known_appeals:
            moderation["appeals"].append(appeal)
            known_appeals.add(str(appeal.get("id")))
            changed = True
    if changed:
        # Preserve local state before the explicit, validated merge.
        if moderation_path.exists():
            shutil.copy2(moderation_path, moderation_path.with_name(f"{moderation_path.name}.pre_restore.{int(time.time())}"))
        _atomic_write(moderation_path, json.dumps(moderation, ensure_ascii=False, indent=2).encode("utf-8"))
    users_path = target / "users.json"
    users = _read_json(target, "users.json", [])
    if int(user_id) not in users:
        users.append(int(user_id))
        _atomic_write(users_path, json.dumps(sorted(set(users)), ensure_ascii=False, indent=2).encode("utf-8"))
    return changed


def start_background_retry(data_dir: Path) -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    def worker():
        while True:
            time.sleep(_RETRY_SECONDS)
            try:
                retry_pending(data_dir)
            except Exception:
                LOG.exception("Background backup retry failed")
    threading.Thread(target=worker, name="telegram-backup-retry", daemon=True).start()


def check_configuration_and_notify_owner() -> bool:
    try:
        _bot_code()
        _master_key()
    except Exception as exc:
        _notify_owner(f"⚠️ النسخ الاحتياطي غير مهيأ: {exc}")
        return False
    valid, reason = verify_channel()
    if not valid:
        _notify_owner(f"⚠️ تعذر استخدام قناة النسخ الاحتياطي {os.environ.get('BACKUP_CHANNEL_ID', DEFAULT_CHANNEL_ID)}: {reason}")
    return valid
