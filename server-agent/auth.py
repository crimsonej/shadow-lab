"""
auth.py — API key creation, validation, revocation, and usage tracking.

Keys are stored as a JSON file so the agent is stateless across restarts.
Format:
{
  "sk-xxxx": {
    "label": "my-app",
    "created_at": "2024-01-01T00:00:00",
    "requests": 142,
    "tokens_in": 9800,
    "tokens_out": 4200,
    "limit_rpm": 0,          # 0 = unlimited
    "enabled": true,
    "last_used": "..."
  }
}
"""
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

import config

_lock = threading.Lock()


def _load() -> Dict[str, Any]:
    config.KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not config.KEYS_FILE.exists():
        return {}
    try:
        return json.loads(config.KEYS_FILE.read_text())
    except Exception:
        return {}


def _save(data: Dict[str, Any]) -> None:
    config.KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.KEYS_FILE.write_text(json.dumps(data, indent=2))


def create_key(label: str = "", limit_rpm: int = 0) -> Dict[str, Any]:
    """Generate a new API key and persist it. Returns the full record."""
    key = "sk-" + secrets.token_hex(24)
    record = {
        "label": label or key[:12],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "requests": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "limit_rpm": limit_rpm,
        "enabled": True,
        "last_used": None,
    }
    with _lock:
        data = _load()
        data[key] = record
        _save(data)
    return {"key": key, **record}


def revoke_key(key: str) -> bool:
    """Disable a key. Returns True if found."""
    with _lock:
        data = _load()
        if key not in data:
            return False
        data[key]["enabled"] = False
        _save(data)
    return True


def delete_key(key: str) -> bool:
    """Permanently remove a key. Returns True if found."""
    with _lock:
        data = _load()
        if key not in data:
            return False
        del data[key]
        _save(data)
    return True


def list_keys() -> list:
    """Return all keys (key string + metadata, never exposed in real usage)."""
    with _lock:
        data = _load()
    return [{"key": k, **v} for k, v in data.items()]


def validate_key(key: str) -> Optional[Dict[str, Any]]:
    """
    Return key record if valid and enabled, else None.
    Updates last_used timestamp.
    """
    with _lock:
        data = _load()
        record = data.get(key)
        if not record or not record.get("enabled", False):
            return None
        record["last_used"] = datetime.now(timezone.utc).isoformat()
        data[key] = record
        _save(data)
    return record


def record_usage(key: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Increment usage counters for a key."""
    with _lock:
        data = _load()
        if key not in data:
            return
        data[key]["requests"] = data[key].get("requests", 0) + 1
        data[key]["tokens_in"] = data[key].get("tokens_in", 0) + tokens_in
        data[key]["tokens_out"] = data[key].get("tokens_out", 0) + tokens_out
        _save(data)
