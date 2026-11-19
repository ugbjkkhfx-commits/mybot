"""Small, crash-safe JSON persistence helpers for subscription and moderation state."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", "."))
_LOCK = threading.RLock()


def _path(name: str) -> Path:
    return _DATA_DIR / name


def _read(name: str, default: Any) -> Any:
    path = _path(name)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        # Do not silently overwrite malformed data; preserve a copy for recovery.
        try:
            if path.exists():
                backup = path.with_suffix(path.suffix + ".corrupt")
                if not backup.exists():
                    backup.write_bytes(path.read_bytes())
        except OSError:
            pass
        return default


def _write(name: str, value: Any) -> None:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def get_subscription() -> dict[str, Any]:
    with _LOCK:
        data = _read("subscription.json", {})
        return data if isinstance(data, dict) else {}


def save_subscription(data: dict[str, Any]) -> None:
    with _LOCK:
        _write("subscription.json", data)


def get_moderation() -> dict[str, Any]:
    with _LOCK:
        data = _read("moderation.json", {"users": {}})
        if not isinstance(data, dict):
            data = {"users": {}}
        if not isinstance(data.get("users"), dict):
            data["users"] = {}
        return data


def save_moderation(data: dict[str, Any]) -> None:
    with _LOCK:
        _write("moderation.json", data)


def get_user_record(user_id: int) -> dict[str, Any]:
    with _LOCK:
        users = get_moderation()["users"]
        record = users.get(str(int(user_id)), {})
        return record if isinstance(record, dict) else {}


def update_user_record(user_id: int, **changes: Any) -> dict[str, Any]:
    """Merge a user's current display details without changing their stable ID."""
    with _LOCK:
        data = get_moderation()
        key = str(int(user_id))
        record = data["users"].get(key, {})
        if not isinstance(record, dict):
            record = {}
        record.update(changes)
        record["user_id"] = int(user_id)
        data["users"][key] = record
        save_moderation(data)
        return dict(record)


def record_departure(user_id: int, event_key: str, **profile: Any) -> tuple[dict[str, Any], bool]:
    """Count a distinct observed departure once; returns (record, newly_counted)."""
    with _LOCK:
        data = get_moderation()
        key = str(int(user_id))
        record = data["users"].get(key, {})
        if not isinstance(record, dict):
            record = {}
        if record.get("last_departure_event") == event_key:
            return dict(record), False
        record.update(profile)
        record["user_id"] = int(user_id)
        record["departure_count"] = int(record.get("departure_count", 0)) + 1
        record["last_departure_event"] = event_key
        record["temporarily_restricted"] = record["departure_count"] < 5
        if record["departure_count"] >= 5:
            record["permanently_banned"] = True
            record["temporarily_restricted"] = False
            record["ban_reason"] = "تكرار مغادرة قناة الاشتراك الإجباري 5 مرات"
        else:
            record["temporarily_restricted"] = True
            record["ban_reason"] = "مغادرة قناة الاشتراك الإجباري"
        data["users"][key] = record
        save_moderation(data)
        return dict(record), True


def set_temporary_restriction(user_id: int, restricted: bool) -> dict[str, Any]:
    with _LOCK:
        data = get_moderation()
        key = str(int(user_id))
        record = data["users"].get(key, {})
        if not isinstance(record, dict):
            record = {}
        record["user_id"] = int(user_id)
        if not record.get("permanently_banned", False):
            record["temporarily_restricted"] = bool(restricted)
        data["users"][key] = record
        save_moderation(data)
        return dict(record)


def lift_permanent_ban(user_id: int) -> dict[str, Any]:
    """Lift only the permanent ban; retain the infraction counter and audit history."""
    with _LOCK:
        data = get_moderation()
        key = str(int(user_id))
        record = data["users"].get(key, {})
        if not isinstance(record, dict):
            record = {}
        record["user_id"] = int(user_id)
        record["permanently_banned"] = False
        record["temporarily_restricted"] = False
        record["ban_reason"] = None
        data["users"][key] = record
        save_moderation(data)
        return dict(record)
