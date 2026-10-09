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
            return json.loads(DB_PATH.read_text())
        except Exception:
            pass
    return {}


def _save(data: dict):
    DB_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _key(group_id: str, content_id: str) -> str:
    return f"{group_id}::{content_id}"


def is_sent(group_id: str, content_id: str) -> bool:
    with _lock:
        data = _load()
        return _key(group_id, content_id) in data


def mark_sent(group_id: str, content_id: str, meta: dict = None):
    with _lock:
        data = _load()
        data[_key(group_id, content_id)] = meta or {}
        _save(data)


def get_all_sent(group_id: str) -> set[str]:
    with _lock:
        data = _load()
        prefix = f"{group_id}::"
        return {k[len(prefix):] for k in data if k.startswith(prefix)}


def clear_group(group_id: str):
    with _lock:
        data = _load()
        keys_to_del = [k for k in data if k.startswith(f"{group_id}::")]
        for k in keys_to_del:
            del data[k]
        _save(data)
