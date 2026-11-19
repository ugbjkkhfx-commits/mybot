from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", "."))
_LOCK = threading.RLock()
MODERATION_FILE = "moderation.json"
SUBSCRIPTION_FILE = "subscription.json"


def _read_json(filename: str, default):
    path = DATA_DIR / filename
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default


def _write_json(filename: str, value) -> None:
    target = DATA_DIR / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def get_subscription() -> dict:
    with _LOCK:
        value = _read_json(SUBSCRIPTION_FILE, {})
        return value if isinstance(value, dict) else {}


def save_subscription(value: dict) -> None:
    with _LOCK:
        _write_json(SUBSCRIPTION_FILE, value)


def get_moderation() -> dict:
    with _LOCK:
        value = _read_json(MODERATION_FILE, {})
        if not isinstance(value, dict):
            value = {}
        value.setdefault("users", {})
        value.setdefault("owner_reply_map", {})
        return value


def save_moderation(value: dict) -> None:
    with _LOCK:
        _write_json(MODERATION_FILE, value)


def get_user_record(user_id: int) -> dict:
    key = str(int(user_id))
    with _LOCK:
        moderation = get_moderation()
        record = moderation["users"].get(key, {})
        if not isinstance(record, dict):
            return {"user_id": int(user_id), "departure_count": 0}
        return {"user_id": int(user_id), "departure_count": 0, **record}


def update_user_record(user_id: int, **fields) -> dict:
    key = str(int(user_id))
    with _LOCK:
        moderation = get_moderation()
        record = moderation["users"].setdefault(key, {"user_id": int(user_id), "departure_count": 0})
        if not isinstance(record, dict):
            record = {"user_id": int(user_id), "departure_count": 0}
            moderation["users"][key] = record
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
            record = {"user_id": int(user_id), "departure_count": 0}
        events = record.setdefault("departure_events", [])
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


def set_temporary_restriction(user_id: int, restricted: bool) -> dict:
    record = update_user_record(user_id, temporarily_restricted=bool(restricted))
    return record


def lift_permanent_ban(user_id: int, admin_id: int) -> dict:
    record = update_user_record(
        user_id,
        permanently_banned=False,
        temporarily_restricted=False,
        ban_lifted_by=int(admin_id),
    )
    return record
