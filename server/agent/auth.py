import sqlite3
import secrets
from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from config import settings

router = APIRouter(prefix="/v1/admin", tags=["admin"])
security = HTTPBearer()

def get_db():
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key TEXT PRIMARY KEY,
            name TEXT,
            usage_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

# Initialize DB on import
init_db()

def verify_admin(credentials: HTTPAuthorizationCredentials = Security(security)):
    if not settings.ADMIN_KEY or credentials.credentials != settings.ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid Admin Key")
    return credentials.credentials

async def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    key = credentials.credentials
    conn = get_db()
    row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (key,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid API Key")
    conn.close()
    return key

def increment_usage(key: str):
    conn = get_db()
    conn.execute("UPDATE api_keys SET usage_count = usage_count + 1 WHERE key = ?", (key,))
    conn.commit()
    conn.close()

@router.post("/keys")
def generate_key(name: str, admin: str = Depends(verify_admin)):
    new_key = f"sk-shadow-{secrets.token_urlsafe(32)}"
    conn = get_db()
    try:
        conn.execute("INSERT INTO api_keys (key, name) VALUES (?, ?)", (new_key, name))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=500, detail="Error generating key")
    conn.close()
    return {"key": new_key, "name": name}

@router.get("/keys")
def list_keys(admin: str = Depends(verify_admin)):
    conn = get_db()
    rows = conn.execute("SELECT key, name, usage_count, created_at FROM api_keys").fetchall()
    conn.close()
    return {"keys": [dict(r) for r in rows]}

@router.delete("/keys/{key}")
def revoke_key(key: str, admin: str = Depends(verify_admin)):
    conn = get_db()
    conn.execute("DELETE FROM api_keys WHERE key = ?", (key,))
    conn.commit()
    conn.close()
    return {"status": "revoked", "key": key}
