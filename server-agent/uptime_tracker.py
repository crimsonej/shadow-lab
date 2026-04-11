"""
uptime_tracker.py — Lightweight uptime and monthly runtime persistence.

Stores boot/shutdown events and accumulated runtime in a JSON file.
No database dependency — uses a single flat file at DATA_DIR/uptime.json.
"""
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

import config

log = logging.getLogger(__name__)

_boot_time: float = 0.0


def _data_path() -> Path:
    return config.DATA_DIR / "uptime.json"


def _load() -> Dict[str, Any]:
    path = _data_path()
    if not path.exists():
        return {"sessions": [], "monthly": {}}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"sessions": [], "monthly": {}}


def _save(data: Dict[str, Any]) -> None:
    path = _data_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _month_key() -> str:
    """Current month key, e.g. '2026-04'."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def record_boot() -> None:
    """Called once on agent startup. Records boot timestamp."""
    global _boot_time
    _boot_time = time.time()

    data = _load()
    data["last_boot"] = datetime.now(timezone.utc).isoformat()
    data["sessions"].append({
        "boot": data["last_boot"],
        "shutdown": None,
        "duration_seconds": None,
    })
    # Keep only last 100 sessions to bound file size
    if len(data["sessions"]) > 100:
        data["sessions"] = data["sessions"][-100:]
    _save(data)
    log.info("Uptime tracker: boot recorded")


def record_shutdown() -> None:
    """Called on agent shutdown. Closes the current session and accumulates monthly runtime."""
    global _boot_time
    if _boot_time == 0:
        return

    duration = time.time() - _boot_time
    now_iso = datetime.now(timezone.utc).isoformat()
    month = _month_key()

    data = _load()
    # Close the last open session
    if data["sessions"] and data["sessions"][-1]["shutdown"] is None:
        data["sessions"][-1]["shutdown"] = now_iso
        data["sessions"][-1]["duration_seconds"] = round(duration, 1)

    # Accumulate monthly total
    if month not in data.get("monthly", {}):
        data.setdefault("monthly", {})[month] = {"total_seconds": 0, "session_count": 0}
    data["monthly"][month]["total_seconds"] = round(
        data["monthly"][month]["total_seconds"] + duration, 1
    )
    data["monthly"][month]["session_count"] += 1

    _save(data)
    log.info(f"Uptime tracker: shutdown recorded ({duration:.0f}s session)")


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable string."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def get_uptime() -> Dict[str, Any]:
    """
    Return current uptime statistics.

    Returns:
        {
            "current_session_seconds": float,
            "current_session_formatted": str,
            "last_boot": str (ISO),
            "total_sessions": int,
        }
    """
    session_seconds = time.time() - _boot_time if _boot_time > 0 else 0
    data = _load()
    return {
        "current_session_seconds": round(session_seconds, 1),
        "current_session_formatted": _format_duration(session_seconds),
        "last_boot": data.get("last_boot", "unknown"),
        "total_sessions": len(data.get("sessions", [])),
    }


def get_monthly_runtime() -> Dict[str, Any]:
    """
    Return monthly runtime accumulation.

    Returns:
        {
            "current_month": str,
            "current_month_seconds": float,
            "current_month_formatted": str,
            "all_months": { "2026-04": { "total_seconds": ..., "session_count": ... }, ... }
        }
    """
    data = _load()
    month = _month_key()
    monthly = data.get("monthly", {})

    # Add current session to this month's total for the live view
    current_session = time.time() - _boot_time if _boot_time > 0 else 0
    recorded = monthly.get(month, {}).get("total_seconds", 0)
    live_total = recorded + current_session

    return {
        "current_month": month,
        "current_month_seconds": round(live_total, 1),
        "current_month_formatted": _format_duration(live_total),
        "all_months": monthly,
    }
