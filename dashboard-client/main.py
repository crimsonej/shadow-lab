"""
main.py — Ollama Dashboard: Local Backend
==========================================
Runs on your local machine.
Proxies management calls to remote server agents and serves the web UI.

All routes under /api/* talk to remote agents.
GET / serves the single-page dashboard HTML.
"""
import asyncio
import logging
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DASHBOARD_PORT = 7860
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Ollama Dashboard", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db.init_db()

# Async HTTP client for calling remote agents
_http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _agent_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


async def _proxy_get(host: str, token: str, path: str):
    try:
        r = await _http.get(f"{host}{path}", headers=_agent_headers(token))
        return r.status_code, r.json()
    except httpx.ConnectError:
        return 503, {"error": "Agent unreachable"}
    except Exception as e:
        return 500, {"error": str(e)}


async def _proxy_post(host: str, token: str, path: str, body: dict):
    try:
        r = await _http.post(f"{host}{path}", json=body, headers=_agent_headers(token))
        return r.status_code, r.json()
    except httpx.ConnectError:
        return 503, {"error": "Agent unreachable"}
    except Exception as e:
        return 500, {"error": str(e)}


async def _proxy_delete(host: str, token: str, path: str, body: dict):
    try:
        r = await _http.request("DELETE", f"{host}{path}", json=body, headers=_agent_headers(token))
        return r.status_code, r.json()
    except httpx.ConnectError:
        return 503, {"error": "Agent unreachable"}
    except Exception as e:
        return 500, {"error": str(e)}


# ── Serve dashboard UI ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_file = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_file.read_text())


# ── Server management ─────────────────────────────────────────────────────────

class AddServerRequest(BaseModel):
    name: str
    host: str
    admin_token: str
    notes: str = ""


class UpdateServerRequest(BaseModel):
    name: str
    host: str
    admin_token: str
    notes: str = ""


@app.get("/api/servers")
async def api_list_servers():
    servers = db.list_servers()
    # Hide admin_token in listing
    for s in servers:
        s["admin_token"] = "***"
    return servers


@app.post("/api/servers")
async def api_add_server(body: AddServerRequest):
    server = db.add_server(body.name, body.host, body.admin_token, body.notes)
    return server


@app.put("/api/servers/{server_id}")
async def api_update_server(server_id: int, body: UpdateServerRequest):
    server = db.update_server(server_id, body.name, body.host, body.admin_token, body.notes)
    if not server:
        raise HTTPException(404, "Server not found")
    return server


@app.delete("/api/servers/{server_id}")
async def api_remove_server(server_id: int):
    ok = db.remove_server(server_id)
    if not ok:
        raise HTTPException(404, "Server not found")
    return {"status": "removed"}


# ── Server status (health + metrics) ─────────────────────────────────────────

@app.get("/api/servers/{server_id}/status")
async def api_server_status(server_id: int):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")

    status_code, health = await _proxy_get(server["host"], server["admin_token"], "/v1/health")
    metrics_code, metrics_data = await _proxy_get(server["host"], server["admin_token"], "/admin/metrics")
    models_code, models_data = await _proxy_get(server["host"], server["admin_token"], "/admin/models")

    if status_code == 200:
        db.update_server_seen(server_id)

    return {
        "online": status_code == 200,
        "health": health if status_code == 200 else None,
        "metrics": metrics_data if metrics_code == 200 else None,
        "models": models_data.get("models", []) if models_code == 200 else [],
    }


# ── Model management ──────────────────────────────────────────────────────────

class PullModelBody(BaseModel):
    name: str


class DeleteModelBody(BaseModel):
    name: str


@app.post("/api/servers/{server_id}/models/pull")
async def api_pull_model(server_id: int, body: PullModelBody):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    # This is a long operation; just kick it off and return immediately
    # The dashboard can poll /status to see when the model appears
    async def do_pull():
        try:
            async with _http.stream(
                "POST",
                f"{server['host']}/admin/models/pull",
                json={"name": body.name},
                headers=_agent_headers(server["admin_token"]),
                timeout=600,
            ) as r:
                async for _ in r.aiter_lines():
                    pass
        except Exception as e:
            log.error(f"Pull error: {e}")

    asyncio.create_task(do_pull())
    return {"status": "pulling", "model": body.name, "note": "Poll /status to track progress"}


@app.delete("/api/servers/{server_id}/models")
async def api_delete_model(server_id: int, body: DeleteModelBody):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    code, data = await _proxy_delete(server["host"], server["admin_token"], "/admin/models", {"name": body.name})
    if code != 200:
        raise HTTPException(code, data.get("error", "Failed"))
    return data


# ── API key management ────────────────────────────────────────────────────────

class CreateKeyBody(BaseModel):
    label: str = ""
    limit_rpm: int = 0


class RevokeKeyBody(BaseModel):
    key: str


class DeleteKeyBody(BaseModel):
    key: str


@app.get("/api/servers/{server_id}/keys")
async def api_list_keys(server_id: int):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    code, data = await _proxy_get(server["host"], server["admin_token"], "/admin/keys")
    if code == 200:
        return data
    # Fallback to local cache
    return db.list_cached_keys(server_id)


@app.post("/api/servers/{server_id}/keys")
async def api_create_key(server_id: int, body: CreateKeyBody):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    code, data = await _proxy_post(
        server["host"], server["admin_token"], "/admin/keys",
        {"label": body.label, "limit_rpm": body.limit_rpm}
    )
    if code != 200:
        raise HTTPException(code, data.get("error", "Failed to create key"))
    # Cache locally
    db.cache_key(server_id, data["key"], data.get("label", ""), data.get("created_at", ""))
    return data


@app.post("/api/servers/{server_id}/keys/revoke")
async def api_revoke_key(server_id: int, body: RevokeKeyBody):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    code, data = await _proxy_post(
        server["host"], server["admin_token"], "/admin/keys/revoke", {"key": body.key}
    )
    if code != 200:
        raise HTTPException(code, data.get("error", "Failed"))
    return data


@app.delete("/api/servers/{server_id}/keys")
async def api_delete_key(server_id: int, body: DeleteKeyBody):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    code, data = await _proxy_delete(
        server["host"], server["admin_token"], "/admin/keys", {"key": body.key}
    )
    if code != 200:
        raise HTTPException(code, data.get("error", "Failed"))
    db.remove_cached_key(server_id, body.key)
    return data


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  Ollama Dashboard running at: http://localhost:{DASHBOARD_PORT}\n")
    uvicorn.run("main:app", host="127.0.0.1", port=DASHBOARD_PORT, reload=False, log_level="warning")
