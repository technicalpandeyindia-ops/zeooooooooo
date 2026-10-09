"""
db.py — Multi-Key Deduplication State Tracker.
Stores both URL-based and Title/Subject-based keys to ensure already-sent
videos and PDFs are NEVER sent twice, allowing seamless resumption.
"""

import json
import os
import re
from pathlib import Path
from threading import Lock

DB_PATH = Path(os.environ.get("DB_PATH", "./sent_tracker.json"))
_lock = Lock()


def _normalize(text: str) -> str:
    """Normalizes string for robust title/subject matching."""
    return re.sub(r"[^a-zA-Z0-9]", "", text or "").lower()


def _load() -> dict:
    if DB_PATH.exists():
        try:
            return json.loads(DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict):
    with _lock:
        try:
            DB_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def is_already_sent(group_id: str, subject: str, title: str, url: str) -> bool:
    """
    Checks if an item was already sent using both URL hash and Subject+Title key.
    If either was recorded, returns True.
    """
    data = _load()

    # 1. URL key
    url_key = f"{group_id}:{url}".strip()
    if url_key in data:
        return True

    # 2. Subject + Title key
    norm_subj = _normalize(subject)
    norm_title = _normalize(title)
    if norm_subj and norm_title:
        title_key = f"{group_id}:{norm_subj}:{norm_title}"
        if title_key in data:
            return True

    return False


def mark_as_sent(group_id: str, subject: str, title: str, url: str):
    """
    Marks an item as sent under both its URL key and Subject+Title key.
    """
    data = _load()

    # Record URL key
    url_key = f"{group_id}:{url}".strip()
    data[url_key] = True

    # Record Subject + Title key
    norm_subj = _normalize(subject)
    norm_title = _normalize(title)
    if norm_subj and norm_title:
        title_key = f"{group_id}:{norm_subj}:{norm_title}"
        data[title_key] = True

    _save(data)


def get_all_sent_count() -> int:
    """Returns total count of tracked items."""
    return len(_load())


def clear_group(group_id: str):
    """Clears tracking history for a specific group ID."""
    data = _load()
    prefix = f"{group_id}:"
    filtered = {k: v for k, v in data.items() if not k.startswith(prefix)}
    _save(filtered)
