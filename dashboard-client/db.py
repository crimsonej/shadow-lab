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
        # ── Control Plane: uptime snapshots ───────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS uptime_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id   INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                uptime_seconds REAL,
                monthly_seconds REAL,
                FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
            )
        """)
        conn.commit()

        # ── Schema migration: add SSH columns if missing ──────────────────
        _migrate_add_column(conn, "servers", "connection_type", "TEXT DEFAULT 'http'")
        _migrate_add_column(conn, "servers", "ssh_host", "TEXT DEFAULT ''")
        _migrate_add_column(conn, "servers", "ssh_user", "TEXT DEFAULT ''")
        _migrate_add_column(conn, "servers", "ssh_port", "INTEGER DEFAULT 22")
        _migrate_add_column(conn, "servers", "ssh_key_path", "TEXT DEFAULT ''")


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


# ── Schema migration helper ──────────────────────────────────────────────────

def _migrate_add_column(conn, table: str, column: str, col_type: str) -> None:
    """Add a column to a table if it doesn't already exist. Idempotent."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


# ── Uptime snapshots ─────────────────────────────────────────────────────────

def record_uptime_snapshot(
    server_id: int, uptime_seconds: float, monthly_seconds: float
) -> None:
    """Record a point-in-time uptime snapshot for a server."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO uptime_snapshots (server_id, recorded_at, uptime_seconds, monthly_seconds) VALUES (?,?,?,?)",
            (server_id, datetime.now(timezone.utc).isoformat(), uptime_seconds, monthly_seconds),
        )
        conn.commit()


def get_uptime_history(server_id: int, limit: int = 100) -> List[Dict]:
    """Return recent uptime snapshots for a server."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM uptime_snapshots WHERE server_id=? ORDER BY id DESC LIMIT ?",
            (server_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Extended server CRUD with SSH fields ─────────────────────────────────────

def add_server_full(
    name: str, host: str, admin_token: str, notes: str = "",
    connection_type: str = "http", ssh_host: str = "", ssh_user: str = "",
    ssh_port: int = 22, ssh_key_path: str = "",
) -> Dict:
    """Add a server with full SSH configuration."""
    host = host.rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO servers (name, host, admin_token, added_at, notes, "
            "connection_type, ssh_host, ssh_user, ssh_port, ssh_key_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, host, admin_token, datetime.now(timezone.utc).isoformat(),
             notes, connection_type, ssh_host, ssh_user, ssh_port, ssh_key_path),
        )
        conn.commit()
        return get_server(cur.lastrowid)


def update_server_full(
    server_id: int, name: str, host: str, admin_token: str, notes: str = "",
    connection_type: str = "http", ssh_host: str = "", ssh_user: str = "",
    ssh_port: int = 22, ssh_key_path: str = "",
) -> Optional[Dict]:
    """Update a server with full SSH configuration."""
    host = host.rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    with _conn() as conn:
        conn.execute(
            "UPDATE servers SET name=?, host=?, admin_token=?, notes=?, "
            "connection_type=?, ssh_host=?, ssh_user=?, ssh_port=?, ssh_key_path=? "
            "WHERE id=?",
            (name, host, admin_token, notes,
             connection_type, ssh_host, ssh_user, ssh_port, ssh_key_path, server_id),
        )
        conn.commit()
    return get_server(server_id)
