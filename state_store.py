from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", "."))
_LOCK = threading.RLock()
MODERATION_FILE = "moderation.json"
SUBSCRIPTION_FILE = "subscription.json"
USERS_FILE = "users.json"
BLOCKED_FILE = "blocked.json"
_UNSAFE_FILES: set[str] = set()
_LOG = logging.getLogger(__name__)


class DataIntegrityError(RuntimeError):
    """Persistent state is corrupt and no validated backup is available."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _validate(filename: str, value):
    if filename == SUBSCRIPTION_FILE:
        if not isinstance(value, dict):
            raise ValueError("subscription data must be an object")
        if "chat_id" in value and (isinstance(value["chat_id"], bool) or not isinstance(value["chat_id"], int)):
            raise ValueError("subscription chat_id must be an integer")
        for field in ("enabled", "verified"):
            if field in value and not isinstance(value[field], bool):
                raise ValueError(f"subscription {field} must be a boolean")
    elif filename == MODERATION_FILE:
        if not isinstance(value, dict) or not isinstance(value.get("users", {}), dict):
            raise ValueError("moderation data must contain an object of user records")
        appeals = value.get("appeals", [])
        if not isinstance(appeals, list):
            raise ValueError("appeals must be a list")
        for appeal in appeals:
            if not isinstance(appeal, dict):
                raise ValueError("appeal entries must be objects")
            if isinstance(appeal.get("user_id"), bool) or not isinstance(appeal.get("user_id"), int):
                raise ValueError("appeal user_id must be an integer")
            if not isinstance(appeal.get("text"), str) or not appeal["text"].strip():
                raise ValueError("appeal text must be non-empty")
            if appeal.get("status") not in ("pending", "processed"):
                raise ValueError("appeal status must be pending or processed")
            if not isinstance(appeal.get("created_at"), str):
                raise ValueError("appeal created_at must be a string")
        for user_key, record in value.get("users", {}).items():
            if not str(user_key).isdigit() or not isinstance(record, dict):
                raise ValueError(f"invalid moderation record for user {user_key!r}")
            count = record.get("departure_count", 0)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("departure_count must be a non-negative integer")
            for field in ("permanently_banned", "temporarily_restricted"):
                if field in record and not isinstance(record[field], bool):
                    raise ValueError(f"{field} must be a boolean")
            events = record.get("departure_events", [])
            if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
                raise ValueError("departure_events must be a list of strings")
            if "user_id" in record and (isinstance(record["user_id"], bool) or int(record["user_id"]) != int(user_key)):
                raise ValueError("record user_id must match its Telegram ID key")
    elif filename in (USERS_FILE, BLOCKED_FILE):
        if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            raise ValueError(f"{filename} must be a list of integer Telegram IDs")
    return value


def _atomic_replace(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.backup.{_timestamp()}.json")


def _valid_backup_files(path: Path):
    return sorted(path.parent.glob(f"{path.name}.backup.*.json"), reverse=True)


def _recover_corrupt_file(path: Path, filename: str):
    quarantined = path.with_name(f"{path.name}.corrupt.{_timestamp()}")
    try:
        os.replace(path, quarantined)
        _LOG.error("Corrupt persistent file quarantined at %s", quarantined)
    except OSError:
        _LOG.exception("Could not preserve corrupt persistent file %s", path)
    for backup in _valid_backup_files(path):
        try:
            with backup.open("r", encoding="utf-8") as handle:
                value = _validate(filename, json.load(handle))
            _atomic_replace(path, value)
            _UNSAFE_FILES.discard(filename)
            _LOG.warning("Restored %s from validated backup %s", filename, backup)
            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            _LOG.exception("Ignoring invalid backup %s", backup)
    _UNSAFE_FILES.add(filename)
    raise DataIntegrityError(f"{filename} is corrupt; preserved at {quarantined}; no valid backup exists. Writes disabled.")


def _read_json(filename: str, default):
    path = DATA_DIR / filename
    if filename in _UNSAFE_FILES:
        raise DataIntegrityError(f"Reads/writes for {filename} are blocked after unrecoverable corruption")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return _validate(filename, json.load(handle))
    except FileNotFoundError:
        return default
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _recover_corrupt_file(path, filename)


def _write_json(filename: str, value) -> None:
    if filename in _UNSAFE_FILES:
        raise DataIntegrityError(f"Refusing to overwrite unrecovered corrupt file {filename}")
    value = _validate(filename, value)
    target = DATA_DIR / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            with target.open("r", encoding="utf-8") as handle:
                previous = _validate(filename, json.load(handle))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            _recover_corrupt_file(target, filename)
            with target.open("r", encoding="utf-8") as handle:
                previous = _validate(filename, json.load(handle))
        backup = _backup_path(target)
        fd, backup_tmp = tempfile.mkstemp(prefix=f".{backup.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(previous, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(backup_tmp, backup)
        finally:
            try:
                os.unlink(backup_tmp)
            except FileNotFoundError:
                pass
    _atomic_replace(target, value)
    _UNSAFE_FILES.discard(filename)


def get_subscription() -> dict:
    with _LOCK:
        return _read_json(SUBSCRIPTION_FILE, {})


def save_subscription(value: dict) -> None:
    with _LOCK:
        _write_json(SUBSCRIPTION_FILE, value)


def get_moderation() -> dict:
    with _LOCK:
        value = _read_json(MODERATION_FILE, {})
        value.setdefault("users", {})
        value.setdefault("appeals", [])
        value.pop("owner_reply_map", None)
        return value


def save_moderation(value: dict) -> None:
    with _LOCK:
        value = dict(value)
        value.pop("owner_reply_map", None)
        value.setdefault("appeals", [])
        _write_json(MODERATION_FILE, value)


def load_json_set(filename: str) -> set[int]:
    with _LOCK:
        return set(_read_json(filename, []))


def save_json_set(filename: str, values: set[int]) -> None:
    with _LOCK:
        _write_json(filename, sorted(int(item) for item in values))


def get_user_record(user_id: int) -> dict:
    key = str(int(user_id))
    with _LOCK:
        record = get_moderation()["users"].get(key, {})
        if not isinstance(record, dict):
            raise DataIntegrityError(f"Invalid user record for Telegram ID {key}")
        return {"user_id": int(user_id), "departure_count": 0, **record}


def update_user_record(user_id: int, **fields) -> dict:
    key = str(int(user_id))
    with _LOCK:
        moderation = get_moderation()
        record = moderation["users"].setdefault(key, {"user_id": int(user_id), "departure_count": 0})
        if not isinstance(record, dict):
            raise DataIntegrityError(f"Invalid user record for Telegram ID {key}")
        record.update(fields)
        record["user_id"] = int(user_id)
        moderation["users"][key] = record
        save_moderation(moderation)
        return dict(record)


def record_departure(user_id: int, event_key: str, max_departures: int = 5, **profile):
    key = str(int(user_id))
    with _LOCK:
        moderation = get_moderation()
        record = moderation["users"].setdefault(key, {"user_id": int(user_id), "departure_count": 0})
        if not isinstance(record, dict):
            raise DataIntegrityError(f"Invalid user record for Telegram ID {key}")
        events = record.setdefault("departure_events", [])
        if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
            raise DataIntegrityError(f"Invalid departure event history for Telegram ID {key}")
        if event_key in events:
            return dict(record), False
        events.append(event_key)
        record["departure_events"] = events[-100:]
        record.update(profile)
        record["user_id"] = int(user_id)
        record["departure_count"] = int(record.get("departure_count", 0)) + 1
        record["temporarily_restricted"] = record["departure_count"] < int(max_departures)
        if record["departure_count"] >= int(max_departures):
            record["permanently_banned"] = True
            record["ban_reason"] = "تكرار مغادرة قناة الاشتراك الإجباري 5 مرات"
        moderation["users"][key] = record
        save_moderation(moderation)
        return dict(record), True


def submit_appeal(user_id: int, display_name: str, username: str | None, text: str) -> tuple[dict, bool]:
    """Store one active appeal per Telegram ID; repeated pending submissions are rejected."""
    user_id = int(user_id)
    text = text.strip()
    if not text:
        raise ValueError("Appeal text cannot be empty")
    with _LOCK:
        data = get_moderation()
        appeals = data["appeals"]
        for appeal in appeals:
            if appeal["user_id"] == user_id and appeal["status"] == "pending":
                return dict(appeal), False
        appeal = {
            "id": max((item.get("id", 0) for item in appeals), default=0) + 1,
            "user_id": user_id,
            "display_name": display_name or "",
            "username": username or "",
            "text": text,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        appeals.append(appeal)
        data["appeals"] = appeals
        save_moderation(data)
        return dict(appeal), True


def list_appeals(status: str | None = None) -> list[dict]:
    with _LOCK:
        appeals = get_moderation()["appeals"]
        result = [dict(item) for item in appeals if status is None or item["status"] == status]
        return sorted(result, key=lambda item: (item["status"] != "pending", item["created_at"], item["id"]))


def get_appeal(appeal_id: int) -> dict | None:
    with _LOCK:
        for item in get_moderation()["appeals"]:
            if int(item["id"]) == int(appeal_id):
                return dict(item)
        return None


def resolve_appeal(appeal_id: int, admin_id: int, *, approve: bool) -> dict | None:
    with _LOCK:
        data = get_moderation()
        appeal = next((item for item in data["appeals"] if int(item["id"]) == int(appeal_id)), None)
        if appeal is None or appeal["status"] != "pending":
            return None
        appeal["status"] = "processed"
        appeal["resolution"] = "approved" if approve else "rejected"
        appeal["processed_at"] = datetime.now(timezone.utc).isoformat()
        appeal["processed_by"] = int(admin_id)
        if approve:
            record = data["users"].setdefault(str(appeal["user_id"]), {"user_id": appeal["user_id"], "departure_count": 0})
            record["permanently_banned"] = False
            record["temporarily_restricted"] = False
            record["ban_lifted_by"] = int(admin_id)
            record["appeal_approved_at"] = appeal["processed_at"]
        save_moderation(data)
        return dict(appeal)


def set_temporary_restriction(user_id: int, restricted: bool) -> dict:
    return update_user_record(user_id, temporarily_restricted=bool(restricted))


def lift_permanent_ban(user_id: int, admin_id: int) -> dict:
    return update_user_record(user_id, permanently_banned=False, temporarily_restricted=False, ban_lifted_by=int(admin_id))
