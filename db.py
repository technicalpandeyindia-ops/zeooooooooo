"""
db.py — persistent tracker using a local JSON file.
Stores which lectures/PDFs/notes have already been sent to which group.
"""

import json
import os
from pathlib import Path
from threading import Lock

DB_PATH = Path(os.environ.get("DB_PATH", "./sent_tracker.json"))
_lock = Lock()


def _load() -> dict:
    if DB_PATH.exists():
        try:
            return json.loads(DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict):
    with _lock:
        DB_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def is_sent(item_key: str) -> bool:
    """Check if item_key (group_id:url or url) has already been sent."""
    data = _load()
    return item_key in data


def mark_sent(item_key: str):
    """Mark item_key as sent."""
    data = _load()
    data[item_key] = True
    _save(data)


def get_all_sent() -> list[str]:
    """Retrieve list of all sent item keys."""
    return list(_load().keys())


def clear_group(group_id: str):
    """Clear sent tracking history for a specific group."""
    data = _load()
    filtered = {k: v for k, v in data.items() if not k.startswith(f"{group_id}:")}
    _save(filtered)
