"""
db.py — SQLite persistence for the local dashboard.

Tables:
  servers  — registered remote agents
  api_keys — mirror of keys created on each server (for display only)
"""
import sqlite3
import json
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

DB_PATH = Path.home() / ".ollama-dashboard" / "dashboard.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL,
                host      TEXT NOT NULL,           -- e.g. http://1.2.3.4:8080
                admin_token TEXT NOT NULL,
                added_at  TEXT NOT NULL,
                last_seen TEXT,
                notes     TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS key_cache (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                api_key   TEXT NOT NULL,
                label     TEXT,
                created_at TEXT,
                enabled   INTEGER DEFAULT 1,
                FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
            )
        """)
        conn.commit()


# ── Server CRUD ───────────────────────────────────────────────────────────────

def add_server(name: str, host: str, admin_token: str, notes: str = "") -> Dict:
    # Normalize host
    host = host.rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO servers (name, host, admin_token, added_at, notes) VALUES (?,?,?,?,?)",
            (name, host, admin_token, datetime.now(timezone.utc).isoformat(), notes),
        )
        conn.commit()
        return get_server(cur.lastrowid)


def get_server(server_id: int) -> Optional[Dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    return dict(row) if row else None


def list_servers() -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def update_server_seen(server_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE servers SET last_seen=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), server_id),
        )
        conn.commit()


def remove_server(server_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
        conn.commit()
    return cur.rowcount > 0


def update_server(server_id: int, name: str, host: str, admin_token: str, notes: str = "") -> Optional[Dict]:
    host = host.rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    with _conn() as conn:
        conn.execute(
            "UPDATE servers SET name=?, host=?, admin_token=?, notes=? WHERE id=?",
            (name, host, admin_token, notes, server_id),
        )
        conn.commit()
    return get_server(server_id)


# ── Key cache ─────────────────────────────────────────────────────────────────
# We cache keys locally so the dashboard can show them even when the agent is offline.

def cache_key(server_id: int, api_key: str, label: str = "", created_at: str = "") -> None:
    with _conn() as conn:
        exists = conn.execute(
            "SELECT id FROM key_cache WHERE server_id=? AND api_key=?", (server_id, api_key)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO key_cache (server_id, api_key, label, created_at) VALUES (?,?,?,?)",
                (server_id, api_key, label, created_at or datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()


def list_cached_keys(server_id: int) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM key_cache WHERE server_id=? ORDER BY id", (server_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def remove_cached_key(server_id: int, api_key: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM key_cache WHERE server_id=? AND api_key=?", (server_id, api_key)
        )
        conn.commit()
